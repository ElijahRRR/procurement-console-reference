"""执行协议（契约 §5）：账号执行批次（租约）、领取、进度、服务端护栏、提交登记、等待验证、结果回报、
结束批次、租约清扫。插件请求的 HTTP 外壳在 server/routes（插件路由），这里只有业务判断。

执行权与互斥（方案 v2 §2，全部由数据库唯一索引兜底）：

- 一个账号同时只有一个未结束的批次（execution_runs_one_active_per_account）。开始批次前先查一遍给出
  「谁在执行、从何时开始」，真正的互斥靠插入时的唯一索引；冲突在 SAVEPOINT 里捕获，调用方事务仍可用。
- 一笔任务、一个账号同时只有一个在途尝试（claimed / checking / guard_passed / submitting / verifying）。
- 每个调用都核对批次 / 尝试属于**这台设备、这个工作区**（否则 404，不泄露存在性），并且批次仍有效
  （未结束且租约未过期，否则 409 LEASE_LOST）。已结束尝试的重复结果上报例外：内容相同就原样返回。

加锁顺序（避免死锁，与 services/orders 的「任务 → 尝试」一致）：执行闸门（工作区咨询锁：开始批次 / 领取取
共享锁，清除模拟结果取独占锁）→ 批次 → 任务 → 尝试 → 账号。

提交登记前的最后关口（mark_submitting）：除状态、取消、账号、身份（含新鲜度）外，还要求领取后来源没有执行相关
变化（见「来源基线」一节），并按最新配置对存下的结算事实复核一次护栏。

提交登记是分界线（铁律 9）：

- submitted_at 为空：可以中止（aborted）、可重试（PAGE_LOAD_FAILED 自动重试一次）、可转异常；
  任务回到已分配时**保留分配**。
- submitted_at 非空：任何非成功结局（失败、结果不明、断线、租约过期、批次被结束、设备被撤销）一律
  尝试 → unknown、任务 → result_unknown、账号暂停执行（原因「结果待核实：<平台订单号>」），绝不自动重买。
- 插件说「结果不明」，或未登记提交却上报成功、成功却没有合格的亚马逊订单号：同样按结果待核实处理
  （宁可人工核实，也不让任务回到可领取状态）。
- 回执不丢：已登记提交的尝试在租约过期、尚未清扫时照常接收结果；已是「结果待核实」后才送到的不同结果
  （迟到的回执）只记录、不改状态（_late_receipt）。

账号被本模块暂停时，除 accounts.pause 写的 'account.paused' 外，另写一条 PAUSE_MARKER_KIND 事件
{cause: result_unknown | account_error, simulated, attempt_id, code}，供「清除模拟结果」判断哪些账号
只是被模拟的待核实结果暂停的（services/simulation）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from registry import settings
from services import accounts, clock, devices, events, guard, orders, quota, settings_svc, writeback
from services.errors import DomainError, conflict, not_found, validation
from services.models import (
    ATTEMPT_INFLIGHT, ERROR_CODES, EXECUTION_FIELDS, PRE_EXECUTION_STATUSES, UpstreamRecord, money, money_cap,
)

TRIGGERS = ("manual", "scheduled")
OUTCOMES = ("succeeded", "failed", "unknown")
FINAL_STATUSES = ("succeeded", "failed", "unknown", "aborted")
AMOUNT_KEYS = ("subtotal", "shipping", "tax", "total_before_tax", "total")
PAUSE_MARKER_KIND = "account.paused_by_execution"
SIM_PREFIX = "SIM-"
_MAX_AMOUNT = Decimal("10000000000")       # 尝试的金额列是 numeric(12,2)：放不下的按读不出（None）

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
    "CLAIM_BUSY_RETRY_S", "PAUSE_MARKER_KIND", "apply_source_drift", "drift_updates", "end_run", "execution_gate",
    "finish_run", "mark_submitting", "mark_verifying", "next_attempt", "progress", "report_result", "run_heartbeat",
    "start_run", "submit_guard", "sweep",
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


def _identity_stale(dev: dict[str, Any]) -> str | None:
    """输入：设备行 → 输出：身份超过 identity_fresh_s 没有上报时的说明；新鲜 → None（开始批次、领取、提交前都看）。"""
    fresh_s = int(settings.get().identity_fresh_s)
    at = dev.get("identity_at")
    if at is not None and (clock.now() - at).total_seconds() <= fresh_s:
        return None
    return f"插件超过 {max(1, fresh_s // 60)} 分钟没有上报登录身份，身份信息已过期"


# ── 执行闸门（清除模拟结果 ↔ 开始批次 / 领取）──────────────────────────────────

_LOCK_NS = 7_310_433          # pg_advisory_xact_lock 命名空间（allocation 用 7_310_431、feishu_intake 用 7_310_432）


def execution_gate(conn, workspace_id: int, *, exclusive: bool = False) -> None:
    """输入：连接、工作区、是否独占 → 输出：无（事务级咨询锁，事务结束释放）。

    开始批次与领取取共享锁（彼此不排队）；清除模拟结果取独占锁，等进行中的开始 / 领取提交后才动手，清除期间
    也不会冒出新的批次或尝试（否则新领取的尝试会被一并清掉、任务却卡在采购中）。调用方须在加任何行锁之前取。
    """
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    conn.execute(f"SELECT {fn}(%s::int, %s::int)", (_LOCK_NS, int(workspace_id) % 2_147_483_647))


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


# ── 来源基线与领取后的来源变化 ────────────────────────────────────────────────
#
# 采购开始后，来源更新不改执行相关原值（契约 §2），但 order_sources 的规范化记录照样前进到最新。为了不让
# 这些被挡住的变化在采购没完成就结束时丢掉，领取时把每条来源关联的指纹与被监视的值存进尝试的
# task_snapshot.source_baseline。之后：
#
# - 提交登记前（进度、提交登记）：来源相对基线出现外部 AMZ 订单号、人审结论改变或执行相关字段被改 →
#   进度 continue=false、提交登记 409 SOURCE_CHANGED（插件随后上报中止）。
# - 采购没有完成就回到开始前 / 异常（中止、清扫、失败、护栏拦截、清除模拟结果）：apply_source_drift 把变化
#   落到任务上：人审结论直接采用（撤回 → 待确认）；外部 AMZ 订单号、字段变化写进 conflict（incoming 为来源
#   新值，控制台「接受 / 保留」），任务按派生规则转待确认 / 待核实（异常保持异常），并置 needs_attention。
#   每笔尝试只落一次（task_snapshot.source_drift_checked_at）。

_SOURCE_TEXT_FIELDS = frozenset({"recipient_name", "address1", "address2", "city", "state", "postal_code", "phone",
                                 "country", "district"})
_SOURCE_MONEY_FIELDS = frozenset({"unit_price", "price_cap"})
_DRIFT_MARK = "source_drift_checked_at"


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _source_vals(rec: UpstreamRecord) -> dict[str, Any]:
    """输入：上游记录 → 输出：被监视的值（任务列口径，可入 JSON）：执行相关字段、建议采购日期依据、人审、外部单号。"""
    vals: dict[str, Any] = {}
    for f in EXECUTION_FIELDS:
        v = getattr(rec, f, None)
        if f in _SOURCE_TEXT_FIELDS:
            v = _clean(v)
        vals[f] = v
    asin = _clean(rec.asin)
    asin = asin.upper() if asin else None
    vals["asin"] = asin if asin and orders.ASIN_RE.match(asin) else None
    q = rec.quantity
    vals["quantity"] = q if isinstance(q, int) and not isinstance(q, bool) and q > 0 else None
    vals["unit_price"] = money(rec.unit_price)
    vals["price_cap"] = money_cap(rec.price_cap)
    vals["country"] = (vals["country"] or "US").upper()
    if rec.suggested_purchase_date is not None:
        vals["suggested_purchase_date"], vals["suggested_date_basis"] = rec.suggested_purchase_date, "source"
    else:
        ot = rec.order_time
        if ot is not None and ot.tzinfo is None:
            ot = ot.replace(tzinfo=clock.BJ_TZ)
        sd = rec.ship_days if isinstance(rec.ship_days, int) and not isinstance(rec.ship_days, bool) else None
        d = orders.compute_suggested_date(ot, sd)
        vals["suggested_purchase_date"], vals["suggested_date_basis"] = d, ("computed" if d else None)
    vals["review_status"] = rec.review if rec.review in ("approved", "rejected", "unknown") else "unknown"
    vals["review_value"] = _clean(rec.review_value)
    vals["external_amz_order_no"] = _clean(rec.external_amz_order_no)
    vals["external_buyer_account"] = _clean(rec.external_buyer_account)
    return _jsonable(vals)


def _canon(f: str, v: Any) -> Any:
    """输入：字段、值（来源值或任务列）→ 输出：可直接比较的规范形式（金额两位、文本去空白不分大小写）。"""
    if v is None:
        return None
    if f in _SOURCE_MONEY_FIELDS:
        m = money_cap(v) if f == "price_cap" else money(v)
        return None if m is None else format(m, "f")
    if f == "quantity":
        try:
            return int(v)
        except (TypeError, ValueError):
            return str(v)
    if isinstance(v, (datetime, date)):
        v = v.isoformat()
    s = " ".join(str(v).split()).casefold()
    return s or None


def _links(conn, order: dict[str, Any]) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id, kind, feishu_connection_id, feishu_record_id, import_batch_id, import_row_no, normalized, "
        "fingerprint FROM procure.order_sources WHERE workspace_id = %s AND purchase_order_id = %s "
        "ORDER BY last_seen_at, id",
        (order["workspace_id"], order["id"]),
    ).fetchall()


def _link_vals(link: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return _source_vals(UpstreamRecord.from_json(link["normalized"] or {}))
    except (TypeError, ValueError, KeyError):
        return None


def _source_baseline(conn, order: dict[str, Any]) -> list[dict[str, Any]]:
    """输入：任务 → 输出：[{id, kind, fp, vals}]：它每条来源关联此刻的指纹与被监视的值（按最近出现排序）。"""
    out = []
    for link in _links(conn, order):
        vals = _link_vals(link)
        if vals is not None:
            out.append({"id": link["id"], "kind": link["kind"], "fp": link["fingerprint"], "vals": vals})
    return out


def _order_vals(order: dict[str, Any]) -> dict[str, Any]:
    keys = (*EXECUTION_FIELDS, "suggested_date_basis", "review_status", "review_value", "external_amz_order_no")
    return _jsonable({k: order.get(k) for k in keys})


def _link_desc(link: dict[str, Any]) -> dict[str, Any]:
    if link["kind"] == "feishu":
        return {"kind": "feishu", "connection_id": link["feishu_connection_id"], "record_id": link["feishu_record_id"]}
    return {"kind": "file", "batch_id": link["import_batch_id"], "row_no": link["import_row_no"]}


def _known_order_nos(conn, order: dict[str, Any]) -> set[str]:
    rows = conn.execute(
        "SELECT amazon_order_no FROM procure.purchase_attempts WHERE purchase_order_id = %s AND workspace_id = %s "
        "AND amazon_order_no IS NOT NULL",
        (order["id"], order["workspace_id"]),
    ).fetchall()
    nos = {_canon("amazon_order_no", r["amazon_order_no"]) for r in rows}
    if order.get("external_amz_order_no"):
        nos.add(_canon("amazon_order_no", order["external_amz_order_no"]))
    return nos


def _source_drift(conn, order: dict[str, Any], att: dict[str, Any]) -> dict[str, Any] | None:
    """输入：任务、尝试（task_snapshot 里有领取时的来源基线）→ 输出：领取之后来源在执行相关内容上的变化
    {fields: {字段: [任务现值, 来源新值]}, incoming, review, external, source_kind, source}；没有变化、
    或尝试没有基线（旧数据、外部尝试）→ None。

    同一条来源关联与自己的基线比；领取后才出现的关联（如新一批文件）与同种来源最近的基线比（内容与任一
    基线相同就不算变化），没有同种基线时与任务现值比。另一种来源的空值不算变化（与 ingest 一致）。
    """
    snap = att.get("task_snapshot") if isinstance(att.get("task_snapshot"), dict) else {}
    base = snap.get("source_baseline")
    if not isinstance(base, list):
        return None
    by_id = {b.get("id"): b for b in base if isinstance(b, dict)}
    base_fps = {b.get("fp") for b in by_id.values()}
    primary = order.get("primary_source_kind")
    fields: dict[str, list[Any]] = {}
    incoming: dict[str, Any] = {}
    review = external = None
    src_kind = src = None
    known_nos: set[str] | None = None
    for link in _links(conn, order):
        b = by_id.get(link["id"])
        if (b is not None and b.get("fp") == link["fingerprint"]) or (b is None and link["fingerprint"] in base_fps):
            continue
        cur = _link_vals(link)
        if cur is None:
            continue
        if b is not None:
            ref = b.get("vals") or {}
        else:
            same = [x for x in by_id.values() if x.get("kind") == link["kind"]]
            ref = (same[-1].get("vals") or {}) if same else _order_vals(order)
        same_kind = link["kind"] == primary
        found = False
        for f in EXECUTION_FIELDS:
            c = cur.get(f)
            if _canon(f, c) == _canon(f, ref.get(f)) or _canon(f, c) == _canon(f, order.get(f)):
                continue
            if c is None and (f == "suggested_purchase_date" or (not same_kind and f not in _SOURCE_TEXT_FIELDS)):
                continue
            fields[f] = [_jsonable(order.get(f)), c]
            incoming[f] = c
            if f == "suggested_purchase_date":
                incoming["suggested_date_basis"] = cur.get("suggested_date_basis")
            found = True
        st = cur.get("review_status")
        if (same_kind and st != ref.get("review_status") and st != order.get("review_status")
                and not (order.get("review_source") == "console" and st == "unknown")):
            review = {"review_status": st, "review_value": cur.get("review_value"), "source_kind": link["kind"]}
            found = True
        no = cur.get("external_amz_order_no")
        if no and _canon("amazon_order_no", no) != _canon("amazon_order_no", ref.get("external_amz_order_no")):
            if known_nos is None:
                known_nos = _known_order_nos(conn, order)
            if _canon("amazon_order_no", no) not in known_nos:
                external = {"external_amz_order_no": no, "external_buyer_account": cur.get("external_buyer_account"),
                            "source_kind": link["kind"], "source": _link_desc(link)}
                found = True
        if found:
            src_kind, src = link["kind"], _link_desc(link)
    if not fields and review is None and external is None:
        return None
    return {"fields": fields, "incoming": incoming, "review": review, "external": external,
            "source_kind": src_kind, "source": src}


def _hold_reason(order: dict[str, Any]) -> str | None:
    """输入：采购中的任务 → 输出：来源在执行期间写在任务上的暂停（order.conflict，领取时一定为空）的说明；
    没有 → None。插件据此在提交前中止，任务随后转待核实。"""
    c = order.get("conflict")
    if not c:
        return None
    reason = c.get("reason") if isinstance(c, dict) else None
    return f"来源要求暂停采购：{reason}" if reason else "来源要求暂停采购，需人工核实"


def _zh_join(prefix: str, text: str) -> str:
    """输入：中文前缀、后文 → 输出：拼接结果（后文以英文或数字开头时留半角空格）。"""
    return f"{prefix} {text}" if text[:1].isascii() and text[:1].isalnum() else f"{prefix}{text}"


def _drift_text(drift: dict[str, Any]) -> str:
    """输入：_source_drift 的结果 → 输出：中文说明（如「来源出现 AMZ 订单号 …；来源修改了 ASIN、数量」）。"""
    parts = []
    if drift["external"]:
        parts.append(f"来源出现 AMZ 订单号 {drift['external']['external_amz_order_no']}")
    if drift["review"]:
        rv = drift["review"]
        shown = rv["review_value"] or {"rejected": "拒绝", "unknown": "未知"}.get(rv["review_status"], rv["review_status"])
        parts.append(f"人审结论变为「{shown}」")
    if drift["fields"]:
        labels = "、".join(orders.FIELD_LABELS.get(f, f) for f in drift["fields"])
        parts.append(_zh_join("来源修改了", labels))
    return "领取后上游有变化：" + "；".join(parts)


def _drift_checked(att: dict[str, Any]) -> bool:
    snap = att.get("task_snapshot")
    return isinstance(snap, dict) and snap.get(_DRIFT_MARK) is not None


def _mark_drift_checked(conn, att: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE procure.purchase_attempts SET task_snapshot = COALESCE(task_snapshot, '{}'::jsonb) || %s "
        "WHERE id = %s AND workspace_id = %s",
        (Jsonb({_DRIFT_MARK: clock.now().isoformat()}), att["id"], att["workspace_id"]),
    )


def drift_updates(conn, order: dict[str, Any], att: dict[str, Any], *, actor_kind: str,
                  actor_id: int | None) -> dict[str, Any] | None:
    """输入：任务（已锁）、它最近一次没有完成采购的尝试、操作者 → 输出：要写到任务上的列（人审、conflict、
    needs_attention）；尝试已处理过、没有基线或来源没有变化 → None。写 'order.source_changed_during_attempt' 事件，
    并把尝试标记为已处理（之后同一笔尝试不再重复落地）。只计算与记录，不改任务状态。"""
    if _drift_checked(att):
        return None
    drift = _source_drift(conn, order, att)
    _mark_drift_checked(conn, att)
    if drift is None:
        return None
    now = clock.now()
    text = _drift_text(drift)
    upd: dict[str, Any] = {}
    if drift["review"]:
        rv = drift["review"]
        upd.update(review_status=rv["review_status"], review_value=rv["review_value"],
                   review_source=rv["source_kind"], reviewed_by=None, reviewed_at=now)
    payload = None
    if drift["external"]:
        ex = drift["external"]
        no = ex["external_amz_order_no"]
        reason = f"采购过程中来源出现 AMZ 订单号 {no}，需人工核实是否已在别处采购"
        payload = {"kind": "external_order_no", "source_kind": ex["source_kind"], "reason": reason,
                   "fields": {"external_amz_order_no": [order.get("external_amz_order_no"), no]},
                   "incoming": {"external_amz_order_no": no, "external_buyer_account": ex["external_buyer_account"]},
                   "source": ex["source"], "at": now}
    elif drift["fields"]:
        labels = "、".join(orders.FIELD_LABELS.get(f, f) for f in drift["fields"])
        reason = _zh_join("采购过程中来源修改了", labels) + "，需人工核实"
        payload = {"kind": "fields", "source_kind": drift["source_kind"], "reason": reason,
                   "fields": drift["fields"], "incoming": drift["incoming"], "source": drift["source"], "at": now}
    if payload is not None and not order.get("conflict"):
        upd["conflict"] = payload
    note = f"{text}（采购未完成，已按来源最新内容转人工核实）"
    existing = order.get("attention_note") or ""
    upd["needs_attention"] = True
    upd["attention_note"] = existing if note in existing else (f"{existing}；{note}" if existing else note)[:1000]
    events.record(conn, order["workspace_id"], "order", order["id"], "order.source_changed_during_attempt",
                  actor_kind=actor_kind, actor_id=actor_id,
                  detail={"attempt_id": att["id"], "reason": text, "fields": drift["fields"],
                          "review": drift["review"], "external": drift["external"], "source": drift["source"]})
    return upd


def apply_source_drift(conn, order: dict[str, Any], att: dict[str, Any], *, actor_kind: str,
                       actor_id: int | None) -> dict[str, Any]:
    """输入：采购没有完成就回到开始前或异常的任务（已锁）、刚结束的尝试、操作者 → 输出：任务行。

    把领取后来源的执行相关变化落到任务上（见本节说明），再按派生规则重新定状态：人审撤回 → pending_review，
    外部单号 / 字段变化、或执行期间来源写在任务上的暂停（conflict）→ conflict；异常的任务保持异常（恢复时
    再派生）。其他状态原样返回。
    """
    if order["status"] not in PRE_EXECUTION_STATUSES and order["status"] != "exception":
        return order
    upd = drift_updates(conn, order, att, actor_kind=actor_kind, actor_id=actor_id) or {}
    if order["status"] == "exception":
        if not upd:
            return order
        return orders.transition(conn, order, "exception", actor_kind=actor_kind, actor_id=actor_id, updates=upd)
    merged = {**order, **upd}
    target = orders.derive_status(merged)
    if target == order["status"]:
        return orders.transition(conn, order, target, actor_kind=actor_kind, actor_id=actor_id, updates=upd) \
            if upd else order
    return orders.transition(conn, order, target, reason=orders.derived_reason(merged, target), actor_kind=actor_kind,
                             actor_id=actor_id, updates=upd,
                             event_detail={"attempt_id": att["id"], "source_changed": True})


# ── 任务落地（尝试结束后任务去哪）─────────────────────────────────────────────


def _owns(order: dict[str, Any], att: dict[str, Any]) -> bool:
    return order["status"] == "executing" and order["current_attempt_id"] == att["id"]


def _cancel_reason(order: dict[str, Any]) -> str:
    note = order.get("upstream_cancel_note")
    return f"上游取消（{note}），采购已中止（未提交）" if note else "上游取消，采购已中止（未提交）"


def _release(conn, order: dict[str, Any], att: dict[str, Any], reason: str, *, actor_kind: str,
             actor_id: int | None, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    """输入：任务、未登记提交就结束的尝试、原因 → 输出：任务行。上游已取消 → cancelled；否则回 assigned（保留分配），
    再把领取后被挡住的来源变化落到任务上（apply_source_drift：人审撤回 → 待确认，外部单号 / 字段变化 → 待核实）。"""
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
    row = orders.transition(conn, order, "assigned", reason=reason, actor_kind=actor_kind, actor_id=actor_id,
                            updates=upd, event_detail=detail)
    return apply_source_drift(conn, row, att, actor_kind=actor_kind, actor_id=actor_id)


def _to_exception(conn, order: dict[str, Any], att: dict[str, Any], reason: str, *, actor_kind: str,
                  actor_id: int | None) -> dict[str, Any]:
    """输入：任务、未登记提交就失败的尝试、异常原因 → 输出：任务行（上游已取消 → cancelled，否则 exception；
    领取后被挡住的来源变化同样落到任务上，恢复时按派生规则重新定状态）。"""
    if not _owns(order, att):
        return order
    if order["upstream_cancelled"]:
        return _release(conn, order, att, reason, actor_kind=actor_kind, actor_id=actor_id)
    row = orders.transition(conn, order, "exception", reason=reason, actor_kind=actor_kind, actor_id=actor_id,
                            updates={"current_attempt_id": None}, event_detail={"attempt_id": att["id"]})
    return apply_source_drift(conn, row, att, actor_kind=actor_kind, actor_id=actor_id)


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
    execution_gate(conn, ws)
    if dev["bound_account_id"] is None:
        raise DomainError("NOT_BOUND", "设备还没有绑定买家账号，请先在控制台「账号与设备」里绑定", 403)
    acc = _account_row(conn, ws, dev["bound_account_id"])
    if acc is None or acc["archived_at"] is not None:
        raise DomainError("NOT_BOUND", "设备绑定的买家账号已归档，请在控制台重新绑定", 403)
    problem = _identity_problem(dev, acc)
    now = clock.now()
    if problem is not None:
        raise DomainError("IDENTITY_MISMATCH", problem[1], 403,
                          {"identity_status": problem[0], "identity_at": _iso(dev["identity_at"]), "stale": False})
    stale = _identity_stale(dev)
    if stale is not None:
        raise DomainError("IDENTITY_MISMATCH", f"{stale}，请打开或刷新亚马逊页面后重试", 403,
                          {"identity_status": "match", "identity_at": _iso(dev["identity_at"]), "stale": True})
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
    """输入：任务行 → 输出：新尝试的种类（尝试种类的唯一规则）。

    - 'retry'：页面错误的自动重试，即最近一次本系统尝试因 PAGE_LOAD_FAILED 失败、任务被放回已分配
      （page_load_retries > 0；人工恢复异常会清零，之后算 'auto'）；
    - 'repurchase'：任务曾有成功尝试（含来源带来的外部单号）——只有亚马逊取消后同意重采才会再次被领取；
    - 其余 'auto'。
    """
    rows = conn.execute(
        "SELECT kind, status, error_code FROM procure.purchase_attempts WHERE purchase_order_id = %s "
        "AND workspace_id = %s ORDER BY attempt_no DESC",
        (order["id"], order["workspace_id"]),
    ).fetchall()
    own = [r for r in rows if r["kind"] != "external"]
    if (own and own[0]["status"] == "failed" and own[0]["error_code"] == "PAGE_LOAD_FAILED"
            and (order.get("page_load_retries") or 0) > 0):
        return "retry"
    if any(r["status"] == "succeeded" for r in rows):
        return "repurchase"
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
        "price_cap": _m(money_cap(orders.price_cap_eff(order))),
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
    # 下发给插件的是 payload；库里另存来源基线，供提交前与采购未完成时判断「领取后上游改了什么」
    _set_attempt(conn, att, task_snapshot={**payload, "source_baseline": _source_baseline(conn, order)})
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


_CLAIMABLE_WHERE = """
    o.workspace_id = %s AND o.assigned_account_id = %s AND o.status = 'assigned'
    AND o.suggested_purchase_date <= %s AND o.review_status = 'approved' AND NOT o.upstream_cancelled
    AND o.conflict IS NULL AND NOT (o.id = ANY(%s::bigint[]))
    AND NOT EXISTS (SELECT 1 FROM procure.purchase_attempts pa
                    WHERE pa.purchase_order_id = o.id AND pa.workspace_id = o.workspace_id
                      AND pa.status = ANY(%s))
"""
#: 候选任务全被别的事务锁着（导入、同步、分配进行中）时，建议插件多少秒后再领取
CLAIM_BUSY_RETRY_S = 5


def next_attempt(conn, device: dict[str, Any], run_id: Any) -> dict[str, Any]:
    """输入：设备、批次 id → 输出：{attempt: 载荷 | None, reason}（契约 §3.3、§5 第 6 步）；候选任务只是被别的
    事务锁着时另有 retry_after_s（插件稍后再领，不要结束批次）。

    本批次已有在途尝试 → ATTEMPT_IN_FLIGHT（409）。账号已暂停 / 已归档 / 身份不符或超过 identity_fresh_s 没有
    上报 / 当天额度已满（used(账号, 北京今天, 本批次的模拟标记) ≥ 每日上限，在途、待核实、成功都占用）/
    账号在别处还有在途尝试时不领取，返回原因。领取条件：已分配给本账号、建议采购日期 ≤ 北京今天、人审通过、
    未上游取消、无冲突、金额仍在账号区间内；顺序：建议采购日期、下单时间、id；FOR UPDATE SKIP LOCKED。
    金额已不在区间的任务解除分配回待分配并跳过。
    """
    dev = devices.require_active(conn, device)
    execution_gate(conn, dev["workspace_id"])
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
    stale = _identity_stale(dev)
    if stale is not None:
        return {"attempt": None, "reason": f"{stale}，停止领取（请打开或刷新亚马逊页面后重新开始）"}
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
            f"SELECT o.* FROM procure.purchase_orders o WHERE {_CLAIMABLE_WHERE} "
            "ORDER BY o.suggested_purchase_date, o.order_time NULLS LAST, o.id LIMIT 20 FOR UPDATE OF o SKIP LOCKED",
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
    # SKIP LOCKED 分不清「没有」和「被锁着」：不加锁再数一遍（普通读不等行锁）
    locked = conn.execute(f"SELECT count(*) AS n FROM procure.purchase_orders o WHERE {_CLAIMABLE_WHERE}",
                          (ws, acc["id"], today, skipped, _INFLIGHT)).fetchone()["n"]
    if locked:
        return {"attempt": None, "retry_after_s": CLAIM_BUSY_RETRY_S,
                "reason": f"有 {locked} 笔可执行的任务正被其他操作（导入、同步或分配）占用，"
                          f"请 {CLAIM_BUSY_RETRY_S} 秒后再领取"}
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

    未登记提交时，上游已取消 / 账号已暂停或归档 / 身份不符或已过期 / 领取后来源有执行相关变化（外部 AMZ
    订单号、人审结论、执行相关字段，见 _source_drift）→ continue=false（插件随后上报中止）；尝试已结束 →
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
            stale = _identity_stale(dev) if problem is None else None
            hold = _hold_reason(order) if problem is None and stale is None else None
            drift = _source_drift(conn, order, att) if problem is None and stale is None and hold is None else None
            if problem is not None:
                cancel = problem[1]
            elif stale is not None:
                cancel = stale
            elif hold is not None:
                cancel = f"{hold}，未提交前中止"
            elif drift is not None:
                cancel = f"{_drift_text(drift)}，未提交前中止"
    cols: dict[str, Any] = {"stage": st, "heartbeat_at": clock.now()}
    if att["status"] == "claimed":
        cols["status"] = "checking"
    _set_attempt(conn, att, **cols)
    events.record(conn, att["workspace_id"], "attempt", att["id"], "attempt.progress", actor_kind="device",
                  actor_id=dev["id"], order_id=order["id"],
                  detail={"stage": st, "detail": _small(detail) if detail is not None else None,
                          "continue": cancel is None, "cancel_reason": cancel})
    return {"continue": cancel is None, "cancel_reason": cancel}


def _evaluate_guard(conn, facts: dict[str, Any], order: dict[str, Any], acc: dict[str, Any]) -> dict[str, Any]:
    """输入：规范化后的结算页事实、任务、账号 → 输出：按任务与账号**当前**有效值（限价、金额区间、有效配送上限、
    期望卡尾号）与站点今天得到的护栏裁决。"""
    return guard.evaluate(
        facts, asin=orders.asin_eff(order), quantity=orders.quantity_eff(order), postal_code=order["postal_code"],
        price_cap=orders.price_cap_eff(order), amount_min=acc["min_amount"], amount_max=acc["max_amount"],
        max_delivery_days=accounts.effective_max_delivery_days(conn, order["workspace_id"], acc),
        expected_card_last4=acc["expected_card_last4"], site_today=clock.site_today(acc["site"]),
    )


def submit_guard(conn, device: dict[str, Any], attempt_id: Any, facts: Any) -> dict[str, Any]:
    """输入：设备、尝试、结算页事实 → 输出：护栏裁决（services/guard.evaluate 的结果）。

    事实先经 guard.sanitize_facts 规范化（只留契约 §4 的键、卡号只留后 4 位），判定与入库都用规范化后的事实。
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
    guard_input = guard.sanitize_facts(facts)
    verdict = _evaluate_guard(conn, guard_input, order, acc)
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

    拒绝（409，不改任何状态）：护栏未放行 / 尝试不在 guard_passed、批次失效（LEASE_LOST）、任务已取消或状态
    已变、账号已归档或暂停（ACCOUNT_PAUSED）、身份不符或超过 identity_fresh_s 没有上报（IDENTITY_MISMATCH，
    detail.stale）、领取后来源有执行相关变化（SOURCE_CHANGED）、按最新配置（任务的有效限价、账号金额区间、
    有效配送上限、期望卡尾号、站点今天）对存下的结算事实复核护栏不再放行（GUARD_BLOCKED，detail 带
    block_code / reasons）。
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
    stale = _identity_stale(dev)
    if stale is not None:
        raise conflict(f"{stale}，不能提交", code="IDENTITY_MISMATCH", identity_status="match", stale=True,
                       identity_at=_iso(dev["identity_at"]))
    hold = _hold_reason(order)
    if hold is not None:
        raise conflict(f"{hold}，不能提交", code="SOURCE_CHANGED", conflict_kind=(order["conflict"] or {}).get("kind"))
    drift = _source_drift(conn, order, att)
    if drift is not None:
        raise conflict(f"{_drift_text(drift)}，不能提交", code="SOURCE_CHANGED", fields=sorted(drift["fields"]),
                       review=drift["review"], external=drift["external"])
    recheck = _evaluate_guard(conn, att["guard_input"] if isinstance(att["guard_input"], dict) else {}, order, acc)
    if recheck["verdict"] != "pass":
        why = recheck["reasons"][0] if recheck["reasons"] else (
            ERROR_CODES[recheck["code"]].zh if recheck["code"] in ERROR_CODES else "护栏拦截")
        raise conflict(f"按最新配置复核护栏未通过：{why}，不能提交", code="GUARD_BLOCKED",
                       block_code=recheck["code"], reasons=recheck["reasons"])
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
        try:
            d = None if isinstance(v, bool) or not isinstance(v, (str, int, float, Decimal)) else money(v)
        except (InvalidOperation, ValueError, OverflowError):    # 位数大到无法取两位小数 → 读不出
            d = None
        amounts[k] = d if d is not None and d >= 0 and d < _MAX_AMOUNT else None
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


_OUTCOME_ZH = {"succeeded": "成功", "failed": "失败", "unknown": "结果不明"}


def _late_receipt(conn, dev: dict[str, Any], att: dict[str, Any], order: dict[str, Any],
                  r: dict[str, Any]) -> dict[str, Any]:
    """输入：已是「结果待核实」（unknown）的尝试、它的任务、迟到的规范化结果 → 输出：
    {attempt_status: 'unknown', order_status, late_receipt: True, message}。

    例如清扫已把尝试判为待核实后，插件才送到成功回执。**不改**尝试与任务的状态、不恢复账号、不自动核实；
    回执补进尝试上还空着的列（亚马逊订单号、金额、卡尾号、配送），完整回执写 'attempt.late_result' 事件，
    任务的状态说明与提醒补一句，供人工核实时参考。调用方按正常返回提交事务（插件路由回 409）。
    """
    ws = att["workspace_id"]
    ak, aid = _actor_of(dev)
    what = _OUTCOME_ZH.get(r["outcome"], r["outcome"])
    out = {"attempt_status": "unknown", "order_status": order["status"], "late_receipt": True,
           "message": f"该采购尝试已登记为「结果待核实」，迟到的结果（{what}）已记录，请在控制台核实"}
    seen = conn.execute(
        "SELECT detail FROM procure.events WHERE workspace_id = %s AND entity = 'attempt' AND entity_id = %s "
        "AND kind = 'attempt.late_result' ORDER BY id DESC LIMIT 1",
        (ws, att["id"]),
    ).fetchone()
    if seen is not None and all((seen["detail"] or {}).get(k) == r[k]
                                for k in ("outcome", "amazon_order_no", "error_code")):
        return out          # 插件补交同一份回执：不重复记录
    cols: dict[str, Any] = {}
    if r["amazon_order_no"] and not att["amazon_order_no"]:
        cols["amazon_order_no"] = r["amazon_order_no"]
    for k, v in r["amounts"].items():
        if v is not None and att[k] is None:
            cols[k] = v
    for k in ("card_last4", "delivery_text", "delivery_date", "delivery_days"):
        if r[k] is not None and att[k] is None:
            cols[k] = r[k]
    if cols:
        _set_attempt(conn, att, **cols)
    note = f"插件迟到上报的结果：{what}"
    if r["amazon_order_no"]:
        note += f"，亚马逊订单号 {r['amazon_order_no']}"
    elif r["error_code"]:
        note += f"（{r['error_code']}）"
    note += "，请据此核实"
    if order["status"] == "result_unknown" and order["current_attempt_id"] == att["id"]:
        existing = order["status_reason"] or ""
        reason = existing if note in existing else (f"{existing}；{note}" if existing else note)[:1000]
        attention = order["attention_note"] or ""
        attention = attention if note in attention else (f"{attention}；{note}" if attention else note)[:1000]
        order = orders.transition(conn, order, "result_unknown", reason=reason, actor_kind=ak, actor_id=aid,
                                  updates={"needs_attention": True, "attention_note": attention})
    events.record(conn, ws, "attempt", att["id"], "attempt.late_result", actor_kind=ak, actor_id=aid,
                  order_id=order["id"],
                  detail={"outcome": r["outcome"], "amazon_order_no": r["amazon_order_no"],
                          "error_code": r["error_code"], "error_detail": r["error_detail"],
                          "amounts": {k: _m(v) for k, v in r["amounts"].items()}, "card_last4": r["card_last4"],
                          "delivery_text": r["delivery_text"], "delivery_date": r["delivery_date"],
                          "delivery_days": r["delivery_days"], "reason": note})
    return {**out, "order_status": order["status"]}


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
    - 已结束的尝试再次上报：结局与单号 / 错误码都相同 → 原样返回；已是「结果待核实」而本次不同（迟到的回执，
      如清扫之后才送到的成功）→ 不改状态，回执记进尝试与 'attempt.late_result' 事件，返回 late_receipt: true
      （见 _late_receipt；插件路由回 409 但提交事务）；其他不同 → CONFLICT。
    - 已登记提交的在途尝试：批次租约过期（尚未清扫）也照常接收结果，回执不丢；未登记提交的要求批次有效。
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
        if att["status"] == "unknown":
            return _late_receipt(conn, dev, att, order, r)
        raise conflict(
            f"该采购尝试的结果已登记为「{ATTEMPT_STATUS_ZH.get(att['status'], att['status'])}」，与本次上报不一致",
            attempt_status=att["status"], reported=r["outcome"])
    if att["submitted_at"] is None:
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
