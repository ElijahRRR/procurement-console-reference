"""买家账号：配置校验、列表与详情（含当天额度、绑定设备、当前执行批次）、暂停 / 恢复执行、采用插件识别的 ID。

- 账号名在工作区内唯一（含已归档的，库里有唯一约束）；跨工作区访问一律 NOT_FOUND。
- 影响分配的配置变化（金额区间、每日额度、优先级、采购开关、站点）以及新建、恢复执行、归档后，
  立即按今天重新分配（allocation.allocate(trigger='account_change')）。暂停执行不重新分配：
  暂停的账号不参与自动分配，已分配给它的任务保留。
- 在线 = 3 分钟内有心跳。
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from typing import Any

from services import clock, events, quota, settings_svc
from services.errors import conflict, not_found, validation
from services.models import ATTEMPT_INFLIGHT, money

ONLINE_WINDOW = timedelta(minutes=3)
CUSTOMER_ID_RE = re.compile(r"^A[0-9A-Z]{8,}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CARD_RE = re.compile(r"^[0-9]{4}$")

#: 前端可能整份回传的只读键，忽略
_READONLY = frozenset({"id", "workspace_id", "created_at", "updated_at", "archived_at", "quota_today", "devices",
                       "active_run", "effective_max_delivery_days", "execution_paused", "pause_reason", "paused_at"})
_EDITABLE = ("name", "amazon_customer_id", "site", "min_amount", "max_amount", "daily_limit", "priority",
             "max_delivery_days", "purchase_enabled", "sync_enabled", "purchase_mode", "schedule_times",
             "default_device_id", "expected_card_last4", "notes")
#: 这些字段变化会改变分配结果
ALLOCATION_FIELDS = frozenset({"min_amount", "max_amount", "daily_limit", "priority", "purchase_enabled", "site"})


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
        elif key == "default_device_id":
            if v is None:
                out[key] = None
            else:
                did = _int_field(v, "默认执行设备", key, 1, 2 ** 62)
                dev = conn.execute(
                    "SELECT id FROM procure.devices WHERE id = %s AND workspace_id = %s AND revoked_at IS NULL",
                    (did, ws),
                ).fetchone()
                if dev is None:
                    raise validation("默认执行设备不存在或已撤销", field=key)
                out[key] = did
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
    """输入：工作区、账号 id、是否加行锁 → 输出：buyer_accounts 行（含已归档）；不存在或跨工作区 → NOT_FOUND。"""
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        raise not_found("买家账号") from None
    row = conn.execute(
        "SELECT * FROM procure.buyer_accounts WHERE id = %s AND workspace_id = %s" + (" FOR UPDATE" if for_update else ""),
        (aid, workspace_id),
    ).fetchone()
    if row is None:
        raise not_found("买家账号")
    return row


def effective_max_delivery_days(conn, workspace_id: int, account_row: dict[str, Any]) -> int:
    """输入：工作区、账号行 → 输出：有效最长配送天数（账号覆盖值，否则全局默认）。"""
    if account_row.get("max_delivery_days") is not None:
        return int(account_row["max_delivery_days"])
    return settings_svc.default_max_delivery_days(conn, workspace_id)


def _m(v: Any) -> str | None:
    return None if v is None else f"{Decimal(v):.2f}"


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _shape(conn, ws: int, row: dict[str, Any], *, simulated: bool, default_days: int) -> dict[str, Any]:
    now = clock.now()
    devs = conn.execute(
        "SELECT id, name, last_seen_at, identity_status FROM procure.devices "
        "WHERE workspace_id = %s AND bound_account_id = %s AND revoked_at IS NULL ORDER BY name, id",
        (ws, row["id"]),
    ).fetchall()
    run = conn.execute(
        """
        SELECT r.id, r.device_id, d.name AS device_name, r.started_at
        FROM procure.execution_runs r JOIN procure.devices d ON d.id = r.device_id
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
        "paused_at": _iso(row["paused_at"]), "purchase_mode": row["purchase_mode"],
        "schedule_times": list(row["schedule_times"] or []), "default_device_id": row["default_device_id"],
        "expected_card_last4": row["expected_card_last4"], "notes": row["notes"],
        "quota_today": quota.summary(conn, row, clock.bj_today(), simulated),
        "devices": [{"id": d["id"], "name": d["name"],
                     "online": d["last_seen_at"] is not None and now - d["last_seen_at"] <= ONLINE_WINDOW,
                     "identity_status": d["identity_status"], "last_seen_at": _iso(d["last_seen_at"])} for d in devs],
        "active_run": ({"id": run["id"], "device_id": run["device_id"], "device_name": run["device_name"],
                        "started_at": _iso(run["started_at"])} if run else None),
        "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
        "archived_at": _iso(row["archived_at"]),
    }


def get(conn, workspace_id: int, account_id: int) -> dict[str, Any]:
    """输入：工作区、账号 id → 输出：契约 §10 账号形状（含 quota_today、devices、active_run）。"""
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


def create(conn, workspace_id: int, data: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、账号字段、操作人 → 输出：账号（契约 §10）。新建后立即重新分配。"""
    vals = _validate(conn, workspace_id, data, None)
    now = clock.now()
    vals.update(workspace_id=workspace_id, created_at=now, updated_at=now)
    names = list(vals)
    row = conn.execute(
        f"INSERT INTO procure.buyer_accounts ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) "
        "RETURNING *",
        list(vals.values()),
    ).fetchone()
    ak, aid = _actor(actor_user_id)
    events.record(conn, workspace_id, "account", row["id"], "account.created", actor_kind=ak, actor_id=aid,
                  detail={k: v for k, v in vals.items() if k not in ("workspace_id", "created_at", "updated_at")})
    if row["purchase_enabled"]:
        _reallocate(conn, workspace_id, ak, aid)
    return get(conn, workspace_id, row["id"])


def update(conn, workspace_id: int, account_id: int, patch: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、账号、要改的字段、操作人 → 输出：改后的账号。影响分配的字段变化后立即重新分配。"""
    row = get_row(conn, workspace_id, account_id, for_update=True)
    if row["archived_at"] is not None:
        raise validation("账号已归档，不能修改")
    vals = _validate(conn, workspace_id, patch, row)
    changes = {k: [row[k], v] for k, v in vals.items() if row[k] != v and not (
        isinstance(v, list) and list(row[k] or []) == v)}
    if not changes:
        return get(conn, workspace_id, account_id)
    sets = ", ".join(f"{k} = %s" for k in changes)
    conn.execute(
        f"UPDATE procure.buyer_accounts SET {sets}, updated_at = %s WHERE id = %s AND workspace_id = %s",
        [*(v[1] for v in changes.values()), clock.now(), account_id, workspace_id],
    )
    ak, aid = _actor(actor_user_id)
    events.record(conn, workspace_id, "account", account_id, "account.updated", actor_kind=ak, actor_id=aid,
                  detail={"changes": changes})
    if ALLOCATION_FIELDS & set(changes):
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
    """
    row = get_row(conn, workspace_id, account_id, for_update=True)
    if row["archived_at"] is not None:
        return get(conn, workspace_id, account_id)
    busy = conn.execute(
        "SELECT (SELECT count(*) FROM procure.execution_runs WHERE workspace_id = %s AND account_id = %s "
        "        AND ended_at IS NULL) AS runs, "
        "       (SELECT count(*) FROM procure.purchase_attempts WHERE workspace_id = %s AND account_id = %s "
        "        AND status = ANY(%s)) AS attempts",
        (workspace_id, account_id, workspace_id, account_id, sorted(ATTEMPT_INFLIGHT | {"unknown"})),
    ).fetchone()
    if busy["runs"] or busy["attempts"]:
        raise conflict("账号正在执行或有待核实的采购，不能归档")
    from services import orders

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
    conn.execute(
        "UPDATE procure.devices SET bound_account_id = NULL WHERE workspace_id = %s AND bound_account_id = %s",
        (workspace_id, account_id),
    )
    events.record(conn, workspace_id, "account", account_id, "account.archived", actor_kind=ak, actor_id=aid)
    _reallocate(conn, workspace_id, ak, aid)
    return get(conn, workspace_id, account_id)


def adopt_customer_id(conn, workspace_id: int, account_id: int, device_id: int,
                      actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、账号、设备、操作人 → 输出：账号。把该设备最近识别到的 customer_id 写成账号的买家号 ID。

    设备须属于同一工作区（否则 NOT_FOUND）、未撤销、已绑定该账号，且识别到的 ID 格式正确、未被其他账号占用。
    采用后该设备的身份状态即为 match。
    """
    account = get_row(conn, workspace_id, account_id, for_update=True)
    if account["archived_at"] is not None:
        raise validation("账号已归档，不能修改")
    try:
        did = int(device_id)
    except (TypeError, ValueError):
        raise not_found("设备") from None
    dev = conn.execute("SELECT * FROM procure.devices WHERE id = %s AND workspace_id = %s",
                       (did, workspace_id)).fetchone()
    if dev is None:
        raise not_found("设备")
    if dev["revoked_at"] is not None:
        raise validation("设备已撤销")
    if dev["bound_account_id"] != account_id:
        raise validation("该设备没有绑定此账号，不能采用它识别的买家号 ID")
    identity = dev["identity"] or {}
    cid = identity.get("customer_id") if isinstance(identity, dict) else None
    cid = cid.strip().upper() if isinstance(cid, str) else None
    if not cid or not CUSTOMER_ID_RE.match(cid):
        raise validation("插件没有识别到格式正确的买家号 ID")
    _check_customer_id_unique(conn, workspace_id, cid, account_id)
    now = clock.now()
    conn.execute("UPDATE procure.buyer_accounts SET amazon_customer_id = %s, updated_at = %s "
                 "WHERE id = %s AND workspace_id = %s", (cid, now, account_id, workspace_id))
    conn.execute("UPDATE procure.devices SET identity_status = 'match' WHERE id = %s AND workspace_id = %s "
                 "AND identity_status <> 'signed_out'", (did, workspace_id))
    ak, aid = _actor(actor_user_id)
    events.record(conn, workspace_id, "account", account_id, "account.customer_id_adopted", actor_kind=ak,
                  actor_id=aid, detail={"device_id": did, "customer_id": cid,
                                        "previous": account["amazon_customer_id"]})
    return get(conn, workspace_id, account_id)
