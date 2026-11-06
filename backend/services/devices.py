"""插件设备：配对码与设备令牌、心跳与身份识别、设备列表与绑定、撤销（契约 §5.1–5.3、§10「设备」「插件心跳」）。

- **配对**：控制台生成 8 位配对码（字母表去掉易混的 0/O、1/I/L），库里只存 sha256；
  有效期取 registry 的 pairing_ttl_s（默认 10 分钟），一次性。插件凭码换设备令牌，令牌也只存 sha256。
- **认证**：authenticate(令牌) 找到未撤销的设备行（含 workspace_id）；工作区由设备决定，插件请求不需要也
  不允许指定工作区。
- **身份**：插件上报 {signed_in, customer_id, site, page_url}，与绑定账号的 amazon_customer_id 比对：
  match / mismatch / signed_out / unknown（未绑定、账号未登记买家号 ID、或插件没识别到 ID）。
  身份连同服务端时刻 at 存进 devices.identity；状态变化写 'device.identity_changed' 事件。
  执行模块按「存下的身份 × 账号当前登记的 ID」重新计算有效状态（effective_identity_status），
  控制台改了账号的买家号 ID 后不必等下一次心跳。
- **在线** = 3 分钟（ONLINE_WINDOW_S）内有心跳。
- **改绑**：设备有未结束的执行批次时拒绝改绑（CONFLICT）；改绑后按新账号重算身份状态。
- **撤销**：令牌哈希置空、记撤销时间；它未结束的执行批次按租约过期同样处理（未登记提交的尝试中止、
  任务回已分配；已登记提交的进结果待核实并暂停账号），见 services/execution.end_run。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from registry import settings
from services import accounts, clock, events, settings_svc
from services.errors import DomainError, conflict, not_found, validation
from services.orders import UNSET

ONLINE_WINDOW_S = 180
PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LEN = 8
BROWSERS = ("chrome", "adspower", "other")

#: 心跳里账号配置要给插件的键（契约 §10「插件心跳 / 配置」）
HEARTBEAT_ACCOUNT_KEYS = (
    "id", "name", "amazon_customer_id", "purchase_mode", "schedule_times", "effective_max_delivery_days",
    "purchase_enabled", "execution_paused", "pause_reason", "min_amount", "max_amount", "daily_limit",
    "expected_card_last4", "quota_today",
)

__all__ = [
    "ONLINE_WINDOW_S", "UNSET", "authenticate", "compute_identity_status", "create_pairing_code",
    "effective_identity_status", "get_device", "heartbeat", "list_devices", "pair", "report_identity",
    "require_active", "revoke", "update_device",
]


# ── 小工具 ────────────────────────────────────────────────────────────────────


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _short(v: Any, n: int) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] or None


def _norm_code(code: Any) -> str:
    """输入：用户输入的配对码（可小写、带空格或连字符）→ 输出：大写字母数字串。"""
    if not isinstance(code, str):
        return ""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _clean_name(v: Any) -> str:
    if not isinstance(v, str) or not v.strip():
        raise validation("设备名称不能为空", field="name")
    s = v.strip()
    if len(s) > 64:
        raise validation("设备名称不能超过 64 个字", field="name")
    return s


def _browser(v: Any) -> str | None:
    s = _short(v, 32)
    if s is None:
        return None
    s = s.lower()
    return s if s in BROWSERS else "other"


def _int_id(v: Any, what: str) -> int:
    if isinstance(v, bool):
        raise not_found(what)
    try:
        return int(v)
    except (TypeError, ValueError):
        raise not_found(what) from None


def _account(conn, workspace_id: int, account_id: int | None) -> dict[str, Any] | None:
    if account_id is None:
        return None
    return conn.execute("SELECT * FROM procure.buyer_accounts WHERE id = %s AND workspace_id = %s",
                        (account_id, workspace_id)).fetchone()


# ── 配对与认证 ────────────────────────────────────────────────────────────────


def create_pairing_code(conn, workspace_id: int, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、操作人 → 输出：{code, expires_at}（8 位一次性配对码；库里只存哈希）。"""
    now = clock.now()
    expires = now + timedelta(seconds=settings.get().pairing_ttl_s)
    code = None
    for _ in range(8):
        candidate = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LEN))
        row = conn.execute(
            "INSERT INTO procure.device_pairings (code_hash, workspace_id, created_by, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (code_hash) DO NOTHING RETURNING code_hash",
            (_sha(candidate), workspace_id, actor_user_id, now, expires),
        ).fetchone()
        if row is not None:
            code = candidate
            break
    if code is None:  # pragma: no cover - 31^8 的空间里连撞 8 次
        raise RuntimeError("生成配对码失败")
    events.record(conn, workspace_id, "device", None, "device.pairing_code_created",
                  actor_kind="user" if actor_user_id is not None else "system", actor_id=actor_user_id,
                  detail={"expires_at": expires})
    return {"code": code, "expires_at": expires.isoformat()}


def pair(conn, code: Any, *, device_name: Any, browser: Any, ext_version: Any,
         user_agent: Any = None) -> dict[str, Any]:
    """输入：配对码、设备名、浏览器、插件版本、UA → 输出：{device_id, device_token, workspace_name}。

    配对码不存在、已用过或已过期 → VALIDATION「配对码无效或已过期」。设备名校验失败时配对码不被消耗。
    """
    name = _clean_name(device_name)
    br = _browser(browser)
    ver = _short(ext_version, 32)
    ua = _short(user_agent, 500)
    norm = _norm_code(code)
    invalid = validation("配对码无效或已过期", field="code")
    if len(norm) != PAIRING_CODE_LEN:
        raise invalid
    now = clock.now()
    p = conn.execute("SELECT * FROM procure.device_pairings WHERE code_hash = %s FOR UPDATE",
                     (_sha(norm),)).fetchone()
    if p is None or p["used_at"] is not None or p["expires_at"] <= now:
        raise invalid
    ws = p["workspace_id"]
    token = secrets.token_urlsafe(32)
    dev = conn.execute(
        """
        INSERT INTO procure.devices (workspace_id, name, browser, token_hash, ext_version, user_agent, last_seen_at,
                                     identity_status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'unknown', %s) RETURNING *
        """,
        (ws, name, br, _sha(token), ver, ua, now, now),
    ).fetchone()
    conn.execute("UPDATE procure.device_pairings SET used_at = %s, device_id = %s WHERE code_hash = %s",
                 (now, dev["id"], p["code_hash"]))
    ws_row = conn.execute("SELECT name FROM procure.workspaces WHERE id = %s", (ws,)).fetchone()
    events.record(conn, ws, "device", dev["id"], "device.paired", actor_kind="device", actor_id=dev["id"],
                  detail={"name": name, "browser": br, "ext_version": ver, "code_created_by": p["created_by"]})
    return {"device_id": dev["id"], "device_token": token, "workspace_name": ws_row["name"] if ws_row else None}


def authenticate(conn, token: Any) -> dict[str, Any] | None:
    """输入：设备令牌 → 输出：未撤销的设备行（含 workspace_id）；令牌为空、未知或已撤销 → None。"""
    if not isinstance(token, str) or not token.strip():
        return None
    return conn.execute("SELECT * FROM procure.devices WHERE token_hash = %s AND revoked_at IS NULL",
                        (_sha(token.strip()),)).fetchone()


def require_active(conn, device: dict[str, Any]) -> dict[str, Any]:
    """输入：设备行（authenticate 的结果，可能已过时）→ 输出：库里最新的设备行；已撤销 → UNAUTHORIZED（401）。"""
    row = conn.execute("SELECT * FROM procure.devices WHERE id = %s AND workspace_id = %s",
                       (device["id"], device["workspace_id"])).fetchone()
    if row is None:
        raise not_found("设备")
    if row["revoked_at"] is not None or row["token_hash"] is None:
        raise DomainError("UNAUTHORIZED", "设备授权已撤销，请重新配对", 401)
    return row


# ── 身份 ──────────────────────────────────────────────────────────────────────


def compute_identity_status(identity: dict[str, Any] | None, account_row: dict[str, Any] | None) -> str:
    """输入：插件上报的身份、绑定账号行 → 输出：match / mismatch / signed_out / unknown。

    未登录 → signed_out；未绑定 / 账号已归档 / 账号未登记买家号 ID / 插件没识别到 ID → unknown；
    ID 相同（不分大小写）→ match，否则 mismatch。
    """
    if not isinstance(identity, dict) or not identity:
        return "unknown"
    if not identity.get("signed_in"):
        return "signed_out"
    if account_row is None or account_row.get("archived_at") is not None:
        return "unknown"
    expected = account_row.get("amazon_customer_id")
    got = identity.get("customer_id")
    if not expected or not isinstance(got, str) or not got.strip():
        return "unknown"
    return "match" if got.strip().upper() == str(expected).strip().upper() else "mismatch"


def effective_identity_status(device_row: dict[str, Any], account_row: dict[str, Any] | None) -> str:
    """输入：设备行、它绑定的账号行 → 输出：有效身份状态（有存下的身份就按账号当前 ID 重算，否则用存下的状态）。"""
    if isinstance(device_row.get("identity"), dict) and device_row["identity"]:
        return compute_identity_status(device_row["identity"], account_row)
    return device_row.get("identity_status") or "unknown"


def _clean_identity(identity: Any) -> dict[str, Any]:
    if not isinstance(identity, dict):
        raise validation("身份信息格式不对", field="identity")
    cid = _short(identity.get("customer_id"), 64)
    url = _short(identity.get("page_url"), 2000)
    if url:
        parts = urlsplit(url)
        # 只留 origin + path：不存查询串与片段（可能带会话参数）
        url = (f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme and parts.netloc
               else url.split("?", 1)[0].split("#", 1)[0])[:500] or None
    return {
        "signed_in": bool(identity.get("signed_in")),
        "customer_id": cid.upper() if cid else None,
        "site": _short(identity.get("site"), 8),
        "page_url": url,
    }


def report_identity(conn, device: dict[str, Any], identity: Any) -> str:
    """输入：设备行、插件上报的身份 {signed_in, customer_id, site, page_url} → 输出：身份状态。

    存下身份（附服务端时刻 at）、状态与 identity_at；状态变化时写 'device.identity_changed' 事件。
    """
    dev = require_active(conn, device)
    ident = _clean_identity(identity)
    now = clock.now()
    ident["at"] = now.isoformat()
    ws = dev["workspace_id"]
    acc = _account(conn, ws, dev["bound_account_id"])
    status = compute_identity_status(ident, acc)
    conn.execute(
        "UPDATE procure.devices SET identity = %s, identity_status = %s, identity_at = %s "
        "WHERE id = %s AND workspace_id = %s",
        (Jsonb(ident), status, now, dev["id"], ws),
    )
    if status != dev["identity_status"]:
        events.record(conn, ws, "device", dev["id"], "device.identity_changed", actor_kind="device",
                      actor_id=dev["id"],
                      detail={"from": dev["identity_status"], "to": status, "customer_id": ident["customer_id"],
                              "signed_in": ident["signed_in"], "account_id": dev["bound_account_id"],
                              "expected_customer_id": acc["amazon_customer_id"] if acc else None})
    return status


# ── 心跳 ──────────────────────────────────────────────────────────────────────


def _active_run_of_account(conn, workspace_id: int, account_id: int) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT r.id, r.device_id, d.name AS device_name, r.started_at
        FROM procure.execution_runs r
        JOIN procure.devices d ON d.id = r.device_id AND d.workspace_id = r.workspace_id
        WHERE r.workspace_id = %s AND r.account_id = %s AND r.ended_at IS NULL
        ORDER BY r.started_at DESC, r.id DESC LIMIT 1
        """,
        (workspace_id, account_id),
    ).fetchone()


def heartbeat(conn, device: dict[str, Any], *, ext_version: Any = None, identity: Any = None) -> dict[str, Any]:
    """输入：设备行、插件版本、可选身份 → 输出：契约 §10「插件心跳 / 配置」
    {device, account, identity_status, server_time, bj_date, simulation_only, active_run}。

    更新 last_seen_at（与版本）；带身份时同 report_identity。不续执行批次的租约（那是批次心跳的事）。
    """
    dev = require_active(conn, device)
    ws = dev["workspace_id"]
    now = clock.now()
    ver = _short(ext_version, 32)
    if ver is not None and ver != dev["ext_version"]:
        conn.execute("UPDATE procure.devices SET last_seen_at = %s, ext_version = %s WHERE id = %s AND workspace_id = %s",
                     (now, ver, dev["id"], ws))
        events.record(conn, ws, "device", dev["id"], "device.ext_version_changed", actor_kind="device",
                      actor_id=dev["id"], detail={"from": dev["ext_version"], "to": ver})
    else:
        conn.execute("UPDATE procure.devices SET last_seen_at = %s WHERE id = %s AND workspace_id = %s",
                     (now, dev["id"], ws))
    if identity is not None:
        report_identity(conn, dev, identity)
    dev = require_active(conn, dev)
    st = settings_svc.get_row(conn, ws)
    account_out = None
    active_run = None
    acc_row = _account(conn, ws, dev["bound_account_id"])
    if acc_row is not None and acc_row["archived_at"] is None:
        full = accounts.get(conn, ws, acc_row["id"])
        account_out = {k: full.get(k) for k in HEARTBEAT_ACCOUNT_KEYS}
        run = _active_run_of_account(conn, ws, acc_row["id"])
        if run is not None:
            active_run = {"id": run["id"], "device_id": run["device_id"], "device_name": run["device_name"],
                          "is_mine": run["device_id"] == dev["id"]}
    return {
        "device": {"id": dev["id"], "name": dev["name"], "bound_account_id": dev["bound_account_id"]},
        "account": account_out,
        "identity_status": effective_identity_status(dev, acc_row),
        "server_time": now.isoformat(),
        "bj_date": clock.bj_today().isoformat(),
        "simulation_only": bool(st["simulation_only"]),
        "active_run": active_run,
    }


# ── 控制台：列表、修改、撤销 ──────────────────────────────────────────────────

_DEVICE_SELECT = """
SELECT d.*, ba.name AS account_name, ba.amazon_customer_id AS account_customer_id,
       ba.archived_at AS account_archived_at,
       r.id AS run_id, r.account_id AS run_account_id, r.started_at AS run_started_at, r.simulated AS run_simulated
FROM procure.devices d
LEFT JOIN procure.buyer_accounts ba ON ba.id = d.bound_account_id AND ba.workspace_id = d.workspace_id
LEFT JOIN LATERAL (
    SELECT id, account_id, started_at, simulated FROM procure.execution_runs
    WHERE device_id = d.id AND workspace_id = d.workspace_id AND ended_at IS NULL
    ORDER BY started_at DESC, id DESC LIMIT 1
) r ON true
"""


def _device_out(r: dict[str, Any]) -> dict[str, Any]:
    now = clock.now()
    acc = None
    if r["bound_account_id"] is not None:
        acc = {"id": r["bound_account_id"], "amazon_customer_id": r["account_customer_id"],
               "archived_at": r["account_archived_at"]}
    revoked = r["revoked_at"] is not None
    identity = r["identity"] if isinstance(r["identity"], dict) else None
    return {
        "id": r["id"],
        "name": r["name"],
        "browser": r["browser"],
        "ext_version": r["ext_version"],
        "bound_account": {"id": r["bound_account_id"], "name": r["account_name"]} if acc else None,
        "online": (not revoked and r["last_seen_at"] is not None
                   and (now - r["last_seen_at"]).total_seconds() <= ONLINE_WINDOW_S),
        "last_seen_at": _iso(r["last_seen_at"]),
        "identity": identity,
        "identity_status": r["identity_status"] if revoked else effective_identity_status(r, acc),
        "created_at": _iso(r["created_at"]),
        "revoked_at": _iso(r["revoked_at"]),
        "active_run": ({"id": r["run_id"], "account_id": r["run_account_id"], "started_at": _iso(r["run_started_at"]),
                        "simulated": r["run_simulated"]} if r["run_id"] is not None else None),
    }


def list_devices(conn, workspace_id: int) -> list[dict[str, Any]]:
    """输入：工作区 → 输出：设备列表（契约 §10「设备」，另附 active_run；未撤销的在前，按名称）。"""
    rows = conn.execute(
        _DEVICE_SELECT + " WHERE d.workspace_id = %s ORDER BY (d.revoked_at IS NOT NULL), d.name, d.id",
        (workspace_id,),
    ).fetchall()
    return [_device_out(r) for r in rows]


def get_device(conn, workspace_id: int, device_id: Any) -> dict[str, Any]:
    """输入：工作区、设备 id → 输出：设备（契约 §10）；不存在或跨工作区 → NOT_FOUND。"""
    did = _int_id(device_id, "设备")
    r = conn.execute(_DEVICE_SELECT + " WHERE d.workspace_id = %s AND d.id = %s", (workspace_id, did)).fetchone()
    if r is None:
        raise not_found("设备")
    return _device_out(r)


def _locked(conn, workspace_id: int, device_id: Any) -> dict[str, Any]:
    did = _int_id(device_id, "设备")
    row = conn.execute("SELECT * FROM procure.devices WHERE id = %s AND workspace_id = %s FOR UPDATE",
                       (did, workspace_id)).fetchone()
    if row is None:
        raise not_found("设备")
    return row


def update_device(conn, workspace_id: int, device_id: Any, *, name: Any = UNSET, bound_account_id: Any = UNSET,
                  actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、设备、新名称 / 绑定账号（UNSET = 不改，None = 解除绑定）、操作人 → 输出：设备（契约 §10）。

    账号须属于同一工作区且未归档（否则 VALIDATION）；设备有未结束的执行批次时不能改绑（CONFLICT）；
    改绑后按新账号重算身份状态。已撤销的设备不能修改。
    """
    dev = _locked(conn, workspace_id, device_id)
    if dev["revoked_at"] is not None:
        raise validation("设备已撤销，不能修改")
    upd: dict[str, Any] = {}
    changes: dict[str, Any] = {}
    if name is not UNSET:
        n = _clean_name(name)
        if n != dev["name"]:
            upd["name"] = n
            changes["name"] = [dev["name"], n]
    if bound_account_id is not UNSET:
        new_id: int | None = None
        acc = None
        if bound_account_id is not None:
            if isinstance(bound_account_id, bool):
                raise validation("买家账号不存在", field="bound_account_id")
            try:
                new_id = int(bound_account_id)
            except (TypeError, ValueError):
                raise validation("买家账号不存在", field="bound_account_id") from None
            acc = _account(conn, workspace_id, new_id)
            if acc is None:
                raise validation("买家账号不存在", field="bound_account_id")
            if acc["archived_at"] is not None:
                raise validation(f"买家账号 {acc['name']} 已归档，不能绑定", field="bound_account_id")
        if new_id != dev["bound_account_id"]:
            busy = conn.execute(
                "SELECT id FROM procure.execution_runs WHERE device_id = %s AND workspace_id = %s AND ended_at IS NULL "
                "LIMIT 1",
                (dev["id"], workspace_id),
            ).fetchone()
            if busy is not None:
                raise conflict("设备正在执行采购批次，结束后才能改绑账号", run_id=busy["id"])
            status = compute_identity_status(dev["identity"], acc) if dev["identity"] else "unknown"
            upd.update(bound_account_id=new_id, identity_status=status)
            changes["bound_account_id"] = [dev["bound_account_id"], new_id]
            if status != dev["identity_status"]:
                changes["identity_status"] = [dev["identity_status"], status]
    if upd:
        sets = ", ".join(f"{k} = %s" for k in upd)
        conn.execute(f"UPDATE procure.devices SET {sets} WHERE id = %s AND workspace_id = %s",
                     [*upd.values(), dev["id"], workspace_id])
        events.record(conn, workspace_id, "device", dev["id"], "device.updated",
                      actor_kind="user" if actor_user_id is not None else "system", actor_id=actor_user_id,
                      detail={"changes": changes})
    return get_device(conn, workspace_id, dev["id"])


def revoke(conn, workspace_id: int, device_id: Any, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、设备、操作人 → 输出：设备（契约 §10，revoked_at 非空）。

    令牌哈希置空（令牌立即失效）；它未结束的执行批次按租约过期同样处理（end_reason='device_revoked'）；
    账号把它设为默认执行设备的，清掉该设置。已撤销的设备原样返回。
    """
    from services import execution

    dev = _locked(conn, workspace_id, device_id)
    if dev["revoked_at"] is not None:
        return get_device(conn, workspace_id, dev["id"])
    ak = "user" if actor_user_id is not None else "system"
    now = clock.now()
    conn.execute("UPDATE procure.devices SET token_hash = NULL, revoked_at = %s WHERE id = %s AND workspace_id = %s",
                 (now, dev["id"], workspace_id))
    runs = conn.execute(
        "SELECT * FROM procure.execution_runs WHERE device_id = %s AND workspace_id = %s AND ended_at IS NULL "
        "ORDER BY id FOR UPDATE",
        (dev["id"], workspace_id),
    ).fetchall()
    ended = []
    for run in runs:
        execution.end_run(conn, run, "device_revoked", actor_kind=ak, actor_id=actor_user_id)
        ended.append(run["id"])
    cleared = conn.execute(
        "UPDATE procure.buyer_accounts SET default_device_id = NULL, updated_at = %s "
        "WHERE workspace_id = %s AND default_device_id = %s RETURNING id",
        (now, workspace_id, dev["id"]),
    ).fetchall()
    events.record(conn, workspace_id, "device", dev["id"], "device.revoked", actor_kind=ak, actor_id=actor_user_id,
                  detail={"runs_ended": ended, "default_device_cleared_for": [r["id"] for r in cleared]})
    return get_device(conn, workspace_id, dev["id"])
