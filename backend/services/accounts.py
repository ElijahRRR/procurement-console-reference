"""买家账号：配置校验、列表与详情（含当天额度、所在浏览器、当前执行批次）、暂停 / 恢复执行、待登记的买家号。

- 账号由所有者在控制台登记；账号上登记的买家号 ID（亚马逊 customerId）就是它的执行身份：任何登录着这个买家号的
  浏览器都能执行它的任务，与哪台电脑、哪个浏览器无关。插件不会自动创建账号。
- **待登记的买家号**（discovered）：浏览器上报了、或来源给未采购的任务指定了（purchase_orders.designated_customer_id），
  但还没有被任何未归档账号登记的买家号 ID。所有者可以登记（新建账号或填到已有账号）或忽略
  （ignore_buyer_id / unignore_buyer_id）；登记某个 ID 时同时清掉它的忽略记录，并立即重新分配（指定它的任务随即分给它）。
- 修改或清空买家号 ID：账号有未结束批次、在途或结果待核实的尝试时拒绝（执行中的身份检查拿它当基准）。
- 账号名在工作区内唯一（含已归档的，库里有唯一约束）；跨工作区访问一律 NOT_FOUND。
- 影响分配的配置变化（金额区间、每日额度、优先级、采购开关、站点，以及**名称与买家号 ID**：来源指定靠它们对应
  账号）以及新建（不论采购开关开没开）、恢复执行、归档后，立即按今天重新分配
  （allocation.allocate(trigger='account_change')）。暂停执行不重新分配：暂停的账号不参与自动分配，已分配给它的任务保留。
  忽略 / 恢复一个买家号 ID 时，如果有待分配的任务指定了它，也重新分配一次（只会刷新这些任务的未分配原因：原因里写着
  这个 ID 是否已被忽略）。
- 加锁顺序（契约 §3.2）：账号行（FOR NO KEY UPDATE，不挡外键检查的 KEY SHARE）→ 工作区分配锁
  （allocation.lock_workspace）→ 任务行。改了影响分配的字段时先取分配锁再改账号行；改名还要先锁齐任务行再改
  账号行（名称在唯一约束里，改名的 UPDATE 拿的行锁会挡外键检查）；归档在锁它的任务行之前先取分配锁。
- 在线 = 3 分钟内有联系。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg

from services import clock, events, quota, settings_svc
from services.errors import conflict, not_found, validation
from services.models import ATTEMPT_INFLIGHT, CUSTOMER_ID_RE, money

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CARD_RE = re.compile(r"^[0-9]{4}$")

#: 前端可能整份回传的只读键，忽略
_READONLY = frozenset({"id", "workspace_id", "created_at", "updated_at", "archived_at", "quota_today", "browsers",
                       "active_run", "effective_max_delivery_days", "execution_paused", "pause_reason", "paused_at",
                       "pause_cause", "open_result_unknown"})
_EDITABLE = ("name", "amazon_customer_id", "site", "min_amount", "max_amount", "daily_limit", "priority",
             "max_delivery_days", "purchase_enabled", "sync_enabled", "purchase_mode", "schedule_times",
             "expected_card_last4", "notes")
#: 这些字段变化会改变分配结果（名称与买家号 ID 决定来源指定的任务去哪个账号）
ALLOCATION_FIELDS = frozenset({"min_amount", "max_amount", "daily_limit", "priority", "purchase_enabled", "site",
                               "name", "amazon_customer_id"})
#: 待登记的买家号只看这些状态之外的任务上的来源指定（已经买完或不会再买的不算）
_DESIGNATION_DONE_STATUSES = ("purchased", "closed", "cancelled")
#: 账号输出的 pause_cause 取值（未暂停为 None）
PAUSE_CAUSES = ("result_unknown", "account_error", "manual")
#: 执行模块写的暂停标记里可能出现的起因（见 execution.PAUSE_MARKER_KIND）
_EXECUTION_CAUSES = frozenset({"result_unknown", "account_error"})


# ── 校验 ─────────────────────────────────────────────────────────────────────


def _int_field(v: Any, label: str, field: str, lo: int, hi: int, *, nullable: bool = False) -> int | None:
    if v is None and nullable:
        return None
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        v = int(v.strip())
    if isinstance(v, bool) or not isinstance(v, int):
        raise validation(f"{label}必须是整数", field=field)
    if not lo <= v <= hi:
        raise validation(f"{label}必须在 {lo} 到 {hi} 之间", field=field)
    return v


def _money_field(v: Any, label: str, field: str, *, nullable: bool) -> Decimal | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        if nullable:
            return None
        raise validation(f"{label}不能为空", field=field)
    m = None if isinstance(v, bool) else money(v)
    if m is None:
        raise validation(f"{label}格式不对", field=field)
    if m < 0:
        raise validation(f"{label}不能为负数", field=field)
    return m


def _text_field(v: Any, label: str, field: str, max_len: int) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise validation(f"{label}必须是文本", field=field)
    s = v.strip()
    if len(s) > max_len:
        raise validation(f"{label}不能超过 {max_len} 个字", field=field)
    return s or None


def _validate(conn, ws: int, data: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    """输入：工作区、提交的字段、现有账号行（新建为 None）→ 输出：规范化后的待写字段。"""
    if not isinstance(data, dict):
        raise validation("账号内容格式不对")
    out: dict[str, Any] = {}
    for key, v in data.items():
        if key in _READONLY:
            continue
        if key not in _EDITABLE:
            raise validation(f"未知的账号字段：{key}", field=key)
        if key == "name":
            name = _text_field(v, "账号名称", key, 64)
            if not name:
                raise validation("账号名称不能为空", field=key)
            out[key] = name
        elif key == "amazon_customer_id":
            s = _text_field(v, "买家号 ID", key, 64)
            s = s.upper() if s else None
            if s is not None and not CUSTOMER_ID_RE.match(s):
                raise validation("买家号 ID 格式不对（应以 A 开头，至少 9 位字母或数字）", field=key)
            out[key] = s
        elif key == "site":
            if v != "US":
                raise validation("目前只支持美国站（US）", field=key)
            out[key] = v
        elif key == "min_amount":
            out[key] = _money_field(v, "最低金额", key, nullable=False)
        elif key == "max_amount":
            out[key] = _money_field(v, "最高金额", key, nullable=True)
        elif key == "daily_limit":
            out[key] = _int_field(v, "每日最大采购单数", key, 0, 1000)
        elif key == "priority":
            out[key] = _int_field(v, "优先级", key, 0, 9)
        elif key == "max_delivery_days":
            out[key] = _int_field(v, "最长配送天数", key, 1, 60, nullable=True)
        elif key in ("purchase_enabled", "sync_enabled"):
            if not isinstance(v, bool):
                raise validation("开关必须是 true 或 false", field=key)
            out[key] = v
        elif key == "purchase_mode":
            if v not in ("manual", "scheduled"):
                raise validation("执行模式只能是 manual 或 scheduled", field=key)
            out[key] = v
        elif key == "schedule_times":
            if v is None:
                v = []
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise validation("执行时间应为 HH:MM 列表", field=key)
            times = sorted({x.strip() for x in v})
            bad = [x for x in times if not _HHMM_RE.match(x)]
            if bad:
                raise validation(f"执行时间格式不对：{'、'.join(bad)}（应为北京时间 HH:MM）", field=key)
            if len(times) > 24:
                raise validation("执行时间最多 24 个", field=key)
            out[key] = times
        elif key == "expected_card_last4":
            s = _text_field(v, "期望卡尾号", key, 4)
            if s is not None and not _CARD_RE.match(s):
                raise validation("期望卡尾号应为 4 位数字", field=key)
            out[key] = s
        elif key == "notes":
            out[key] = _text_field(v, "备注", key, 2000)

    merged = {**(current or {}), **out}
    if current is None and not merged.get("name"):
        raise validation("账号名称不能为空", field="name")
    mn = merged.get("min_amount", Decimal("0")) or Decimal("0")
    mx = merged.get("max_amount")
    if mx is not None and mn > mx:
        raise validation("最低金额不能高于最高金额", field="min_amount")
    if merged.get("purchase_mode") == "scheduled" and not merged.get("schedule_times"):
        raise validation("定时执行需要至少设置一个执行时间", field="schedule_times")
    if "name" in out:
        dup = conn.execute(
            "SELECT 1 FROM procure.buyer_accounts WHERE workspace_id = %s AND name = %s AND id <> %s",
            (ws, out["name"], current["id"] if current else -1),
        ).fetchone()
        if dup:
            raise validation(f"账号名称「{out['name']}」已存在", field="name")
    if out.get("amazon_customer_id"):
        _check_customer_id_unique(conn, ws, out["amazon_customer_id"], current["id"] if current else None)
    return out


def _unique_write(conn, sql: str, params: list[Any] | tuple[Any, ...]):
    """在保存点里执行一条会碰唯一约束的写入：并发下应用层检查没拦住的重复（名称、买家号 ID）→ VALIDATION。"""
    try:
        with conn.transaction():
            return conn.execute(sql, params)
    except psycopg.errors.UniqueViolation as exc:
        cname = getattr(exc.diag, "constraint_name", None) or ""
        if "customer_id" in cname:
            raise validation("这个买家号 ID 已登记在另一个账号上", field="amazon_customer_id") from None
        if "name" in cname:
            raise validation("账号名称已存在", field="name") from None
        raise conflict("账号与已有账号冲突，请刷新后重试") from None


def _check_customer_id_unique(conn, ws: int, customer_id: str, account_id: int | None) -> None:
    other = conn.execute(
        "SELECT name FROM procure.buyer_accounts WHERE workspace_id = %s AND amazon_customer_id = %s "
        "AND archived_at IS NULL AND id <> %s",
        (ws, customer_id, account_id if account_id is not None else -1),
    ).fetchone()
    if other:
        raise validation(f"买家号 ID {customer_id} 已登记在账号 {other['name']}", field="amazon_customer_id")


# ── 读取 ─────────────────────────────────────────────────────────────────────


def get_row(conn, workspace_id: int, account_id: int, for_update: bool = False) -> dict[str, Any]:
    """输入：工作区、账号 id、是否加行锁 → 输出：buyer_accounts 行（含已归档）；不存在或跨工作区 → NOT_FOUND。

    for_update=True 加的是 FOR NO KEY UPDATE：同一账号的修改 / 暂停 / 恢复 / 归档照样互相串行，但不挡别的事务插入
    引用这个账号的行（采购尝试、分配写回）时外键检查要的 FOR KEY SHARE，否则「拿着账号行等分配锁」与「拿着分配锁
    （或任务行）等外键检查」会互相等待成死锁。账号行从不删除，主键也不变。
    """
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        raise not_found("买家账号") from None
    row = conn.execute(
        "SELECT * FROM procure.buyer_accounts WHERE id = %s AND workspace_id = %s"
        + (" FOR NO KEY UPDATE" if for_update else ""),
        (aid, workspace_id),
    ).fetchone()
    if row is None:
        raise not_found("买家账号")
    return row


def find_by_customer_id(conn, workspace_id: int, customer_id: str | None) -> dict[str, Any] | None:
    """输入：工作区、买家号 ID → 输出：本工作区登记了这个 ID 的未归档账号行；没有 → None。
    浏览器登录状态的解析与开始批次都按它找账号（库里的唯一索引保证最多一行）。"""
    if not customer_id:
        return None
    return conn.execute(
        "SELECT * FROM procure.buyer_accounts WHERE workspace_id = %s AND amazon_customer_id = %s "
        "AND archived_at IS NULL",
        (workspace_id, customer_id),
    ).fetchone()


def effective_max_delivery_days(conn, workspace_id: int, account_row: dict[str, Any]) -> int:
    """输入：工作区、账号行 → 输出：有效最长配送天数（账号覆盖值，否则全局默认）。"""
    if account_row.get("max_delivery_days") is not None:
        return int(account_row["max_delivery_days"])
    return settings_svc.default_max_delivery_days(conn, workspace_id)


def _m(v: Any) -> str | None:
    return None if v is None else f"{Decimal(v):.2f}"


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def pause_cause(conn, workspace_id: int, row: dict[str, Any]) -> str | None:
    """输入：工作区、账号行 → 输出：当前暂停的起因 result_unknown / account_error / manual；未暂停 → None。

    起因按「最近一次暂停」判定（pause_reason 也是那一次写的）：执行模块暂停账号时紧跟 'account.paused' 写一条
    execution.PAUSE_MARKER_KIND 标记 {cause}，有标记就取它的 cause，没有就是人工暂停。没有任何暂停事件
    （旧数据）时按 pause_reason 判断：以「结果待核实」开头 → result_unknown，否则 manual。
    """
    if not row["execution_paused"]:
        return None
    from services import execution      # 延迟导入：execution 在模块级导入本模块

    marker = execution.PAUSE_MARKER_KIND
    latest = conn.execute(
        "SELECT kind, detail FROM procure.events WHERE workspace_id = %s AND entity = 'account' AND entity_id = %s "
        "AND kind = ANY(%s) ORDER BY id DESC LIMIT 1",
        (workspace_id, row["id"], ["account.paused", marker]),
    ).fetchone()
    if latest is None:
        return "result_unknown" if (row["pause_reason"] or "").startswith("结果待核实") else "manual"
    if latest["kind"] != marker:
        return "manual"
    cause = (latest["detail"] or {}).get("cause")
    return cause if cause in _EXECUTION_CAUSES else "account_error"


def open_result_unknown(conn, workspace_id: int, account_id: int) -> int:
    """输入：工作区、账号 id → 输出：这个账号当前处在「结果待核实」的任务数（按待核实尝试的账号，
    没有尝试指针时按任务的分配账号）。"""
    return int(conn.execute(
        """
        SELECT count(*) AS n FROM procure.purchase_orders o
        LEFT JOIN procure.purchase_attempts a ON a.id = o.current_attempt_id AND a.workspace_id = o.workspace_id
        WHERE o.workspace_id = %s AND o.status = 'result_unknown'
          AND COALESCE(a.account_id, o.assigned_account_id) = %s
        """,
        (workspace_id, account_id),
    ).fetchone()["n"])


def _shape(conn, ws: int, row: dict[str, Any], *, simulated: bool, default_days: int) -> dict[str, Any]:
    from services import devices        # 延迟导入：devices 也导入本模块

    now = clock.now()
    # 所在浏览器：本工作区未移除、此刻上报的登录买家号就是这个账号的 ID 的浏览器（已归档的账号不列）
    devs = []
    if row["amazon_customer_id"] and row["archived_at"] is None:
        devs = conn.execute(
            "SELECT id, name, last_seen_at FROM procure.devices "
            "WHERE workspace_id = %s AND login_customer_id = %s AND hidden_at IS NULL ORDER BY name, id",
            (ws, row["amazon_customer_id"]),
        ).fetchall()
    run = conn.execute(
        """
        SELECT r.id, r.device_id, d.name AS device_name, r.started_at
        FROM procure.execution_runs r
        JOIN procure.devices d ON d.id = r.device_id AND d.workspace_id = r.workspace_id
        WHERE r.workspace_id = %s AND r.account_id = %s AND r.ended_at IS NULL
        ORDER BY r.started_at DESC LIMIT 1
        """,
        (ws, row["id"]),
    ).fetchone()
    return {
        "id": row["id"], "name": row["name"], "amazon_customer_id": row["amazon_customer_id"], "site": row["site"],
        "min_amount": _m(row["min_amount"]), "max_amount": _m(row["max_amount"]), "daily_limit": row["daily_limit"],
        "priority": row["priority"], "max_delivery_days": row["max_delivery_days"],
        "effective_max_delivery_days": (row["max_delivery_days"] if row["max_delivery_days"] is not None
                                        else default_days),
        "purchase_enabled": row["purchase_enabled"], "sync_enabled": row["sync_enabled"],
        "execution_paused": row["execution_paused"], "pause_reason": row["pause_reason"],
        "paused_at": _iso(row["paused_at"]), "pause_cause": pause_cause(conn, ws, row),
        "open_result_unknown": open_result_unknown(conn, ws, row["id"]), "purchase_mode": row["purchase_mode"],
        "schedule_times": list(row["schedule_times"] or []),
        "expected_card_last4": row["expected_card_last4"], "notes": row["notes"],
        "quota_today": quota.summary(conn, row, clock.bj_today(), simulated),
        "browsers": [devices.browser_brief(d, now) for d in devs],
        "active_run": ({"id": run["id"], "device_id": run["device_id"], "device_name": run["device_name"],
                        "started_at": _iso(run["started_at"])} if run else None),
        "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
        "archived_at": _iso(row["archived_at"]),
    }


def get(conn, workspace_id: int, account_id: int) -> dict[str, Any]:
    """输入：工作区、账号 id → 输出：契约 §10 账号形状（含 quota_today、browsers、active_run、pause_cause、
    open_result_unknown）。browsers = [{id, name, online, last_seen_at}]：此刻登录着这个买家号的浏览器。"""
    row = get_row(conn, workspace_id, account_id)
    st = settings_svc.get_row(conn, workspace_id)
    return _shape(conn, workspace_id, row, simulated=st["simulation_only"],
                  default_days=st["default_max_delivery_days"])


def list_accounts(conn, workspace_id: int, include_archived: bool = False) -> list[dict[str, Any]]:
    """输入：工作区、是否含已归档 → 输出：账号列表（按名称）。"""
    rows = conn.execute(
        "SELECT * FROM procure.buyer_accounts WHERE workspace_id = %s"
        + ("" if include_archived else " AND archived_at IS NULL") + " ORDER BY name, id",
        (workspace_id,),
    ).fetchall()
    st = settings_svc.get_row(conn, workspace_id)
    return [_shape(conn, workspace_id, r, simulated=st["simulation_only"],
                   default_days=st["default_max_delivery_days"]) for r in rows]


# ── 修改 ─────────────────────────────────────────────────────────────────────


def _actor(actor_user_id: int | None) -> tuple[str, int | None]:
    return ("user", actor_user_id) if actor_user_id is not None else ("system", None)


def _reallocate(conn, ws: int, actor_kind: str, actor_id: int | None) -> dict[str, Any]:
    from services import allocation

    return allocation.allocate(conn, ws, trigger="account_change", actor_kind=actor_kind, actor_id=actor_id)


def _lock_allocation(conn, ws: int, *, tasks: bool = False) -> None:
    """输入：连接、工作区、是否同时锁上分配可能动到的任务行 → 输出：无。取工作区分配锁（加锁顺序：账号行 → 本锁 →
    任务行；同一事务里可重入）；tasks=True 时再按 id 顺序锁齐任务行（allocation.lock_open_tasks，改名用）。"""
    from services import allocation

    allocation.lock_workspace(conn, ws)
    if tasks:
        allocation.lock_open_tasks(conn, ws)


def _busy_counts(conn, workspace_id: int, account_id: int) -> bool:
    """输入：工作区、账号 → 输出：账号是否有未结束的执行批次，或在途 / 结果待核实的采购尝试。"""
    busy = conn.execute(
        "SELECT (SELECT count(*) FROM procure.execution_runs WHERE workspace_id = %s AND account_id = %s "
        "        AND ended_at IS NULL) AS runs, "
        "       (SELECT count(*) FROM procure.purchase_attempts WHERE workspace_id = %s AND account_id = %s "
        "        AND status = ANY(%s)) AS attempts",
        (workspace_id, account_id, workspace_id, account_id, sorted(ATTEMPT_INFLIGHT | {"unknown"})),
    ).fetchone()
    return bool(busy["runs"] or busy["attempts"])


def _forget_ignored(conn, workspace_id: int, customer_id: str | None) -> None:
    """输入：工作区、刚写进某个账号的买家号 ID（可空）→ 输出：无（登记即不再忽略：删掉这个 ID 的忽略记录）。"""
    if customer_id:
        conn.execute("DELETE FROM procure.ignored_buyer_ids WHERE workspace_id = %s AND customer_id = %s",
                     (workspace_id, customer_id))


def create(conn, workspace_id: int, data: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、账号字段、操作人 → 输出：账号（契约 §10）。新建后一律立即重新分配（不论采购开关开没开：
    来源指定给它的任务要么分给它，要么把「还没有登记」的原因换成当前的原因）。"""
    vals = _validate(conn, workspace_id, data, None)
    now = clock.now()
    vals.update(workspace_id=workspace_id, created_at=now, updated_at=now)
    names = list(vals)
    row = _unique_write(
        conn,
        f"INSERT INTO procure.buyer_accounts ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) "
        "RETURNING *",
        list(vals.values()),
    ).fetchone()
    ak, aid = _actor(actor_user_id)
    _forget_ignored(conn, workspace_id, row["amazon_customer_id"])
    events.record(conn, workspace_id, "account", row["id"], "account.created", actor_kind=ak, actor_id=aid,
                  detail={k: v for k, v in vals.items() if k not in ("workspace_id", "created_at", "updated_at")})
    _reallocate(conn, workspace_id, ak, aid)
    return get(conn, workspace_id, row["id"])


def update(conn, workspace_id: int, account_id: int, patch: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、账号、要改的字段、操作人 → 输出：改后的账号。影响分配的字段（ALLOCATION_FIELDS，含名称与
    买家号 ID）变化后立即重新分配。

    修改或清空买家号 ID：账号有未结束批次、在途或结果待核实的尝试时 → CONFLICT（执行中的身份检查以它为基准，
    待核实的结果也要对着原来的买家号去核实）；允许时 'account.updated' 事件的 changes 里记下旧值。
    写入某个 ID 时同时清掉它的忽略记录。

    要重新分配时，先取工作区分配锁再改账号行；改名还要先锁齐分配可能动到的任务行：改名的 UPDATE 拿的是与外键
    KEY SHARE 冲突的行锁（名称在唯一约束里），拿着它再去等分配锁或任务行，正在把任务分给这个账号的分配、拿着任务行
    正要插入尝试的领取、人工改派就会和它互相等待。
    """
    row = get_row(conn, workspace_id, account_id, for_update=True)
    if row["archived_at"] is not None:
        raise validation("账号已归档，不能修改")
    vals = _validate(conn, workspace_id, patch, row)
    changes = {k: [row[k], v] for k, v in vals.items() if row[k] != v and not (
        isinstance(v, list) and list(row[k] or []) == v)}
    if not changes:
        return get(conn, workspace_id, account_id)
    if "amazon_customer_id" in changes and _busy_counts(conn, workspace_id, account_id):
        raise conflict("账号正在执行或有结果待核实的采购，处理完后才能修改买家号 ID")
    reallocate = bool(ALLOCATION_FIELDS & set(changes))
    if reallocate:
        _lock_allocation(conn, workspace_id, tasks="name" in changes)
    sets = ", ".join(f"{k} = %s" for k in changes)
    _unique_write(
        conn,
        f"UPDATE procure.buyer_accounts SET {sets}, updated_at = %s WHERE id = %s AND workspace_id = %s",
        [*(v[1] for v in changes.values()), clock.now(), account_id, workspace_id],
    )
    if "amazon_customer_id" in changes:
        _forget_ignored(conn, workspace_id, changes["amazon_customer_id"][1])
    ak, aid = _actor(actor_user_id)
    events.record(conn, workspace_id, "account", account_id, "account.updated", actor_kind=ak, actor_id=aid,
                  detail={"changes": changes})
    if reallocate:
        _reallocate(conn, workspace_id, ak, aid)
    return get(conn, workspace_id, account_id)


def pause(conn, workspace_id: int, account_id: int, reason: str | None, *, actor_kind: str,
          actor_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、账号、暂停原因、操作者（user / device / system）→ 输出：账号。

    已暂停时保留最初的暂停时间、更新原因。不重新分配；正在进行的批次由执行模块按 execution_paused 停止。
    """
    row = get_row(conn, workspace_id, account_id, for_update=True)
    text = (reason or "").strip() if isinstance(reason, str) else ""
    text = text[:500] or "人工暂停"
    now = clock.now()
    conn.execute(
        "UPDATE procure.buyer_accounts SET execution_paused = true, pause_reason = %s, "
        "paused_at = COALESCE(paused_at, %s), updated_at = %s WHERE id = %s AND workspace_id = %s",
        (text, now, now, account_id, workspace_id),
    )
    events.record(conn, workspace_id, "account", account_id, "account.paused", actor_kind=actor_kind,
                  actor_id=actor_id, detail={"reason": text, "was_paused": row["execution_paused"]})
    return get(conn, workspace_id, account_id)


def resume(conn, workspace_id: int, account_id: int, *, actor_kind: str, actor_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、账号、操作者 → 输出：账号（恢复执行并立即重新分配）。

    「结果待核实」的任务本身不会因恢复而被再次领取（它不在 assigned 状态）；事件里记下当时仍待核实的数量。
    """
    row = get_row(conn, workspace_id, account_id, for_update=True)
    if row["archived_at"] is not None:
        raise validation("账号已归档，不能恢复执行")
    pending = conn.execute(
        "SELECT count(*) AS n FROM procure.purchase_attempts WHERE workspace_id = %s AND account_id = %s "
        "AND status = 'unknown'",
        (workspace_id, account_id),
    ).fetchone()["n"]
    if not row["execution_paused"]:
        return get(conn, workspace_id, account_id)
    conn.execute(
        "UPDATE procure.buyer_accounts SET execution_paused = false, pause_reason = NULL, paused_at = NULL, "
        "updated_at = %s WHERE id = %s AND workspace_id = %s",
        (clock.now(), account_id, workspace_id),
    )
    events.record(conn, workspace_id, "account", account_id, "account.resumed", actor_kind=actor_kind,
                  actor_id=actor_id, detail={"previous_reason": row["pause_reason"], "unknown_attempts": pending})
    _reallocate(conn, workspace_id, actor_kind, actor_id)
    return get(conn, workspace_id, account_id)


def archive(conn, workspace_id: int, account_id: int, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、账号、操作人 → 输出：账号（已归档）。

    有未结束的执行批次、在途或待核实的尝试时拒绝；已分配的开始前任务解除分配后重新分配。
    加锁顺序：账号行 → 工作区分配锁 → 它的任务行（先取分配锁再锁任务行，不与同时进行的分配互相等待）。
    """
    row = get_row(conn, workspace_id, account_id, for_update=True)
    if row["archived_at"] is not None:
        return get(conn, workspace_id, account_id)
    if _busy_counts(conn, workspace_id, account_id):
        raise conflict("账号正在执行或有待核实的采购，不能归档")
    from services import orders

    _lock_allocation(conn, workspace_id)
    ak, aid = _actor(actor_user_id)
    for o in conn.execute(
        "SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND assigned_account_id = %s "
        "AND status = 'assigned' ORDER BY id FOR UPDATE",
        (workspace_id, account_id),
    ).fetchall():
        orders.transition(conn, o, "queued", reason="原分配账号已归档", actor_kind=ak, actor_id=aid,
                          event_detail={"action": "account_archived"})
    now = clock.now()
    conn.execute(
        "UPDATE procure.buyer_accounts SET archived_at = %s, purchase_enabled = false, updated_at = %s "
        "WHERE id = %s AND workspace_id = %s",
        (now, now, account_id, workspace_id),
    )
    events.record(conn, workspace_id, "account", account_id, "account.archived", actor_kind=ak, actor_id=aid)
    _reallocate(conn, workspace_id, ak, aid)
    return get(conn, workspace_id, account_id)


# ── 待登记的买家号 ────────────────────────────────────────────────────────────


def _customer_id_arg(customer_id: Any) -> str:
    """输入：接口传来的买家号 ID → 输出：去空白、大写后的 ID；形状不对 → VALIDATION。"""
    s = "".join(customer_id.split()).upper() if isinstance(customer_id, str) else ""
    if not s or len(s) > 64 or not CUSTOMER_ID_RE.match(s):
        raise validation("买家号 ID 格式不对（应以 A 开头，至少 9 位字母或数字）", field="customer_id")
    return s


def _name_hints(conn, workspace_id: int, customer_ids: list[str]) -> dict[str, str]:
    """输入：工作区、一组买家号 ID → 输出：{买家号 ID: 来源数据（飞书 / 文件）里同一「买家号 ID」最近出现的「买家号」}；
    查不到的 ID 不在结果里。只读现成的来源关联（order_sources.normalized），一次查完。以 # 开头的值（表格错误值）不算。"""
    if not customer_ids:
        return {}
    rows = conn.execute(
        """
        SELECT DISTINCT ON (cid) cid, name FROM (
            SELECT upper(btrim(normalized->>'external_buyer_customer_id')) AS cid,
                   btrim(normalized->>'external_buyer_account') AS name, last_seen_at, id
            FROM procure.order_sources
            WHERE workspace_id = %s AND upper(btrim(normalized->>'external_buyer_customer_id')) = ANY(%s)
              AND btrim(COALESCE(normalized->>'external_buyer_account', '')) <> ''
              AND left(btrim(normalized->>'external_buyer_account'), 1) <> '#'
        ) s ORDER BY cid, last_seen_at DESC, id DESC
        """,
        (workspace_id, customer_ids),
    ).fetchall()
    return {r["cid"]: r["name"][:64] for r in rows}


#: 「没有任何未归档账号登记这个买家号 ID」的条件（{ws}、{cid} 处填工作区与买家号 ID 的 SQL 表达式）
_UNREGISTERED = ("NOT EXISTS (SELECT 1 FROM procure.buyer_accounts a WHERE a.workspace_id = {ws} "
                 "AND a.amazon_customer_id = {cid} AND a.archived_at IS NULL)")


def _designated_ids(conn, workspace_id: int) -> dict[str, dict[str, Any]]:
    """输入：工作区 → 输出：{买家号 ID: {orders: 指定它的未采购任务数, name: 这些任务最近写的买家号 | None}}。

    只看未采购的任务（状态不在已采购 / 已关闭 / 已取消）上形状合格、且没有任何未归档账号登记的 designated_customer_id。
    """
    rows = conn.execute(
        f"""
        SELECT o.designated_customer_id AS cid, count(*) AS n,
               (array_agg(o.designated_buyer_name ORDER BY o.updated_at DESC, o.id DESC)
                    FILTER (WHERE o.designated_buyer_name IS NOT NULL
                            AND left(o.designated_buyer_name, 1) <> '#'))[1] AS name
        FROM procure.purchase_orders o
        WHERE o.workspace_id = %s AND o.status <> ALL(%s) AND o.designated_customer_id ~ '^A[0-9A-Z]{{8,}}$'
          AND {_UNREGISTERED.format(ws="o.workspace_id", cid="o.designated_customer_id")}
        GROUP BY o.designated_customer_id
        """,
        (workspace_id, list(_DESIGNATION_DONE_STATUSES)),
    ).fetchall()
    return {r["cid"]: {"orders": int(r["n"]), "name": r["name"]} for r in rows}


def discovered(conn, workspace_id: int, include_ignored: bool = False) -> list[dict[str, Any]]:
    """输入：工作区、是否含已忽略的 → 输出：待登记的买家号（按最近上报时间倒序，没有浏览器上报的排在后面）：
    [{customer_id, ignored, browsers: [{id, name, online, last_seen_at}], last_reported_at, name_hint, source_orders}]。

    = 没有被任何未归档账号登记的买家号 ID，来自两处的并集，按 ID 聚合：
    ① 本工作区未移除的浏览器此刻上报的登录买家号 ID；
    ② 来源指定：未采购的任务（状态不在已采购 / 已关闭 / 已取消）上形状合格的 designated_customer_id。
    source_orders = 指定它的未采购任务数（0 = 只来自浏览器）；只来自来源的项 browsers 为空、last_reported_at 为 None。
    name_hint 给「登记」对话框预填账号名：优先取这些任务最近写的买家号，否则来源数据里同一买家号 ID 最近出现的
    买家号，查不到为 None。include_ignored=True 时另外列出忽略名单里的**全部** ID（不要求此刻有浏览器登录着它，
    否则被忽略的 ID 无处恢复）。
    """
    from services import devices        # 延迟导入：devices 也导入本模块

    rows = conn.execute(
        f"""
        SELECT d.id, d.name, d.last_seen_at, d.identity_at, d.login_customer_id AS customer_id
        FROM procure.devices d
        WHERE d.workspace_id = %s AND d.hidden_at IS NULL AND d.login_customer_id IS NOT NULL
          AND {_UNREGISTERED.format(ws="d.workspace_id", cid="d.login_customer_id")}
        ORDER BY d.name, d.id
        """,
        (workspace_id,),
    ).fetchall()
    ignored = {r["customer_id"] for r in conn.execute(
        f"""
        SELECT ig.customer_id FROM procure.ignored_buyer_ids ig
        WHERE ig.workspace_id = %s AND {_UNREGISTERED.format(ws="ig.workspace_id", cid="ig.customer_id")}
        """,
        (workspace_id,),
    ).fetchall()}
    designated = _designated_ids(conn, workspace_id)
    now = clock.now()
    grouped: dict[str, dict[str, Any]] = {}

    def group(cid: str) -> dict[str, Any] | None:
        if cid in ignored and not include_ignored:
            return None
        return grouped.setdefault(cid, {"customer_id": cid, "ignored": cid in ignored, "browsers": [], "at": None})

    for r in rows:
        g = group(r["customer_id"])
        if g is None:
            continue
        g["browsers"].append(devices.browser_brief(r, now))
        if r["identity_at"] is not None and (g["at"] is None or r["identity_at"] > g["at"]):
            g["at"] = r["identity_at"]
    for cid in designated:
        group(cid)
    if include_ignored:
        for cid in ignored:
            group(cid)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    items = sorted(grouped.values(), key=lambda g: g["customer_id"])
    items.sort(key=lambda g: g["at"] or floor, reverse=True)
    hints = _name_hints(conn, workspace_id, [g["customer_id"] for g in items])
    out = []
    for g in items:
        d = designated.get(g["customer_id"]) or {"orders": 0, "name": None}
        out.append({"customer_id": g["customer_id"], "ignored": g["ignored"], "browsers": g["browsers"],
                    "last_reported_at": _iso(g["at"]),
                    "name_hint": (d["name"][:64] if d["name"] else None) or hints.get(g["customer_id"]),
                    "source_orders": d["orders"]})
    return out


def discovered_count(conn, workspace_id: int) -> int:
    """输入：工作区 → 输出：未忽略的待登记买家号个数（与 discovered(include_ignored=False) 的条数相同；概览用）：
    浏览器上报的与来源指定的未登记买家号 ID 的并集。"""
    return int(conn.execute(
        f"""
        SELECT count(*) AS n FROM (
            SELECT d.login_customer_id AS cid FROM procure.devices d
            WHERE d.workspace_id = %s AND d.hidden_at IS NULL AND d.login_customer_id IS NOT NULL
            UNION
            SELECT o.designated_customer_id FROM procure.purchase_orders o
            WHERE o.workspace_id = %s AND o.status <> ALL(%s) AND o.designated_customer_id ~ '^A[0-9A-Z]{{8,}}$'
        ) x
        WHERE {_UNREGISTERED.format(ws="%s", cid="x.cid")}
          AND NOT EXISTS (SELECT 1 FROM procure.ignored_buyer_ids ig
                          WHERE ig.workspace_id = %s AND ig.customer_id = x.cid)
        """,
        (workspace_id, workspace_id, list(_DESIGNATION_DONE_STATUSES), workspace_id, workspace_id),
    ).fetchone()["n"])


def _queued_designating(conn, workspace_id: int, customer_id: str) -> bool:
    """输入：工作区、买家号 ID → 输出：有没有待分配（queued）的任务由来源指定了这个 ID。"""
    return conn.execute(
        "SELECT 1 FROM procure.purchase_orders WHERE workspace_id = %s AND designated_customer_id = %s "
        "AND status = 'queued' LIMIT 1",
        (workspace_id, customer_id),
    ).fetchone() is not None


def ignore_buyer_id(conn, workspace_id: int, customer_id: Any, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、买家号 ID、操作人 → 输出：{customer_id, ignored: true}。幂等。

    忽略后这个 ID 不再出现在待登记列表里，登录着它的浏览器显示「已忽略」、分不到任务。ID 形状不对 → VALIDATION；
    已登记在未归档账号上的 ID 不能忽略（VALIDATION：要停用它应关掉账号的采购开关或归档账号）。
    新忽略时写 'account.buyer_id_ignored' 事件；这时如果有待分配的任务由来源指定了这个 ID，立即重新分配一次
    （trigger='account_change'），这些任务的未分配原因随即注明「已被忽略」——重新分配只改写原因，不产生订单事件。
    重复忽略、没有任务指定的 ID 不触发分配。
    """
    cid = _customer_id_arg(customer_id)
    owner = find_by_customer_id(conn, workspace_id, cid)
    if owner is not None:
        raise validation(f"买家号 ID {cid} 已登记在账号 {owner['name']}，不能忽略", field="customer_id")
    row = conn.execute(
        "INSERT INTO procure.ignored_buyer_ids (workspace_id, customer_id, created_by, created_at) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (workspace_id, customer_id) DO NOTHING RETURNING customer_id",
        (workspace_id, cid, actor_user_id, clock.now()),
    ).fetchone()
    if row is not None:
        ak, aid = _actor(actor_user_id)
        events.record(conn, workspace_id, "account", None, "account.buyer_id_ignored", actor_kind=ak, actor_id=aid,
                      detail={"customer_id": cid})
        if _queued_designating(conn, workspace_id, cid):
            _reallocate(conn, workspace_id, ak, aid)
    return {"customer_id": cid, "ignored": True}


def unignore_buyer_id(conn, workspace_id: int, customer_id: Any, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、买家号 ID、操作人 → 输出：{customer_id, ignored: false}。幂等（没忽略过也成功）。

    确实删掉了忽略记录时写 'account.buyer_id_unignored' 事件；这时如果有待分配的任务由来源指定了这个 ID，立即重新
    分配一次（同 ignore_buyer_id：原因里不再写「已被忽略」）。没忽略过、没有任务指定的 ID 不触发分配。
    """
    cid = _customer_id_arg(customer_id)
    row = conn.execute(
        "DELETE FROM procure.ignored_buyer_ids WHERE workspace_id = %s AND customer_id = %s RETURNING customer_id",
        (workspace_id, cid),
    ).fetchone()
    if row is not None:
        ak, aid = _actor(actor_user_id)
        events.record(conn, workspace_id, "account", None, "account.buyer_id_unignored", actor_kind=ak, actor_id=aid,
                      detail={"customer_id": cid})
        if _queued_designating(conn, workspace_id, cid):
            _reallocate(conn, workspace_id, ak, aid)
    return {"customer_id": cid, "ignored": False}
