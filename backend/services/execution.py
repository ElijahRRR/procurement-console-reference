"""执行协议（契约 §5）：账号执行批次（租约）、领取、进度、服务端护栏、提交登记、等待验证、结果回报、
结束批次、租约清扫。插件请求的 HTTP 外壳在 server/routes（插件路由），这里只有业务判断。

执行权与互斥（方案 v2 §2，全部由数据库唯一索引兜底）：

- 一个账号同时只有一个未结束的批次（execution_runs_one_active_per_account）。开始批次前先查一遍给出
  「谁在执行、从何时开始」，真正的互斥靠插入时的唯一索引；冲突在 SAVEPOINT 里捕获，调用方事务仍可用。
- 一笔任务、一个账号同时只有一个在途尝试（claimed / checking / guard_passed / submitting / verifying）。
- 每个调用都核对批次 / 尝试属于**这台设备、这个工作区**（否则 404，不泄露存在性），并且批次仍有效
  （未结束且租约未过期，否则 409 LEASE_LOST）。已结束尝试的重复结果上报例外：内容相同就原样返回。

加锁顺序（避免死锁，与 services/orders 的「任务 → 尝试」一致）：批次 → 任务 → 尝试 → 账号。

提交登记是分界线（铁律 9）：

- submitted_at 为空：可以中止（aborted）、可重试（PAGE_LOAD_FAILED 自动重试一次）、可转异常；
  任务回到已分配时**保留分配**。
- submitted_at 非空：任何非成功结局（失败、结果不明、断线、租约过期、批次被结束、设备被撤销）一律
  尝试 → unknown、任务 → result_unknown、账号暂停执行（原因「结果待核实：<平台订单号>」），绝不自动重买。
- 插件说「结果不明」，或未登记提交却上报成功、成功却没有合格的亚马逊订单号：同样按结果待核实处理
  （宁可人工核实，也不让任务回到可领取状态）。

账号被本模块暂停时，除 accounts.pause 写的 'account.paused' 外，另写一条 PAUSE_MARKER_KIND 事件
{cause: result_unknown | account_error, simulated, attempt_id, code}，供「清除模拟结果」判断哪些账号
只是被模拟的待核实结果暂停的（services/simulation）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from registry import settings
from services import accounts, clock, devices, events, guard, orders, quota, settings_svc, writeback
from services.errors import DomainError, conflict, not_found, validation
from services.models import ATTEMPT_INFLIGHT, ERROR_CODES, money

TRIGGERS = ("manual", "scheduled")
OUTCOMES = ("succeeded", "failed", "unknown")
FINAL_STATUSES = ("succeeded", "failed", "unknown", "aborted")
AMOUNT_KEYS = ("subtotal", "shipping", "tax", "total_before_tax", "total")
PAUSE_MARKER_KIND = "account.paused_by_execution"
SIM_PREFIX = "SIM-"

_INFLIGHT = sorted(ATTEMPT_INFLIGHT)
_PRE_SUBMIT = ("claimed", "checking", "guard_passed")

ATTEMPT_STATUS_ZH = {
    "claimed": "已领取", "checking": "结算中", "guard_passed": "护栏已放行", "submitting": "已登记提交",
    "verifying": "等待验证", "succeeded": "成功", "failed": "失败", "unknown": "结果待核实", "aborted": "已中止",
}
END_REASON_ZH = {
    "finished": "插件结束了批次", "lease_expired": "执行权超时（租约过期）", "device_revoked": "设备授权已撤销",
    "account_paused": "账号已暂停执行", "simulation_reset": "已清除模拟结果",
}

__all__ = [
    "PAUSE_MARKER_KIND", "end_run", "finish_run", "mark_submitting", "mark_verifying", "next_attempt", "progress",
    "report_result", "run_heartbeat", "start_run", "submit_guard", "sweep",
]


# ── 小工具 ────────────────────────────────────────────────────────────────────


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _m(v: Any) -> str | None:
    return None if v is None else f"{Decimal(v):.2f}"


def _usd(v: Any) -> str:
    return "未知金额" if v is None else f"${Decimal(v):.2f}"


def _ttl() -> int:
    return int(settings.get().lease_ttl_s)


def _id(v: Any, what: str) -> int:
    if isinstance(v, bool):
        raise not_found(what)
    try:
        return int(v)
    except (TypeError, ValueError):
        raise not_found(what) from None


def _text(v: Any, n: int) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] or None


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _small(v: Any, limit: int = 4000) -> Any:
    """输入：插件送来的任意细节 → 输出：可入库的 JSON（过大时只留截断标记）。"""
    j = _jsonable(v)
    try:
        if len(json.dumps(j, ensure_ascii=False)) > limit:
            return {"truncated": True}
    except (TypeError, ValueError):
        return {"unserializable": True}
    return j


def _actor_of(dev: dict[str, Any]) -> tuple[str, int]:
    return "device", dev["id"]


def _lease_lost(message: str, **detail: Any) -> DomainError:
    return DomainError("LEASE_LOST", message, 409, detail)


def _account_row(conn, workspace_id: int, account_id: int) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM procure.buyer_accounts WHERE id = %s AND workspace_id = %s",
                        (account_id, workspace_id)).fetchone()


def _bump(conn, run: dict[str, Any] | None, key: str) -> None:
    """输入：批次、计数键 → 输出：无（execution_runs.stats[key] += 1）。"""
    if run is None:
        return
    conn.execute(
        "UPDATE procure.execution_runs SET stats = jsonb_set(stats, %s, to_jsonb(COALESCE((stats->>%s)::int, 0) + 1)) "
        "WHERE id = %s AND workspace_id = %s",
        ([key], key, run["id"], run["workspace_id"]),
    )


def _set_attempt(conn, att: dict[str, Any], **cols: Any) -> dict[str, Any]:
    """输入：尝试行、要写的列 → 输出：更新后的尝试行（jsonb 列自动包装）。"""
    parts, params = [], []
    for k, v in cols.items():
        parts.append(f"{k} = %s")
        params.append(Jsonb(_jsonable(v)) if k in ("task_snapshot", "guard_input", "guard_verdict") and v is not None
                      else v)
    return conn.execute(
        f"UPDATE procure.purchase_attempts SET {', '.join(parts)} WHERE id = %s AND workspace_id = %s RETURNING *",
        [*params, att["id"], att["workspace_id"]],
    ).fetchone()


# ── 取批次 / 尝试（带归属与有效性校验）──────────────────────────────────────────


def _run_for(conn, dev: dict[str, Any], run_id: Any) -> dict[str, Any]:
    """输入：设备、批次 id → 输出：加锁的批次行；不属于这台设备 / 工作区 → NOT_FOUND。"""
    rid = _id(run_id, "执行批次")
    run = conn.execute(
        "SELECT * FROM procure.execution_runs WHERE id = %s AND workspace_id = %s AND device_id = %s FOR UPDATE",
        (rid, dev["workspace_id"], dev["id"]),
    ).fetchone()
    if run is None:
        raise not_found("执行批次")
    return run


def _require_active(run: dict[str, Any] | None) -> None:
    """输入：批次行 → 输出：无；已结束或租约已过期 → LEASE_LOST（409）。"""
    if run is None:
        raise _lease_lost("这次尝试不属于任何有效的执行批次")
    if run["ended_at"] is not None:
        why = END_REASON_ZH.get(run["end_reason"] or "", run["end_reason"] or "已结束")
        raise _lease_lost(f"执行批次已结束（{why}），请重新开始批次", run_id=run["id"], end_reason=run["end_reason"])
    if run["lease_expires_at"] <= clock.now():
        raise _lease_lost("执行权已超时（租约过期），请重新开始批次", run_id=run["id"],
                          lease_expires_at=_iso(run["lease_expires_at"]))


def _attempt_for(conn, dev: dict[str, Any], attempt_id: Any) -> tuple[dict, dict | None, dict]:
    """输入：设备、尝试 id → 输出：(尝试, 批次, 任务)，按「批次 → 任务 → 尝试」加锁；
    尝试不属于这台设备 / 工作区 → NOT_FOUND。"""
    aid = _id(attempt_id, "采购尝试")
    ws = dev["workspace_id"]
    peek = conn.execute(
        "SELECT run_id, purchase_order_id FROM procure.purchase_attempts WHERE id = %s AND workspace_id = %s "
        "AND device_id = %s",
        (aid, ws, dev["id"]),
    ).fetchone()
    if peek is None:
        raise not_found("采购尝试")
    run = None
    if peek["run_id"] is not None:
        run = conn.execute("SELECT * FROM procure.execution_runs WHERE id = %s AND workspace_id = %s FOR UPDATE",
                           (peek["run_id"], ws)).fetchone()
    order = orders.get_row(conn, ws, peek["purchase_order_id"], for_update=True)
    att = conn.execute("SELECT * FROM procure.purchase_attempts WHERE id = %s AND workspace_id = %s FOR UPDATE",
                       (aid, ws)).fetchone()
    return att, run, order


def _holder(conn, workspace_id: int, account_id: int) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT r.id, r.device_id, d.name AS device_name, r.started_at
        FROM procure.execution_runs r
        JOIN procure.devices d ON d.id = r.device_id AND d.workspace_id = r.workspace_id
        WHERE r.workspace_id = %s AND r.account_id = %s AND r.ended_at IS NULL
        ORDER BY r.started_at, r.id LIMIT 1
        """,
        (workspace_id, account_id),
    ).fetchone()


def _busy(holder: dict[str, Any] | None, account_name: str) -> DomainError:
    if holder is None:
        return DomainError("ACCOUNT_BUSY", f"账号 {account_name} 正在别处执行，请稍后再试", 409,
                           {"holder_device_id": None, "holder_device_name": None, "since": None})
    return DomainError(
        "ACCOUNT_BUSY",
        f"账号 {account_name} 正由设备「{holder['device_name']}」执行"
        f"（北京时间 {holder['started_at'].astimezone(clock.BJ_TZ):%m-%d %H:%M} 起），同一账号同时只能有一个执行者",
        409,
        {"holder_device_id": holder["device_id"], "holder_device_name": holder["device_name"],
         "since": _iso(holder["started_at"])},
    )


def _identity_problem(dev: dict[str, Any], acc: dict[str, Any]) -> tuple[str, str] | None:
    """输入：设备行、账号行 → 输出：(有效身份状态, 中文说明)；身份为 match 时 None（不看新鲜度）。"""
    status = devices.effective_identity_status(dev, acc)
    if status == "match":
        return None
    ident = dev.get("identity") if isinstance(dev.get("identity"), dict) else {}
    if status == "mismatch":
        msg = (f"浏览器登录的买家账号（{ident.get('customer_id') or '未识别'}）与绑定账号 {acc['name']}"
               f"（{acc['amazon_customer_id']}）不符，拒绝领取")
    elif status == "signed_out":
        msg = f"浏览器没有登录亚马逊买家账号，请先登录绑定账号 {acc['name']}"
    elif not acc.get("amazon_customer_id"):
        msg = f"账号 {acc['name']} 还没有登记买家号 ID，请在控制台采用插件识别的 ID 后再开始"
    else:
        msg = "插件还没有识别到浏览器登录的买家账号，请打开亚马逊页面后重试"
    return status, msg


# ── 账号暂停（附模拟标记）────────────────────────────────────────────────────


def _pause_account(conn, workspace_id: int, account_id: int | None, reason: str, *, cause: str, att: dict[str, Any],
                   code: str | None, actor_kind: str, actor_id: int | None) -> None:
    """输入：账号、原因、起因（result_unknown / account_error）、触发的尝试 → 输出：无（暂停执行并写标记事件）。"""
    if account_id is None:
        return
    accounts.pause(conn, workspace_id, account_id, reason, actor_kind=actor_kind, actor_id=actor_id)
    events.record(conn, workspace_id, "account", account_id, PAUSE_MARKER_KIND, actor_kind=actor_kind,
                  actor_id=actor_id, order_id=att["purchase_order_id"],
                  detail={"cause": cause, "simulated": bool(att["simulated"]), "attempt_id": att["id"],
                          "code": code, "reason": reason})


# ── 任务落地（尝试结束后任务去哪）─────────────────────────────────────────────


def _owns(order: dict[str, Any], att: dict[str, Any]) -> bool:
    return order["status"] == "executing" and order["current_attempt_id"] == att["id"]


def _cancel_reason(order: dict[str, Any]) -> str:
    note = order.get("upstream_cancel_note")
    return f"上游取消（{note}），采购已中止（未提交）" if note else "上游取消，采购已中止（未提交）"


def _release(conn, order: dict[str, Any], att: dict[str, Any], reason: str, *, actor_kind: str,
             actor_id: int | None, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    """输入：任务、未登记提交就结束的尝试、原因 → 输出：任务行。上游已取消 → cancelled；否则回 assigned（保留分配）。"""
    upd = {"current_attempt_id": None, **(updates or {})}
    detail = {"attempt_id": att["id"]}
    if not _owns(order, att):
        if order["current_attempt_id"] == att["id"]:
            return orders.transition(conn, order, order["status"], actor_kind=actor_kind, actor_id=actor_id,
                                     updates={"current_attempt_id": None})
        return order
    if order["upstream_cancelled"]:
        return orders.transition(conn, order, "cancelled", reason=_cancel_reason(order), actor_kind=actor_kind,
                                 actor_id=actor_id, updates=upd, event_detail=detail)
    return orders.transition(conn, order, "assigned", reason=reason, actor_kind=actor_kind, actor_id=actor_id,
                             updates=upd, event_detail=detail)


def _to_exception(conn, order: dict[str, Any], att: dict[str, Any], reason: str, *, actor_kind: str,
                  actor_id: int | None) -> dict[str, Any]:
    """输入：任务、未登记提交就失败的尝试、异常原因 → 输出：任务行（上游已取消 → cancelled，否则 exception）。"""
    if not _owns(order, att):
        return order
    if order["upstream_cancelled"]:
        return _release(conn, order, att, reason, actor_kind=actor_kind, actor_id=actor_id)
    return orders.transition(conn, order, "exception", reason=reason, actor_kind=actor_kind, actor_id=actor_id,
                             updates={"current_attempt_id": None}, event_detail={"attempt_id": att["id"]})


def _to_unknown(conn, order: dict[str, Any], att: dict[str, Any], reason: str, *, actor_kind: str,
                actor_id: int | None) -> dict[str, Any]:
    """输入：任务、结果不明的尝试、原因 → 输出：任务行（→ result_unknown；current_attempt_id 指向待核实的尝试）。"""
    if order["status"] == "executing":
        return orders.transition(conn, order, "result_unknown", reason=reason, actor_kind=actor_kind,
                                 actor_id=actor_id, updates={"current_attempt_id": att["id"]},
                                 event_detail={"attempt_id": att["id"]})
    if order["status"] == "result_unknown":
        # 例如执行中收到上游取消（已登记提交）：保留取消说明，补上这次的原因
        existing = order["status_reason"] or ""
        merged = existing if reason in existing else (f"{existing}；{reason}" if existing else reason)
        return orders.transition(conn, order, "result_unknown", reason=merged, actor_kind=actor_kind,
                                 actor_id=actor_id, updates={"current_attempt_id": att["id"]})
    events.record(conn, order["workspace_id"], "order", order["id"], "order.attempt_unknown_unexpected_status",
                  actor_kind=actor_kind, actor_id=actor_id,
                  detail={"attempt_id": att["id"], "status": order["status"], "reason": reason})
    return order


def _settle_inflight(conn, run: dict[str, Any] | None, att: dict[str, Any], order: dict[str, Any], cause: str, *,
                     actor_kind: str, actor_id: int | None) -> str:
    """输入：被迫结束的在途尝试（已锁）、它的任务（已锁）、原因 → 输出：尝试的新状态（aborted / unknown）。

    未登记提交 → aborted，任务回已分配（上游已取消则 cancelled）；已登记提交 → unknown，任务结果待核实，
    账号暂停执行。
    """
    ws = att["workspace_id"]
    now = clock.now()
    if att["submitted_at"] is None:
        att = _set_attempt(conn, att, status="aborted", error_detail=cause, finished_at=now)
        _release(conn, order, att, f"{cause}，未登记提交，任务保留待重新领取", actor_kind=actor_kind, actor_id=actor_id)
        events.record(conn, ws, "attempt", att["id"], "attempt.aborted", actor_kind=actor_kind, actor_id=actor_id,
                      order_id=order["id"], detail={"cause": cause, "run_id": att["run_id"]})
        _bump(conn, run, "aborted")
        return "aborted"
    att = _set_attempt(conn, att, status="unknown", error_detail=cause, finished_at=now)
    _to_unknown(conn, order, att, f"已登记提交但{cause}，需人工核实是否已下单", actor_kind=actor_kind,
                actor_id=actor_id)
    _pause_account(conn, ws, att["account_id"], f"结果待核实：{order['upstream_order_no']}", cause="result_unknown",
                   att=att, code=None, actor_kind=actor_kind, actor_id=actor_id)
    events.record(conn, ws, "attempt", att["id"], "attempt.unknown", actor_kind=actor_kind, actor_id=actor_id,
                  order_id=order["id"], detail={"cause": cause, "run_id": att["run_id"]})
    _bump(conn, run, "unknown")
    return "unknown"


def _settle_run_attempts(conn, run: dict[str, Any], cause: str, *, actor_kind: str,
                         actor_id: int | None) -> dict[str, int]:
    ws = run["workspace_id"]
    out = {"aborted": 0, "unknown": 0}
    peeks = conn.execute(
        "SELECT id, purchase_order_id FROM procure.purchase_attempts WHERE workspace_id = %s AND run_id = %s "
        "AND status = ANY(%s) ORDER BY id",
        (ws, run["id"], _INFLIGHT),
    ).fetchall()
    for p in peeks:
        order = orders.get_row(conn, ws, p["purchase_order_id"], for_update=True)
        att = conn.execute("SELECT * FROM procure.purchase_attempts WHERE id = %s AND workspace_id = %s FOR UPDATE",
                           (p["id"], ws)).fetchone()
        if att is None or att["status"] not in ATTEMPT_INFLIGHT:
            continue
        out[_settle_inflight(conn, run, att, order, cause, actor_kind=actor_kind, actor_id=actor_id)] += 1
    return out


def end_run(conn, run: dict[str, Any], end_reason: str, *, actor_kind: str = "system", actor_id: int | None = None,
            cause: str | None = None, settle: bool = True) -> dict[str, Any]:
    """输入：未结束的批次行（调用方已锁）、结束原因、操作者、是否按超时处理在途尝试 → 输出：
    {run_id, end_reason, aborted, unknown}。

    settle=True 时它的在途尝试按租约过期同样处理（见 _settle_inflight）；「清除模拟结果」传 settle=False，
    在途的模拟尝试由它统一清除。批次已结束 → 原样返回（不重复处理）。
    """
    ws = run["workspace_id"]
    cause = cause or END_REASON_ZH.get(end_reason, "执行批次已结束")
    counts = {"aborted": 0, "unknown": 0}
    if settle:
        counts = _settle_run_attempts(conn, run, cause, actor_kind=actor_kind, actor_id=actor_id)
    row = conn.execute(
        "UPDATE procure.execution_runs SET ended_at = %s, end_reason = %s WHERE id = %s AND workspace_id = %s "
        "AND ended_at IS NULL RETURNING id",
        (clock.now(), end_reason, run["id"], ws),
    ).fetchone()
    if row is not None:
        events.record(conn, ws, "run", run["id"], "run.ended", actor_kind=actor_kind, actor_id=actor_id,
                      detail={"end_reason": end_reason, "account_id": run["account_id"], "device_id": run["device_id"],
                              **counts})
    return {"run_id": run["id"], "end_reason": end_reason, **counts}


# ── 开始批次与批次心跳 ────────────────────────────────────────────────────────


def start_run(conn, device: dict[str, Any], *, trigger: Any, simulated: Any) -> dict[str, Any]:
    """输入：设备、触发方式（manual / scheduled）、是否模拟 → 输出：{run_id, lease_ttl_s, lease_expires_at}。

    依次要求：设备已绑定（NOT_BOUND 403）、账号未归档（NOT_BOUND 403）、身份 match 且在 identity_fresh_s 内上报过
    （IDENTITY_MISMATCH 403）、账号未暂停执行（ACCOUNT_PAUSED 403）、模拟批次（本阶段一律拒绝非模拟，
    REAL_PURCHASE_DISABLED 403）、账号没有未结束的批次（ACCOUNT_BUSY 409，detail 为持有者）。
    """
    dev = devices.require_active(conn, device)
    if trigger not in TRIGGERS:
        raise validation("触发方式只能是 manual 或 scheduled", field="trigger")
    if not isinstance(simulated, bool):
        raise validation("simulated 必须是 true 或 false", field="simulated")
    ws = dev["workspace_id"]
    if dev["bound_account_id"] is None:
        raise DomainError("NOT_BOUND", "设备还没有绑定买家账号，请先在控制台「账号与设备」里绑定", 403)
    acc = _account_row(conn, ws, dev["bound_account_id"])
    if acc is None or acc["archived_at"] is not None:
        raise DomainError("NOT_BOUND", "设备绑定的买家账号已归档，请在控制台重新绑定", 403)
    problem = _identity_problem(dev, acc)
    now = clock.now()
    fresh_s = int(settings.get().identity_fresh_s)
    if problem is not None:
        raise DomainError("IDENTITY_MISMATCH", problem[1], 403,
                          {"identity_status": problem[0], "identity_at": _iso(dev["identity_at"]), "stale": False})
    if dev["identity_at"] is None or (now - dev["identity_at"]).total_seconds() > fresh_s:
        raise DomainError(
            "IDENTITY_MISMATCH",
            f"插件超过 {max(1, fresh_s // 60)} 分钟没有上报登录身份，身份信息已过期，请打开或刷新亚马逊页面后重试",
            403, {"identity_status": "match", "identity_at": _iso(dev["identity_at"]), "stale": True},
        )
    if acc["execution_paused"]:
        raise DomainError("ACCOUNT_PAUSED", f"账号 {acc['name']} 已暂停执行：{acc['pause_reason'] or '人工暂停'}", 403,
                          {"pause_reason": acc["pause_reason"], "paused_at": _iso(acc["paused_at"])})
    sim_only = settings_svc.simulation_only(conn, ws)
    if not simulated:
        # 本阶段（第一个 PR）无论设置如何都拒绝非模拟批次（铁律 10）
        msg = "本阶段只允许模拟执行，服务端拒绝真实采购批次" if sim_only else "真实采购尚未开放，服务端拒绝真实采购批次"
        raise DomainError("REAL_PURCHASE_DISABLED", msg, 403)
    holder = _holder(conn, ws, acc["id"])
    if holder is not None:
        raise _busy(holder, acc["name"])
    ttl = _ttl()
    expires = now + timedelta(seconds=ttl)
    try:
        with conn.transaction():
            run = conn.execute(
                """
                INSERT INTO procure.execution_runs (workspace_id, account_id, device_id, trigger, simulated,
                                                    started_at, heartbeat_at, lease_expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
                """,
                (ws, acc["id"], dev["id"], trigger, simulated, now, now, expires),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise _busy(_holder(conn, ws, acc["id"]), acc["name"]) from None
    events.record(conn, ws, "run", run["id"], "run.started", actor_kind="device", actor_id=dev["id"],
                  detail={"account_id": acc["id"], "trigger": trigger, "simulated": simulated})
    return {"run_id": run["id"], "lease_ttl_s": ttl, "lease_expires_at": expires.isoformat()}


def _stop_reason(dev: dict[str, Any], acc: dict[str, Any] | None) -> str | None:
    if acc is None or acc["archived_at"] is not None:
        return "account_archived"
    if acc["execution_paused"]:
        return "account_paused"
    if dev["bound_account_id"] != acc["id"]:
        return "device_unbound"
    if devices.effective_identity_status(dev, acc) != "match":
        return "identity_mismatch"
    return None


def run_heartbeat(conn, device: dict[str, Any], run_id: Any) -> dict[str, Any]:
    """输入：设备、批次 id → 输出：{lease_expires_at, stop, stop_reason}（续租 lease_ttl_s 秒）。

    stop=true 的原因：account_paused / account_archived / identity_mismatch / device_unbound；租约照样续，
    让插件把手上这一笔做完再结束批次。批次已结束或租约已过期 → LEASE_LOST。
    """
    dev = devices.require_active(conn, device)
    run = _run_for(conn, dev, run_id)
    _require_active(run)
    now = clock.now()
    expires = now + timedelta(seconds=_ttl())
    conn.execute("UPDATE procure.execution_runs SET heartbeat_at = %s, lease_expires_at = %s "
                 "WHERE id = %s AND workspace_id = %s", (now, expires, run["id"], run["workspace_id"]))
    reason = _stop_reason(dev, _account_row(conn, run["workspace_id"], run["account_id"]))
    return {"lease_expires_at": expires.isoformat(), "stop": reason is not None, "stop_reason": reason}


# ── 领取 ──────────────────────────────────────────────────────────────────────


def _attempt_kind(conn, order: dict[str, Any]) -> str:
    """输入：任务行 → 输出：新尝试的种类。曾有成功尝试（亚马逊取消后同意重采才会再次领取）→ 'repurchase'；
    最近一次本系统尝试因 PAGE_LOAD_FAILED 失败（自动重试）→ 'retry'；否则 'auto'。"""
    rows = conn.execute(
        "SELECT kind, status, error_code FROM procure.purchase_attempts WHERE purchase_order_id = %s "
        "AND workspace_id = %s ORDER BY attempt_no DESC",
        (order["id"], order["workspace_id"]),
    ).fetchall()
    if any(r["status"] == "succeeded" for r in rows):
        return "repurchase"
    own = [r for r in rows if r["kind"] != "external"]
    if own and own[0]["status"] == "failed" and own[0]["error_code"] == "PAGE_LOAD_FAILED":
        return "retry"
    return "auto"


def _payload(conn, order: dict[str, Any], acc: dict[str, Any], att: dict[str, Any]) -> dict[str, Any]:
    """输入：任务、账号、新尝试 → 输出：下发给插件的尝试载荷（契约 §5 第 6 步；金额为字符串）。"""
    return {
        "attempt_id": att["id"],
        "attempt_no": att["attempt_no"],
        "kind": att["kind"],
        "order_id": order["id"],
        "upstream_order_no": order["upstream_order_no"],
        "asin": orders.asin_eff(order),
        "quantity": orders.quantity_eff(order),
        "unit_price": _m(order["unit_price"]),
        "subtotal": _m(orders.subtotal_eff(order)),
        "price_cap": _m(orders.price_cap_eff(order)),
        "max_delivery_days": accounts.effective_max_delivery_days(conn, order["workspace_id"], acc),
        "amount_range": {"min": _m(acc["min_amount"]), "max": _m(acc["max_amount"])},
        "recipient_name": order["recipient_name"],
        "address1": order["address1"],
        "address2": order["address2"],
        "city": order["city"],
        "state": order["state"],
        "postal_code": order["postal_code"],
        "phone": order["phone"],
        "country": order["country"],
        "district": order["district"],
        "expected_card_last4": acc["expected_card_last4"],
        "simulated": bool(att["simulated"]),
        "bj_date": _iso(att["bj_date"]),
        "suggested_purchase_date": _iso(order["suggested_purchase_date"]),
        "account": {"id": acc["id"], "name": acc["name"]},
    }


def _claim(conn, dev: dict[str, Any], run: dict[str, Any], acc: dict[str, Any], order: dict[str, Any],
           today: date) -> dict[str, Any]:
    ws = run["workspace_id"]
    now = clock.now()
    kind = _attempt_kind(conn, order)
    attempt_no = orders.next_attempt_no(conn, order["id"], workspace_id=ws)
    try:
        with conn.transaction():
            att = conn.execute(
                """
                INSERT INTO procure.purchase_attempts (workspace_id, purchase_order_id, attempt_no, kind, account_id,
                    device_id, run_id, simulated, status, bj_date, stage, claimed_at, heartbeat_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'claimed', %s, 'claimed', %s, %s) RETURNING *
                """,
                (ws, order["id"], attempt_no, kind, acc["id"], dev["id"], run["id"], run["simulated"], today, now, now),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise conflict("该账号或任务已有进行中的采购尝试", code="ATTEMPT_IN_FLIGHT") from None
    payload = _payload(conn, order, acc, att)
    _set_attempt(conn, att, task_snapshot=payload)
    label = "模拟采购中" if run["simulated"] else "采购中"
    orders.transition(conn, order, "executing", reason=f"{label}（{acc['name']}）", actor_kind="device",
                      actor_id=dev["id"], updates={"current_attempt_id": att["id"]},
                      event_detail={"attempt_id": att["id"], "run_id": run["id"], "simulated": run["simulated"]})
    events.record(conn, ws, "attempt", att["id"], "attempt.claimed", actor_kind="device", actor_id=dev["id"],
                  order_id=order["id"],
                  detail={"run_id": run["id"], "attempt_no": attempt_no, "kind": kind, "simulated": run["simulated"],
                          "bj_date": today})
    _bump(conn, run, "claimed")
    return payload


def next_attempt(conn, device: dict[str, Any], run_id: Any) -> dict[str, Any]:
    """输入：设备、批次 id → 输出：{attempt: 载荷 | None, reason}（契约 §3.3、§5 第 6 步）。

    本批次已有在途尝试 → ATTEMPT_IN_FLIGHT（409）。账号已暂停 / 已归档 / 身份不符 / 当天额度已满
    （used(账号, 北京今天, 本批次的模拟标记) ≥ 每日上限，在途、待核实、成功都占用）/ 账号在别处还有在途尝试
    时不领取，返回原因。领取条件：已分配给本账号、建议采购日期 ≤ 北京今天、人审通过、未上游取消、无冲突、
    金额仍在账号区间内；顺序：建议采购日期、下单时间、id；FOR UPDATE SKIP LOCKED。金额已不在区间的任务
    解除分配回待分配并跳过。
    """
    dev = devices.require_active(conn, device)
    run = _run_for(conn, dev, run_id)
    _require_active(run)
    ws = run["workspace_id"]
    mine = conn.execute(
        "SELECT id FROM procure.purchase_attempts WHERE workspace_id = %s AND run_id = %s AND status = ANY(%s) LIMIT 1",
        (ws, run["id"], _INFLIGHT),
    ).fetchone()
    if mine is not None:
        raise conflict("本批次还有一笔进行中的采购尝试，先上报它的结果", code="ATTEMPT_IN_FLIGHT",
                       attempt_id=mine["id"])
    acc = _account_row(conn, ws, run["account_id"])
    stop = _stop_reason(dev, acc)
    if stop == "account_archived":
        return {"attempt": None, "reason": "账号已归档，停止领取"}
    if stop == "account_paused":
        return {"attempt": None, "reason": f"账号已暂停执行：{acc['pause_reason'] or '人工暂停'}"}
    if stop is not None:
        problem = _identity_problem(dev, acc)
        return {"attempt": None, "reason": problem[1] if problem else "设备与账号的绑定已变化，停止领取"}
    today = clock.bj_today()
    used = quota.used(conn, acc["id"], today, run["simulated"], workspace_id=ws)
    if used >= acc["daily_limit"]:
        return {"attempt": None,
                "reason": f"账号 {acc['name']} 今天（{today.isoformat()}）的额度已用完：已占用 {used} / 上限 {acc['daily_limit']}"}
    elsewhere = conn.execute(
        "SELECT id FROM procure.purchase_attempts WHERE workspace_id = %s AND account_id = %s AND status = ANY(%s) "
        "LIMIT 1",
        (ws, acc["id"], _INFLIGHT),
    ).fetchone()
    if elsewhere is not None:
        return {"attempt": None, "reason": f"账号还有一笔进行中的采购尝试（#{elsewhere['id']}），等它结束后再领取"}

    skipped: list[int] = []
    unassigned = 0
    while True:
        rows = conn.execute(
            """
            SELECT o.* FROM procure.purchase_orders o
            WHERE o.workspace_id = %s AND o.assigned_account_id = %s AND o.status = 'assigned'
              AND o.suggested_purchase_date <= %s AND o.review_status = 'approved' AND NOT o.upstream_cancelled
              AND o.conflict IS NULL AND NOT (o.id = ANY(%s::bigint[]))
              AND NOT EXISTS (SELECT 1 FROM procure.purchase_attempts pa
                              WHERE pa.purchase_order_id = o.id AND pa.workspace_id = o.workspace_id
                                AND pa.status = ANY(%s))
            ORDER BY o.suggested_purchase_date, o.order_time NULLS LAST, o.id
            LIMIT 20
            FOR UPDATE OF o SKIP LOCKED
            """,
            (ws, acc["id"], today, skipped, _INFLIGHT),
        ).fetchall()
        if not rows:
            break
        for o in rows:
            if orders.derive_status(o) != "assigned":
                skipped.append(o["id"])
                continue
            sub = orders.subtotal_eff(o)
            if not orders.amount_in_range(sub, acc):
                orders.transition(conn, o, "queued",
                                  reason=f"金额 {_usd(sub)} 已不在账号 {acc['name']} 的区间内，领取时解除分配",
                                  actor_kind="device", actor_id=dev["id"], event_detail={"action": "claim_recheck"})
                unassigned += 1
                continue
            return {"attempt": _claim(conn, dev, run, acc, o, today), "reason": None}
    future = conn.execute(
        "SELECT count(*) AS n FROM procure.purchase_orders WHERE workspace_id = %s AND assigned_account_id = %s "
        "AND status = 'assigned' AND suggested_purchase_date > %s",
        (ws, acc["id"], today),
    ).fetchone()["n"]
    reason = "今天没有可执行的任务"
    extras = []
    if future:
        extras.append(f"另有 {future} 笔建议采购日期在未来")
    if unassigned:
        extras.append(f"{unassigned} 笔金额已不在区间、已解除分配")
    if extras:
        reason += "（" + "，".join(extras) + "）"
    return {"attempt": None, "reason": reason}


# ── 进度、护栏、提交登记、等待验证 ────────────────────────────────────────────


def progress(conn, device: dict[str, Any], attempt_id: Any, *, stage: Any, detail: Any = None) -> dict[str, Any]:
    """输入：设备、尝试、阶段名、细节 → 输出：{continue, cancel_reason}。

    未登记提交时，上游已取消 / 账号已暂停 / 身份不符 → continue=false（插件随后上报中止）；尝试已结束 →
    continue=false。首次进度把尝试从 claimed 推进到 checking。
    """
    dev = devices.require_active(conn, device)
    att, run, order = _attempt_for(conn, dev, attempt_id)
    _require_active(run)
    st = _text(stage, 64)
    if st is None:
        raise validation("阶段名不能为空", field="stage")
    if att["status"] not in ATTEMPT_INFLIGHT:
        return {"continue": False,
                "cancel_reason": f"本次采购尝试已结束（{ATTEMPT_STATUS_ZH.get(att['status'], att['status'])}）"}
    cancel = None
    if att["submitted_at"] is None:
        acc = _account_row(conn, att["workspace_id"], att["account_id"])
        if order["upstream_cancelled"] or order["status"] == "cancelled":
            note = order["upstream_cancel_note"]
            cancel = f"上游已取消（{note}）" if note else "上游已取消"
        elif acc is None or acc["archived_at"] is not None:
            cancel = "账号已归档"
        elif acc["execution_paused"]:
            cancel = f"账号已暂停执行：{acc['pause_reason'] or '人工暂停'}"
        else:
            problem = _identity_problem(dev, acc)
            if problem is not None:
                cancel = problem[1]
    cols: dict[str, Any] = {"stage": st, "heartbeat_at": clock.now()}
    if att["status"] == "claimed":
        cols["status"] = "checking"
    _set_attempt(conn, att, **cols)
    events.record(conn, att["workspace_id"], "attempt", att["id"], "attempt.progress", actor_kind="device",
                  actor_id=dev["id"], order_id=order["id"],
                  detail={"stage": st, "detail": _small(detail) if detail is not None else None,
                          "continue": cancel is None, "cancel_reason": cancel})
    return {"continue": cancel is None, "cancel_reason": cancel}


def submit_guard(conn, device: dict[str, Any], attempt_id: Any, facts: Any) -> dict[str, Any]:
    """输入：设备、尝试、结算页事实 → 输出：护栏裁决（services/guard.evaluate 的结果）。

    按任务与账号**当前**的有效值判定。放行 → 尝试 guard_passed；拦截 → 尝试 failed（error_code 为拦截码）、
    任务 exception（原因为拦截码的中文说明；上游已取消则 cancelled），批次可以继续下一笔。
    已登记提交或已结束的尝试 → CONFLICT。
    """
    dev = devices.require_active(conn, device)
    att, run, order = _attempt_for(conn, dev, attempt_id)
    _require_active(run)
    if att["status"] in ("submitting", "verifying"):
        raise conflict("已登记提交，不能再次做护栏判定", attempt_status=att["status"])
    if att["status"] not in _PRE_SUBMIT:
        raise conflict(f"本次采购尝试已结束（{ATTEMPT_STATUS_ZH.get(att['status'], att['status'])}）",
                       attempt_status=att["status"])
    ws = att["workspace_id"]
    acc = _account_row(conn, ws, att["account_id"])
    if acc is None:
        raise not_found("买家账号")
    verdict = guard.evaluate(
        facts if isinstance(facts, dict) else {},
        asin=orders.asin_eff(order), quantity=orders.quantity_eff(order), postal_code=order["postal_code"],
        price_cap=orders.price_cap_eff(order), amount_min=acc["min_amount"], amount_max=acc["max_amount"],
        max_delivery_days=accounts.effective_max_delivery_days(conn, ws, acc),
        expected_card_last4=acc["expected_card_last4"], site_today=clock.site_today(acc["site"]),
    )
    guard_input = _small(facts if isinstance(facts, dict) else {"raw": facts}, 20000)
    now = clock.now()
    if verdict["verdict"] == "pass":
        _set_attempt(conn, att, status="guard_passed", stage="guard_passed", guard_input=guard_input,
                     guard_verdict=verdict, delivery_days=verdict["delivery_days"], heartbeat_at=now)
        events.record(conn, ws, "attempt", att["id"], "attempt.guard_passed", actor_kind="device", actor_id=dev["id"],
                      order_id=order["id"], detail={"total_for_cap": verdict["total_for_cap"],
                                                    "delivery_days": verdict["delivery_days"]})
        return verdict
    code = verdict["code"]
    zh = ERROR_CODES[code].zh if code in ERROR_CODES else "护栏拦截"
    reason = f"{zh}：{verdict['reasons'][0]}" if verdict["reasons"] else zh
    att = _set_attempt(conn, att, status="failed", stage="guard", guard_input=guard_input, guard_verdict=verdict,
                       delivery_days=verdict["delivery_days"], error_code=code,
                       error_detail="；".join(verdict["reasons"])[:1000] or zh, finished_at=now, heartbeat_at=now)
    _to_exception(conn, order, att, reason, actor_kind="device", actor_id=dev["id"])
    events.record(conn, ws, "attempt", att["id"], "attempt.guard_blocked", actor_kind="device", actor_id=dev["id"],
                  order_id=order["id"], detail={"code": code, "reasons": verdict["reasons"]})
    _bump(conn, run, "blocked")
    return verdict


def mark_submitting(conn, device: dict[str, Any], attempt_id: Any) -> dict[str, Any]:
    """输入：设备、尝试 → 输出：{ok: true, submitted_at}。返回 200 后插件才允许点击下单；登记即写 submitted_at
    （此后视为可能已下单）。

    拒绝（409）：护栏未放行 / 尝试不在 guard_passed、批次失效（LEASE_LOST）、任务已取消或状态已变、
    账号已暂停（ACCOUNT_PAUSED）、身份不符（IDENTITY_MISMATCH）。
    """
    dev = devices.require_active(conn, device)
    att, run, order = _attempt_for(conn, dev, attempt_id)
    _require_active(run)
    verdict = att["guard_verdict"] if isinstance(att["guard_verdict"], dict) else {}
    if att["status"] != "guard_passed" or verdict.get("verdict") != "pass":
        if att["status"] in ("submitting", "verifying"):
            msg = "该尝试已登记提交，不能重复登记"
        elif att["status"] in FINAL_STATUSES:
            msg = f"本次采购尝试已结束（{ATTEMPT_STATUS_ZH.get(att['status'], att['status'])}），不能提交"
        else:
            msg = "护栏还没有放行，不能提交"
        raise conflict(msg, attempt_status=att["status"])
    if order["upstream_cancelled"] or order["status"] == "cancelled":
        raise conflict("上游已取消，不能提交", order_status=order["status"])
    if not _owns(order, att):
        raise conflict("任务状态已变化，不能提交", order_status=order["status"])
    acc = _account_row(conn, att["workspace_id"], att["account_id"])
    if acc is None or acc["archived_at"] is not None:
        raise conflict("账号已归档，不能提交")
    if acc["execution_paused"]:
        raise conflict(f"账号已暂停执行（{acc['pause_reason'] or '人工暂停'}），不能提交", code="ACCOUNT_PAUSED")
    problem = _identity_problem(dev, acc)
    if problem is not None:
        raise conflict(f"{problem[1]}，不能提交", code="IDENTITY_MISMATCH", identity_status=problem[0])
    now = clock.now()
    _set_attempt(conn, att, status="submitting", submitted_at=now, stage="submitting", heartbeat_at=now)
    events.record(conn, att["workspace_id"], "attempt", att["id"], "attempt.submitting", actor_kind="device",
                  actor_id=dev["id"], order_id=order["id"], detail={"run_id": att["run_id"]})
    return {"ok": True, "submitted_at": now.isoformat()}


def mark_verifying(conn, device: dict[str, Any], attempt_id: Any, note: Any = None) -> dict[str, Any]:
    """输入：设备、尝试、说明 → 输出：{ok: true, attempt_status: 'verifying'}（银行人工验证；任务与名额保留）。

    只限已登记提交的尝试（submitting / verifying），否则 CONFLICT。
    """
    dev = devices.require_active(conn, device)
    att, run, order = _attempt_for(conn, dev, attempt_id)
    _require_active(run)
    if att["status"] not in ("submitting", "verifying"):
        raise conflict("只有已登记提交的尝试才能进入等待验证", attempt_status=att["status"])
    text = _text(note, 500)
    _set_attempt(conn, att, status="verifying", stage="verifying", heartbeat_at=clock.now())
    reason = f"等待银行人工验证：{text}" if text else "等待银行人工验证"
    if _owns(order, att) and order["status_reason"] != reason:
        orders.transition(conn, order, order["status"], reason=reason, actor_kind="device", actor_id=dev["id"])
    events.record(conn, att["workspace_id"], "attempt", att["id"], "attempt.verifying", actor_kind="device",
                  actor_id=dev["id"], order_id=order["id"], detail={"note": text})
    return {"ok": True, "attempt_status": "verifying"}


# ── 结果 ──────────────────────────────────────────────────────────────────────


def _parse_result(payload: Any, site: str) -> dict[str, Any]:
    """输入：插件上报的结果体 → 输出：规范化的结果（outcome 必须合法；金额、卡号、配送读不出时为 None）。"""
    if not isinstance(payload, dict):
        raise validation("结果格式不对")
    outcome = payload.get("outcome")
    if outcome not in OUTCOMES:
        raise validation("outcome 只能是 succeeded / failed / unknown", field="outcome")
    code = _text(payload.get("error_code"), 64)
    amounts_in = payload.get("amounts") if isinstance(payload.get("amounts"), dict) else {}
    amounts: dict[str, Decimal | None] = {}
    for k in AMOUNT_KEYS:
        v = amounts_in.get(k)
        d = None if isinstance(v, bool) or not isinstance(v, (str, int, float, Decimal)) else money(v)
        amounts[k] = d if d is not None and d >= 0 else None
    card = payload.get("card_last4")
    card_s = "".join(ch for ch in str(card) if ch.isdigit()) if card is not None else ""
    delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
    d_date = None
    if isinstance(delivery.get("date"), str):
        try:
            d_date = date.fromisoformat(delivery["date"].strip()[:10])
        except ValueError:
            d_date = None
    d_days = delivery.get("days")
    d_days = d_days if isinstance(d_days, int) and not isinstance(d_days, bool) and d_days >= 0 else None
    if d_date is not None:
        d_days = (d_date - clock.site_today(site)).days
    return {
        "outcome": outcome,
        "amazon_order_no": _text(payload.get("amazon_order_no"), 64),
        "error_code": code.upper() if code else None,
        "error_detail": _text(payload.get("error_detail"), 1000),
        "amounts": amounts,
        "card_last4": card_s[-4:] if len(card_s) >= 4 else None,
        "delivery_text": _text(delivery.get("text"), 200),
        "delivery_date": d_date,
        "delivery_days": d_days,
    }


def _order_no_problem(conn, att: dict[str, Any], no: str | None) -> str | None:
    """输入：尝试、上报的亚马逊订单号 → 输出：不能据此登记成功的原因；合格 → None。"""
    if not no:
        return "上报成功但没有亚马逊订单号"
    if att["simulated"]:
        return None if no.startswith(SIM_PREFIX) else f"模拟尝试的单号应以 {SIM_PREFIX} 开头（收到 {no}）"
    if no.upper().startswith(SIM_PREFIX):
        return f"真实尝试不能使用模拟单号（收到 {no}）"
    dup = conn.execute(
        """
        SELECT o.upstream_order_no FROM procure.purchase_attempts a
        JOIN procure.purchase_orders o ON o.id = a.purchase_order_id AND o.workspace_id = a.workspace_id
        WHERE a.workspace_id = %s AND a.amazon_order_no = %s AND NOT a.simulated AND a.status = 'succeeded'
          AND a.purchase_order_id <> %s LIMIT 1
        """,
        (att["workspace_id"], no, att["purchase_order_id"]),
    ).fetchone()
    if dup is not None:
        return f"亚马逊订单号 {no} 已登记在任务 {dup['upstream_order_no']}"
    return None


def _target_status(conn, att: dict[str, Any], r: dict[str, Any]) -> tuple[str, str | None]:
    """输入：尝试、规范化结果 → 输出：(尝试应有的结局, 不能登记成功的原因)。"""
    if r["outcome"] == "succeeded":
        problem = _order_no_problem(conn, att, r["amazon_order_no"])
        if problem is None and att["submitted_at"] is None:
            problem = "未登记提交却上报成功"
        return ("succeeded", None) if problem is None else ("unknown", problem)
    if r["outcome"] == "unknown" or att["submitted_at"] is not None:
        return "unknown", None
    ec = ERROR_CODES.get(r["error_code"] or "")
    return ("aborted" if ec is not None and ec.category == "abort" else "failed"), None


def _same_as_stored(att: dict[str, Any], target: str, r: dict[str, Any]) -> bool:
    if att["status"] != target:
        return False
    if target == "succeeded":
        return att["amazon_order_no"] == r["amazon_order_no"]
    return (att["error_code"] or None) == (r["error_code"] or None)


def _result_cols(r: dict[str, Any], now: datetime) -> dict[str, Any]:
    cols: dict[str, Any] = {"finished_at": now, "heartbeat_at": now, "error_code": r["error_code"],
                            "error_detail": r["error_detail"]}
    if r["card_last4"] is not None:
        cols["card_last4"] = r["card_last4"]
    for k in ("delivery_text", "delivery_date", "delivery_days"):
        if r[k] is not None:
            cols[k] = r[k]
    for k, v in r["amounts"].items():
        if v is not None:
            cols[k] = v
    return cols


def report_result(conn, device: dict[str, Any], attempt_id: Any, payload: Any) -> dict[str, Any]:
    """输入：设备、尝试、结果 {outcome, amazon_order_no, amounts, card_last4, delivery, error_code, error_detail}
    → 输出：{attempt_status, order_status}（重复上报时另有 duplicate: true）。

    处理表（契约 §5）：
    - 成功且已登记提交、单号合格（模拟须以 SIM- 开头，真实不得以 SIM- 开头且未登记在别的任务）→ 尝试
      succeeded，任务 purchased（purchased_attempt_id、结果版本 +1）；非模拟才调用写回入队。
    - 已登记提交后的任何非成功、插件说结果不明、成功但单号不合格或未登记提交 → 尝试 unknown，任务
      result_unknown，账号暂停执行（「结果待核实：<平台订单号>」）。
    - 未登记提交的失败：中止类（ABORTED_*）→ aborted，任务回已分配（上游已取消 → cancelled）；账号级 →
      failed，任务回已分配、账号暂停；PAGE_LOAD_FAILED → failed，首次回已分配自动重试，第二次 → exception；
      其他（任务级、护栏拦截码、未知码）→ failed，任务 exception。
    - 已结束的尝试再次上报：结局与单号 / 错误码都相同 → 原样返回；不同 → CONFLICT。
    """
    dev = devices.require_active(conn, device)
    att, run, order = _attempt_for(conn, dev, attempt_id)
    ws = att["workspace_id"]
    acc = _account_row(conn, ws, att["account_id"]) if att["account_id"] is not None else None
    r = _parse_result(payload, acc["site"] if acc else "US")
    target, problem = _target_status(conn, att, r)
    if att["status"] in FINAL_STATUSES:
        if _same_as_stored(att, target, r):
            return {"attempt_status": att["status"], "order_status": order["status"], "duplicate": True}
        raise conflict(
            f"该采购尝试的结果已登记为「{ATTEMPT_STATUS_ZH.get(att['status'], att['status'])}」，与本次上报不一致",
            attempt_status=att["status"], reported=r["outcome"])
    _require_active(run)
    ak, aid = _actor_of(dev)
    now = clock.now()
    cols = _result_cols(r, now)

    if target == "succeeded":
        att = _set_attempt(conn, att, status="succeeded", stage="done", amazon_order_no=r["amazon_order_no"], **cols)
        upd: dict[str, Any] = {"purchased_attempt_id": att["id"], "current_attempt_id": None,
                               "result_version": order["result_version"] + 1, "page_load_retries": 0}
        if order["upstream_cancelled"]:
            upd.update(needs_attention=True, attention_note="采购后上游取消，请人工处理对应的亚马逊订单")
        label = "模拟采购成功" if att["simulated"] else "采购成功"
        order = orders.transition(conn, order, "purchased", reason=f"{label}：{r['amazon_order_no']}", actor_kind=ak,
                                  actor_id=aid, updates=upd,
                                  event_detail={"attempt_id": att["id"], "amazon_order_no": r["amazon_order_no"],
                                                "simulated": att["simulated"]})
        if not att["simulated"]:
            writeback.enqueue_for_order(conn, ws, order["id"])
        events.record(conn, ws, "attempt", att["id"], "attempt.succeeded", actor_kind=ak, actor_id=aid,
                      order_id=order["id"], detail={"amazon_order_no": r["amazon_order_no"],
                                                    "total": r["amounts"].get("total"), "simulated": att["simulated"]})
        _bump(conn, run, "succeeded")
        return {"attempt_status": "succeeded", "order_status": order["status"]}

    if target == "unknown":
        why = problem or r["error_detail"] or (ERROR_CODES[r["error_code"]].zh if r["error_code"] in ERROR_CODES
                                               else r["error_code"]) or "插件无法确认结果"
        cols["error_detail"] = why if not r["error_detail"] or r["error_detail"] == why else f"{why}；{r['error_detail']}"
        att = _set_attempt(conn, att, status="unknown", stage="reported", amazon_order_no=r["amazon_order_no"], **cols)
        prefix = "已登记提交但未确认结果" if att["submitted_at"] is not None else "插件无法确认是否已下单"
        order = _to_unknown(conn, order, att, f"{prefix}：{why}", actor_kind=ak, actor_id=aid)
        _pause_account(conn, ws, att["account_id"], f"结果待核实：{order['upstream_order_no']}",
                       cause="result_unknown", att=att, code=r["error_code"], actor_kind=ak, actor_id=aid)
        events.record(conn, ws, "attempt", att["id"], "attempt.unknown", actor_kind=ak, actor_id=aid,
                      order_id=order["id"], detail={"outcome": r["outcome"], "error_code": r["error_code"],
                                                    "reason": why, "submitted": att["submitted_at"] is not None})
        _bump(conn, run, "unknown")
        return {"attempt_status": "unknown", "order_status": order["status"]}

    ec = ERROR_CODES.get(r["error_code"] or "")
    zh = ec.zh if ec else (r["error_detail"] or (f"插件上报失败（{r['error_code']}）" if r["error_code"]
                                                  else "插件上报失败"))
    detail_txt = r["error_detail"]
    reason = f"{zh}：{detail_txt}" if detail_txt and detail_txt != zh else zh
    if target == "aborted":
        att = _set_attempt(conn, att, status="aborted", stage="aborted", **cols)
        order = _release(conn, order, att, f"插件中止：{reason}，任务保留待重新领取", actor_kind=ak, actor_id=aid)
        events.record(conn, ws, "attempt", att["id"], "attempt.aborted", actor_kind=ak, actor_id=aid,
                      order_id=order["id"], detail={"error_code": r["error_code"], "reason": reason})
        _bump(conn, run, "aborted")
        return {"attempt_status": "aborted", "order_status": order["status"]}

    att = _set_attempt(conn, att, status="failed", stage="reported", **cols)
    category = ec.category if ec else "task"
    if category == "account":
        order = _release(conn, order, att, f"{reason}（账号已暂停执行，任务保留）", actor_kind=ak, actor_id=aid)
        _pause_account(conn, ws, att["account_id"], zh, cause="account_error", att=att, code=r["error_code"],
                       actor_kind=ak, actor_id=aid)
    elif category == "retry_once" and order["page_load_retries"] < 1:
        order = _release(conn, order, att, f"{reason}，将自动重试一次", actor_kind=ak, actor_id=aid,
                         updates={"page_load_retries": order["page_load_retries"] + 1})
    elif category == "retry_once":
        order = _to_exception(conn, order, att, f"{reason}（已自动重试一次仍失败）", actor_kind=ak, actor_id=aid)
    else:
        order = _to_exception(conn, order, att, reason, actor_kind=ak, actor_id=aid)
    events.record(conn, ws, "attempt", att["id"], "attempt.failed", actor_kind=ak, actor_id=aid, order_id=order["id"],
                  detail={"error_code": r["error_code"], "category": category, "reason": reason})
    _bump(conn, run, "failed")
    return {"attempt_status": "failed", "order_status": order["status"]}


# ── 结束批次与清扫 ────────────────────────────────────────────────────────────


def finish_run(conn, device: dict[str, Any], run_id: Any, reason: Any = "finished") -> dict[str, Any]:
    """输入：设备、批次、结束原因（插件给的短码，缺省 finished）→ 输出：{run_id, ended_at, end_reason, stats}。

    还有在途尝试时按租约过期同样处理（未登记提交 → 中止、任务回已分配；已登记提交 → 结果待核实、暂停账号）。
    批次已结束或租约已过期 → LEASE_LOST。
    """
    dev = devices.require_active(conn, device)
    run = _run_for(conn, dev, run_id)
    _require_active(run)
    end_reason = _text(reason, 64) or "finished"
    end_run(conn, run, end_reason, actor_kind="device", actor_id=dev["id"], cause="插件结束了执行批次")
    row = conn.execute("SELECT * FROM procure.execution_runs WHERE id = %s AND workspace_id = %s",
                       (run["id"], run["workspace_id"])).fetchone()
    return {"run_id": row["id"], "ended_at": _iso(row["ended_at"]), "end_reason": row["end_reason"],
            "stats": row["stats"]}


def sweep(conn, now: datetime | None = None) -> dict[str, Any]:
    """输入：连接、可选「现在」→ 输出：{expired_runs, revoked_runs, orphan_attempts, aborted, unknown}（id 列表与计数）。

    跨全部工作区（后台任务）：租约已过期的批次 → 结束（lease_expired）；设备已撤销的批次 → 结束
    （device_revoked）；它们的在途尝试按 _settle_inflight 处理。另把「所属批次已结束」的在途尝试一并收尾。
    被别的事务锁住的批次跳过（SKIP LOCKED），下一轮再处理。
    """
    t = now or clock.now()
    out: dict[str, Any] = {"expired_runs": [], "revoked_runs": [], "orphan_attempts": [], "aborted": 0, "unknown": 0}
    expired = conn.execute(
        "SELECT * FROM procure.execution_runs WHERE ended_at IS NULL AND lease_expires_at <= %s "
        "ORDER BY id FOR UPDATE SKIP LOCKED",
        (t,),
    ).fetchall()
    for run in expired:
        res = end_run(conn, run, "lease_expired")
        out["expired_runs"].append(run["id"])
        out["aborted"] += res["aborted"]
        out["unknown"] += res["unknown"]
    revoked = conn.execute(
        """
        SELECT r.* FROM procure.execution_runs r
        JOIN procure.devices d ON d.id = r.device_id AND d.workspace_id = r.workspace_id
        WHERE r.ended_at IS NULL AND d.revoked_at IS NOT NULL
        ORDER BY r.id FOR UPDATE OF r SKIP LOCKED
        """
    ).fetchall()
    for run in revoked:
        res = end_run(conn, run, "device_revoked")
        out["revoked_runs"].append(run["id"])
        out["aborted"] += res["aborted"]
        out["unknown"] += res["unknown"]
    orphans = conn.execute(
        """
        SELECT a.id, a.workspace_id, a.purchase_order_id, a.run_id FROM procure.purchase_attempts a
        LEFT JOIN procure.execution_runs r ON r.id = a.run_id AND r.workspace_id = a.workspace_id
        WHERE a.status = ANY(%s) AND a.kind <> 'external' AND (r.id IS NULL OR r.ended_at IS NOT NULL)
        ORDER BY a.id
        """,
        (_INFLIGHT,),
    ).fetchall()
    for p in orphans:
        order = orders.get_row(conn, p["workspace_id"], p["purchase_order_id"], for_update=True)
        att = conn.execute("SELECT * FROM procure.purchase_attempts WHERE id = %s AND workspace_id = %s "
                           "FOR UPDATE SKIP LOCKED", (p["id"], p["workspace_id"])).fetchone()
        if att is None or att["status"] not in ATTEMPT_INFLIGHT:
            continue
        st = _settle_inflight(conn, None, att, order, "所属执行批次已结束", actor_kind="system", actor_id=None)
        out["orphan_attempts"].append(att["id"])
        out[st] += 1
    return out
