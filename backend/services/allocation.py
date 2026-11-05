"""账号自动分配（契约 §3.2）与人工改派校验。

参与的账号：未归档、purchase_enabled、未 execution_paused、站点相符（美国站 = 收货国家 US）。
参与的任务（「池」）：queued；assigned 且 assignment_kind='auto'、账号仍参与、无在途尝试的（自动分配可重算）；
以及分配到已归档账号的 assigned 任务。人工分配（manual）固定不动但占用容量；分给已关闭采购开关或
已暂停执行的账号的任务原样保留（「关闭采购开关只影响新分配」「暂停执行的账号已分配任务保留」）。

计划日期 plan(t) = max(建议采购日期, 今天)，按日期从早到晚逐日处理。日期 D 内：

- cap(A, D) = daily_limit − used(A, D) − fixed(A, D)；used 按工作区的模拟模式统计（services/quota）。
- covered(A, D)：D 日 A 已有占用额度的尝试（used > 0），或已有人工固定分配（fixed > 0）。
- eligible(t, A)：min_amount ≤ subtotal_eff(t) ≤ max_amount（端点含，max 为空 = 不限）且 cap(A, D) > 0。

1. 保底：未覆盖且有额度的账号与当天任务做最大二分匹配（增广路）。账号按（可选任务数升序、优先级降序、
   名称升序）依次找增广路；候选任务按（可选账号数升序、建议采购日期、下单时间、id）。标注「保底：当天首单」。
2. 填充：剩余任务按（建议采购日期、下单时间、id）依次给可选账号中优先级最高者，同优先级按名称升序；
   标注「按优先级填充」。
3. 分不出去的任务留在 queued，status_reason 写明原因；未覆盖的账号写进 allocation_runs.uncovered。

名称排序按 Unicode 码位（Python 字符串比较），与数据库排序规则无关，保证结果可复现。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from services import clock, events, orders, quota, settings_svc
from services.errors import validation
from services.models import ATTEMPT_INFLIGHT

TRIGGERS = ("intake", "import", "manual", "account_change", "daily")
NOTE_COVERAGE = "保底：当天首单"
NOTE_FILL = "按优先级填充"
REASON_NO_ACCOUNT = "没有可接单的买家账号（均未启用采购、已暂停执行或已归档）"
REASON_FULL = "匹配账号当天额度已满"
UNCOVERED_NO_QUOTA = "当天额度已用完"
UNCOVERED_NO_MATCH = "没有金额匹配的订单"
UNCOVERED_NOT_ENOUGH = "订单数量不足"

_LOCK_NS = 7_310_431          # pg_advisory_xact_lock 的命名空间（按工作区串行化分配）
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_INFLIGHT = sorted(ATTEMPT_INFLIGHT)


def _usd(v: Decimal | None) -> str:
    return "未知金额" if v is None else f"${v:.2f}"


def _reason_out_of_range(sub: Decimal | None) -> str:
    if sub is None:
        return "商品小计未知（缺少采购单价或数量），无法匹配账号"
    return f"金额 {_usd(sub)} 不在任何启用账号的区间内"


def _site_ok(order_row: dict[str, Any], account_row: dict[str, Any]) -> bool:
    return (order_row.get("country") or "US").upper() == account_row["site"]


def _task_key(t: dict[str, Any]) -> tuple:
    return (t["suggested_purchase_date"] or date.max, t["order_time"] is None, t["order_time"] or _EPOCH, t["id"])


def plan_date(order_row: dict[str, Any], today: date) -> date:
    """输入：任务行、今天 → 输出：计划日期 max(建议采购日期, 今天)（缺日期按今天）。"""
    d = order_row.get("suggested_purchase_date")
    return today if d is None or d < today else d


def allocate(conn, workspace_id: int, *, today: date | None = None, trigger: str, actor_kind: str = "system",
             actor_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、今天（缺省北京今天）、触发方式、操作者 → 输出：分配摘要（契约 §10，并写 allocation_runs）。

    只改「池」里的任务（见模块说明）；同一工作区的分配用事务级咨询锁串行化。
    """
    if trigger not in TRIGGERS:
        raise validation(f"未知的分配触发方式：{trigger}", field="trigger")
    today = today or clock.bj_today()
    conn.execute("SELECT pg_advisory_xact_lock(%s::int, %s::int)", (_LOCK_NS, workspace_id % 2_147_483_647))
    simulated = settings_svc.simulation_only(conn, workspace_id)

    accounts = conn.execute(
        "SELECT * FROM procure.buyer_accounts WHERE workspace_id = %s AND archived_at IS NULL "
        "AND purchase_enabled AND NOT execution_paused",
        (workspace_id,),
    ).fetchall()
    accounts.sort(key=lambda a: a["name"])
    acc_ids = [a["id"] for a in accounts]
    by_id = {a["id"]: a for a in accounts}

    pool = conn.execute(
        """
        SELECT o.* FROM procure.purchase_orders o
        LEFT JOIN procure.buyer_accounts a ON a.id = o.assigned_account_id AND a.workspace_id = o.workspace_id
        WHERE o.workspace_id = %s
          AND (o.status = 'queued'
               OR (o.status = 'assigned' AND o.assignment_kind = 'auto' AND o.assigned_account_id = ANY(%s))
               OR (o.status = 'assigned' AND (a.id IS NULL OR a.archived_at IS NOT NULL)))
          AND NOT EXISTS (SELECT 1 FROM procure.purchase_attempts pa
                          WHERE pa.purchase_order_id = o.id AND pa.status = ANY(%s))
        ORDER BY o.id
        FOR UPDATE OF o
        """,
        (workspace_id, acc_ids, _INFLIGHT),
    ).fetchall()

    fixed = {k: v for k, v in quota.fixed_map(conn, workspace_id, today).items() if k[0] in by_id}
    by_date: dict[date, list[dict[str, Any]]] = {}
    for t in pool:
        by_date.setdefault(plan_date(t, today), []).append(t)
    dates = sorted(set(by_date) | {today})
    used = quota.used_map(conn, workspace_id, dates, simulated)

    result: dict[int, tuple[int | None, str]] = {}      # 任务 id → (账号 id 或 None, 分配说明或未分配原因)
    uncovered: list[dict[str, Any]] = []
    n_cov = n_fill = 0

    for d in dates:
        tasks = sorted(by_date.get(d, []), key=_task_key)
        sub = {t["id"]: orders.subtotal_eff(t) for t in tasks}
        cap = {a["id"]: a["daily_limit"] - used.get((a["id"], d), 0) - fixed.get((a["id"], d), 0) for a in accounts}
        covered = {a["id"]: used.get((a["id"], d), 0) > 0 or fixed.get((a["id"], d), 0) > 0 for a in accounts}

        def in_range(t: dict[str, Any], a: dict[str, Any]) -> bool:
            return _site_ok(t, a) and orders.amount_in_range(sub[t["id"]], a)

        # ① 保底：最大二分匹配
        cov_accounts = [a for a in accounts if not covered[a["id"]] and cap[a["id"]] > 0]
        opts = {a["id"]: [t for t in tasks if in_range(t, a)] for a in cov_accounts}
        deg: dict[int, int] = {}
        for a in cov_accounts:
            for t in opts[a["id"]]:
                deg[t["id"]] = deg.get(t["id"], 0) + 1
        cov_accounts.sort(key=lambda a: (len(opts[a["id"]]), -a["priority"], a["name"]))
        for a in cov_accounts:
            opts[a["id"]].sort(key=lambda t: (deg[t["id"]], *_task_key(t)))
        match: dict[int, int] = {}                       # 任务 id → 账号 id

        def augment(aid: int, seen: set[int]) -> bool:
            for t in opts[aid]:
                tid = t["id"]
                if tid in seen:
                    continue
                seen.add(tid)
                if tid not in match or augment(match[tid], seen):
                    match[tid] = aid
                    return True
            return False

        for a in cov_accounts:
            augment(a["id"], set())
        matched_accounts = set(match.values())
        for tid, aid in match.items():
            result[tid] = (aid, NOTE_COVERAGE)
            cap[aid] -= 1
            n_cov += 1

        # ② 填充
        for t in tasks:
            if t["id"] in match:
                continue
            cands = [a for a in accounts if cap[a["id"]] > 0 and in_range(t, a)]
            if cands:
                best = min(cands, key=lambda a: (-a["priority"], a["name"]))
                result[t["id"]] = (best["id"], NOTE_FILL)
                cap[best["id"]] -= 1
                n_fill += 1
            elif not accounts:
                result[t["id"]] = (None, REASON_NO_ACCOUNT)
            elif not any(_site_ok(t, a) for a in accounts):
                result[t["id"]] = (None, f"收货国家 {t.get('country') or '（空）'} 没有对应站点的启用账号")
            elif any(in_range(t, a) for a in accounts):
                result[t["id"]] = (None, REASON_FULL)
            else:
                result[t["id"]] = (None, _reason_out_of_range(sub[t["id"]]))

        # ③ 未覆盖的账号
        for a in accounts:
            if covered[a["id"]] or a["id"] in matched_accounts:
                continue
            if a["daily_limit"] - used.get((a["id"], d), 0) - fixed.get((a["id"], d), 0) <= 0:
                why = UNCOVERED_NO_QUOTA
            elif not any(in_range(t, a) for t in tasks):
                why = UNCOVERED_NO_MATCH
            else:
                why = UNCOVERED_NOT_ENOUGH
            uncovered.append({"account_id": a["id"], "name": a["name"], "date": d.isoformat(), "reason": why})

    # ④ 写回任务
    unassigned: list[dict[str, Any]] = []
    now = clock.now()
    for t in pool:
        aid, text = result[t["id"]]
        if aid is None:
            unassigned.append({"order_id": t["id"], "upstream_order_no": t["upstream_order_no"], "reason": text})
            if t["status"] == "assigned":
                orders.transition(conn, t, "queued", reason=text, actor_kind=actor_kind, actor_id=actor_id,
                                  event_detail={"allocation_trigger": trigger})
            elif t["status_reason"] != text:
                orders._update(conn, workspace_id, t["id"], {"status_reason": text})
            continue
        acc = by_id[aid]
        upd = {"assigned_account_id": aid, "assignment_kind": "auto", "assigned_at": now, "assignment_note": text}
        detail = {"account_id": aid, "account_name": acc["name"], "note": text, "trigger": trigger,
                  "from_account_id": t["assigned_account_id"]}
        if t["status"] == "queued":
            orders.transition(conn, t, "assigned", reason=None, actor_kind=actor_kind, actor_id=actor_id,
                              updates=upd, event_detail={"allocation_trigger": trigger})
            events.record(conn, workspace_id, "order", t["id"], "order.assigned", actor_kind=actor_kind,
                                 actor_id=actor_id, detail={**detail, "kind": "auto"})
        elif t["assigned_account_id"] != aid or t["assignment_kind"] != "auto":
            orders._update(conn, workspace_id, t["id"], {**upd, "status_reason": None})
            events.record(conn, workspace_id, "order", t["id"], "order.reassigned", actor_kind=actor_kind,
                                 actor_id=actor_id, detail={**detail, "kind": "auto"})
        elif t["assignment_note"] != text:
            orders._update(conn, workspace_id, t["id"], {"assignment_note": text})

    stats = {"considered": len(pool), "assigned": n_cov + n_fill, "coverage": n_cov, "fill": n_fill,
             "unassigned": len(unassigned)}
    run = conn.execute(
        "INSERT INTO procure.allocation_runs (workspace_id, trigger, bj_date, at, stats, uncovered, unassigned) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (workspace_id, trigger, today, now, Jsonb(stats), Jsonb(uncovered), Jsonb(unassigned)),
    ).fetchone()
    return _run_out(run)


def _run_out(run: dict[str, Any]) -> dict[str, Any]:
    return {"id": run["id"], "bj_date": run["bj_date"].isoformat(), "at": run["at"].isoformat(),
            "trigger": run["trigger"], "stats": run["stats"], "uncovered": run["uncovered"],
            "unassigned": run["unassigned"]}


def last_run(conn, workspace_id: int) -> dict[str, Any] | None:
    """输入：工作区 → 输出：最近一次分配摘要（契约 §10），没有 → None。"""
    run = conn.execute(
        "SELECT * FROM procure.allocation_runs WHERE workspace_id = %s ORDER BY at DESC, id DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    return _run_out(run) if run else None


def check_manual_assignment(conn, workspace_id: int, order_row: dict[str, Any], account_row: dict[str, Any],
                            today: date) -> None:
    """输入：工作区、任务行、目标账号行、今天 → 输出：无；不满足改派条件时抛 VALIDATION（中文原因）。

    条件：同一工作区、账号未归档且已启用采购、任务待分配或已分配、站点相符、金额在账号区间内（端点含）、
    计划日期当天有剩余额度：daily_limit − used − planned > 0（本任务已计划在该账号同一天时不重复计算）。
    暂停执行的账号允许人工改派（恢复执行后再采购）。
    """
    if account_row["workspace_id"] != workspace_id or order_row["workspace_id"] != workspace_id:
        raise validation("账号与任务不在同一工作区")
    name = account_row["name"]
    if account_row["archived_at"] is not None:
        raise validation(f"账号 {name} 已归档，不能分配")
    if not account_row["purchase_enabled"]:
        raise validation(f"账号 {name} 已关闭采购开关，不接收新分配")
    if order_row["status"] not in ("queued", "assigned"):
        raise validation("只有待分配或已分配的任务可以改派")
    if not _site_ok(order_row, account_row):
        raise validation(f"任务收货国家与账号 {name} 的站点不符")
    sub = orders.subtotal_eff(order_row)
    if sub is None:
        raise validation("商品小计未知（缺少采购单价或数量），不能分配")
    if not orders.amount_in_range(sub, account_row):
        mx = account_row["max_amount"]
        rng = f"{_usd(account_row['min_amount'])}–{_usd(mx) if mx is not None else '不限'}"
        raise validation(f"金额 {_usd(sub)} 不在账号 {name} 的区间 {rng} 内")
    d = plan_date(order_row, today)
    simulated = settings_svc.simulation_only(conn, workspace_id)
    u = quota.used(conn, account_row["id"], d, simulated, workspace_id=workspace_id)
    p = quota.planned(conn, account_row["id"], d, today=today, workspace_id=workspace_id)
    if (order_row["status"] == "assigned" and order_row["assigned_account_id"] == account_row["id"]
            and plan_date(order_row, today) == d):
        p -= 1
    if account_row["daily_limit"] - u - p <= 0:
        raise validation(f"账号 {name} 在 {d.isoformat()} 的额度已满（上限 {account_row['daily_limit']}，"
                         f"已用 {u}，已计划 {p}）")
