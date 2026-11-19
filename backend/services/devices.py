"""浏览器（库里的 devices 行，一行 = 一个装了插件的浏览器环境）：自动登记与令牌认证、登录状态与解析、心跳、
列表 / 改名 / 移除（契约 §5、§10「浏览器」「插件心跳」）。

- **买家号的身份 = 亚马逊 customerId（买家号 ID）**，由插件从当前亚马逊页面读出后上报。浏览器与账号之间没有绑定：
  账号上登记了哪个 ID，登录着这个买家号的浏览器就能执行它的任务。
- **登记**：插件自己生成令牌，`register` 幂等地为它建一行（库里只存 sha256），没有任何人工步骤。令牌只是会话
  标识（把批次、尝试、回执归到同一个浏览器），不代表身份。浏览器的工作区在登记时确定，之后不变。
- **登记上限**（settings.max_devices）只数「还在用」的浏览器：没有被移除（hidden_at 为空）、且最近联系时间
  （从未联系过的按登记时间）在 settings.device_retention_days 天以内（截止时刻见 `retention_cutoff`，清扫删除闲置行
  用的是同一个）。所以控制台「移除」立刻腾出名额，换了令牌留下的旧行过了保留期也不再占名额（执行过批次的旧行
  清扫不会删，只是不再计数）。已登记的浏览器再次联系（包括被移除的、过了保留期的）不查上限。
- **认证**：`authenticate` 只按令牌哈希找行（契约 §0 的认证查询例外）；`require_active` 按 (id, 工作区) 重读。
- **身份上报**：`{signed_in, customer_id, site, page_url, conflict}`。买家号 ID 大写、去空白，形状不对就当作
  没读到，绝不报错；`page_url` 只留 http / https 的 origin + path（去掉用户名密码、查询串与片段；别的协议、解析
  不了的当作没有）。存进 `identity`（附服务端时刻 at）与 `identity_at`；
  `login_customer_id` = 已登录、读到 ID 且各标签页读数一致时的 ID，否则为空。
- **插件送来的文本**（版本、UA、site、page_url）入库前先去掉 PostgreSQL 存不了的字符（NUL、不成对的代理项，
  见 `storable_text`；执行协议里插件送来的文本用的是同一个函数）：坏字符不该变成 500。浏览器名称是所有者在
  控制台填的，含控制字符直接 VALIDATION。
- **登录状态**（`login_state`，由浏览器行算出）：unknown（从未上报）/ conflict / signed_out / no_id / signed_in。
- **解析**（`resolve_login`，每次现算，不存）：signed_in 时在浏览器所属工作区找登记了这个 ID 的未归档账号 →
  ready；没有且 ID 在忽略名单里 → ignored；否则 unregistered。其余状态原样。插件侧状态全集见 PLUGIN_STATES。
  服务端不替插件创建账号：没登记的 ID 只留在浏览器行上，由控制台「待登记的买家号」列出（services/accounts）。
- **过期**（stale）= `identity_at` 距现在超过 registry 的 identity_fresh_s。
- **在线** = 3 分钟（ONLINE_WINDOW_S）内有联系。
- **移除**只是把浏览器从列表里清掉（记 `hidden_at`，不动令牌），同时不再占登记名额；它再次联系（心跳、身份上报、
  登记）时自动回到列表（不查上限）。正在执行批次的浏览器不能移除。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from registry import settings
from services import accounts, clock, events, quota, settings_svc
from services.errors import DomainError, conflict, not_found, validation
from services.orders import UNSET

ONLINE_WINDOW_S = 180
#: 插件生成的令牌：32 字节 base64url（43 个字符）；旧版配对得到的令牌同样符合
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
#: 低于这个版本的插件不认识新的心跳形状与开始批次参数
MIN_EXT_VERSION = "0.2.0"
#: 浏览器行算出的登录状态
LOGIN_STATES = ("unknown", "conflict", "signed_out", "no_id", "signed_in")
#: 插件 / 控制台看到的状态：signed_in 被解析成 ready / ignored / unregistered
PLUGIN_STATES = ("unknown", "signed_out", "no_id", "conflict", "unregistered", "ignored", "ready")
#: 令牌不认识时的说明（server/deps 的 401 用同一句）
UNREGISTERED_MESSAGE = "这个浏览器还没有登记，插件会自动重新登记"
OUTDATED_MESSAGE = f"插件版本过旧：请重新构建并加载新版插件（需要 {MIN_EXT_VERSION} 及以上）"
#: 工作区里还在用的浏览器已到 settings.max_devices 时，新登记得到的 429 说明（写明所有者能做什么）
MAX_DEVICES_MESSAGE = "登记的浏览器太多：请在控制台「账号与浏览器」里移除不用的浏览器，或调大 PC_MAX_DEVICES"
#: page_url 只收这两种协议
_PAGE_URL_SCHEMES = ("http", "https")
#: pg_advisory_xact_lock 命名空间：同一工作区的新登记串行地数名额、插入（allocation 7_310_431、feishu_intake
#: 7_310_432、execution 7_310_433）
_REGISTER_LOCK_NS = 7_310_434

#: 心跳里账号配置要给插件的键（契约 §10「插件心跳」）。卡尾号与金额区间只在领取后的尝试载荷里。
HEARTBEAT_ACCOUNT_KEYS = (
    "id", "name", "amazon_customer_id", "purchase_enabled", "execution_paused", "pause_reason", "purchase_mode",
    "schedule_times", "effective_max_delivery_days", "quota_today", "executable_today", "future_assigned",
    "active_run",
)

__all__ = [
    "HEARTBEAT_ACCOUNT_KEYS", "LOGIN_STATES", "MAX_DEVICES_MESSAGE", "MIN_EXT_VERSION", "ONLINE_WINDOW_S",
    "PLUGIN_STATES", "TOKEN_RE", "UNREGISTERED_MESSAGE", "UNSET", "authenticate", "browser_brief",
    "check_ext_version", "clean_identity", "get_device", "heartbeat", "identity_stale", "list_devices",
    "login_customer_id", "login_state", "register", "remove_device", "report_identity", "require_active",
    "resolve_login", "retention_cutoff", "storable_text", "unauthorized", "update_device",
]


# ── 小工具 ────────────────────────────────────────────────────────────────────


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def storable_text(s: str) -> str:
    """输入：文本 → 输出：去掉 PostgreSQL 存不了的字符后的文本：NUL（text 与 jsonb 都不收）、不成对的代理项
    （编不成 UTF-8，也写不进 JSON 响应）。其余字符原样保留。插件送来的文本入库前都过这里（本模块的 _short、
    services/execution 的 _text 与 _storable_json）。"""
    return s.replace("\x00", "").encode("utf-8", "ignore").decode("utf-8")


def _short(v: Any, n: int) -> str | None:
    """输入：插件送来的值、最多几个字 → 输出：可以入库的文本（先去掉存不了的字符，再去首尾空白、截断）；
    缺失或清完是空的 → None。绝不报错。"""
    if v is None:
        return None
    s = storable_text(str(v)).strip()
    return s[:n] or None


def _clean_name(v: Any) -> str:
    """输入：控制台填的浏览器名称 → 输出：去掉首尾空白的名称。空、超过 64 个字、或含控制字符（NUL、换行、制表符等，
    以及存不了的代理项）→ VALIDATION（field = name）；不替所有者悄悄改掉。"""
    if not isinstance(v, str) or not v.strip():
        raise validation("浏览器名称不能为空", field="name")
    s = v.strip()
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in s):
        raise validation("浏览器名称不能包含换行、制表符等控制字符", field="name")
    if len(s) > 64:
        raise validation("浏览器名称不能超过 64 个字", field="name")
    return s


def _int_id(v: Any, what: str) -> int:
    if isinstance(v, bool):
        raise not_found(what)
    try:
        return int(v)
    except (TypeError, ValueError):
        raise not_found(what) from None


def _is_online(row: dict[str, Any], now) -> bool:
    seen = row.get("last_seen_at")
    return seen is not None and (now - seen).total_seconds() <= ONLINE_WINDOW_S


def browser_brief(row: dict[str, Any], now=None) -> dict[str, Any]:
    """输入：浏览器行（至少 id、name、last_seen_at）→ 输出：{id, name, online, last_seen_at}
    （账号的 browsers、待登记买家号的 browsers 共用）。"""
    return {"id": row["id"], "name": row["name"], "online": _is_online(row, now or clock.now()),
            "last_seen_at": _iso(row["last_seen_at"])}


# ── 版本闸 ────────────────────────────────────────────────────────────────────


def _parse_version(v: Any) -> tuple[int, ...] | None:
    """输入：插件版本（清单里是 1–4 段点分整数）→ 输出：补齐到 4 段的整数元组；不是这种写法 → None。"""
    if not isinstance(v, str) or not re.fullmatch(r"\d{1,9}(\.\d{1,9}){0,3}", v.strip()):
        return None
    parts = tuple(int(x) for x in v.strip().split("."))
    return parts + (0,) * (4 - len(parts))


def check_ext_version(ext_version: Any) -> None:
    """输入：插件上报的版本 → 输出：无；能解析且低于 MIN_EXT_VERSION → PLUGIN_OUTDATED（409）。
    缺失或解析不了的不拦。判定的是去掉 PostgreSQL 存不了的字符（storable_text）之后的版本——也就是会存下来的
    那个：「0.1.0」后面跟一个 NUL 或半个代理项，照样按 0.1.0 拦，不会因为原文解析不了而放行、再以 0.1.0 入库。"""
    ver = storable_text(ext_version) if isinstance(ext_version, str) else ext_version
    got = _parse_version(ver)
    if got is not None and got < _parse_version(MIN_EXT_VERSION):
        raise DomainError("PLUGIN_OUTDATED", OUTDATED_MESSAGE, 409,
                          {"ext_version": ver.strip(), "min_ext_version": MIN_EXT_VERSION})


# ── 登记与认证 ────────────────────────────────────────────────────────────────


def unauthorized() -> DomainError:
    """输出：令牌不认识时的 401（插件收到后用同一个令牌重新登记）。"""
    return DomainError("UNAUTHORIZED", UNREGISTERED_MESSAGE, 401)


def _touch(conn, dev: dict[str, Any], now, ver: str | None) -> dict[str, Any] | None:
    """输入：浏览器行、现在、插件版本（None = 没带）→ 输出：更新后的行；行刚好被清扫删掉 → None。

    记最近联系时间、清掉「已移除」（再次联系的浏览器自动回到列表）；版本变了就更新并写事件。
    """
    ws = dev["workspace_id"]
    changed = ver is not None and ver != dev["ext_version"]
    row = conn.execute(
        "UPDATE procure.devices SET last_seen_at = %s, hidden_at = NULL, ext_version = %s "
        "WHERE id = %s AND workspace_id = %s RETURNING *",
        (now, ver if changed else dev["ext_version"], dev["id"], ws),
    ).fetchone()
    if row is not None and changed:
        events.record(conn, ws, "device", dev["id"], "device.ext_version_changed", actor_kind="device",
                      actor_id=dev["id"], detail={"from": dev["ext_version"], "to": ver})
    return row


def _workspace_for_new_device(conn, workspace_id: Any) -> dict[str, Any]:
    """输入：插件带来的工作区 id（可空）→ 输出：新浏览器归属的工作区 {id, name}。

    系统里恰好一个工作区 → 它；没有 → CONFLICT；多个 → 必须带存在的 workspace_id，否则 WORKSPACE_REQUIRED。
    这是登记时确定工作区的查询（契约 §0 的例外：数工作区个数 / 按 id 取工作区）。
    """
    rows = conn.execute("SELECT id, name FROM procure.workspaces ORDER BY id LIMIT 2").fetchall()
    if not rows:
        raise conflict("服务端还没有初始化：请先打开控制台创建用户")
    if len(rows) == 1:
        return rows[0]
    row = None
    if not isinstance(workspace_id, bool) and workspace_id is not None:
        try:
            wid = int(str(workspace_id).strip())
        except ValueError:
            wid = None
        if wid is not None and 0 < wid < 2 ** 62:
            row = conn.execute("SELECT id, name FROM procure.workspaces WHERE id = %s", (wid,)).fetchone()
    if row is None:
        raise DomainError("WORKSPACE_REQUIRED",
                          "系统里有多个工作区：请在插件设置的「工作区 ID」里填写（控制台「设置」页可以查到）", 409)
    return row


def _register_out(conn, dev: dict[str, Any], *, created: bool) -> dict[str, Any]:
    ws = conn.execute("SELECT name FROM procure.workspaces WHERE id = %s", (dev["workspace_id"],)).fetchone()
    return {"device_id": dev["id"], "device_name": dev["name"], "workspace_id": dev["workspace_id"],
            "workspace_name": ws["name"] if ws else None, "created": created}


def retention_cutoff(now: datetime) -> datetime:
    """输入：现在 → 输出：浏览器「还算在用」的截止时刻 = 现在 − settings.device_retention_days 天。

    最近联系时间（COALESCE(last_seen_at, created_at)，从未联系过的按登记时间）早于它的行：不占登记名额（register），
    其中没有任何批次 / 尝试引用的由清扫删除（execution._purge_idle_devices）。两处用的是同一个时刻。
    """
    return now - timedelta(days=int(settings.get().device_retention_days))


def register(conn, device_token: Any, *, ext_version: Any = None, workspace_id: Any = None,
             user_agent: Any = None) -> dict[str, Any]:
    """输入：插件生成的令牌、插件版本、工作区 id（只在系统里有多个工作区时需要）、UA
    → 输出：{device_id, device_name, workspace_id, workspace_name, created}。不需要任何凭证或人工步骤。

    - 令牌须符合 TOKEN_RE（否则 VALIDATION）；库里只存 sha256。
    - **幂等**：令牌已登记 → 返回那一行（更新最近联系时间与版本、清掉「已移除」），created=false，不看 workspace_id。
      并发登记同一个令牌靠 ON CONFLICT (token_hash) DO NOTHING + 重查，只会有一行。
    - 同一工作区的新令牌按事务级咨询锁排队数名额、插入，并发登记也不会超过上限。
    - 新浏览器的工作区见 _workspace_for_new_device；名称是「浏览器 <id>」，之后只能在控制台改名。
    - 上限：工作区内**还在用**的浏览器（没有被移除、最近联系时间不早于 retention_cutoff）已到 settings.max_devices
      → RATE_LIMITED（429，MAX_DEVICES_MESSAGE）。被移除的、过了保留期没联系的行不占名额；只有新令牌查上限。
    - 版本能解析且低于 MIN_EXT_VERSION → PLUGIN_OUTDATED（409）。
    - 新登记写 'device.registered' 事件（detail：ext_version、user_agent 前 200 字）。
    """
    if not isinstance(device_token, str) or not TOKEN_RE.fullmatch(device_token):
        raise validation("浏览器令牌格式不对（应为 32–128 位字母、数字、下划线或连字符）", field="device_token")
    check_ext_version(ext_version)
    ver = _short(ext_version, 32)
    ua = _short(user_agent, 500)
    token_hash = _sha(device_token)
    now = clock.now()
    existing = conn.execute("SELECT * FROM procure.devices WHERE token_hash = %s", (token_hash,)).fetchone()
    touched = _touch(conn, existing, now, ver) if existing is not None else None
    if touched is not None:
        return _register_out(conn, touched, created=False)
    ws = _workspace_for_new_device(conn, workspace_id)
    # 同一工作区的新登记在这里排队，「数名额 + 插入」一个接一个做：否则名额只剩一个时两个新浏览器同时登记，各自
    # 数到 max−1、都插得进去。排到之后先按令牌重查一次：排队期间同一个令牌可能已经登记成功了（并发重试），那是
    # 已登记的浏览器再次联系，不查上限。锁在事务结束时释放；持有它时只会再锁这个令牌自己的那一行。
    conn.execute("SELECT pg_advisory_xact_lock(%s::int, %s::int)", (_REGISTER_LOCK_NS, ws["id"] % 2_147_483_647))
    existing = conn.execute("SELECT * FROM procure.devices WHERE token_hash = %s", (token_hash,)).fetchone()
    touched = _touch(conn, existing, now, ver) if existing is not None else None
    if touched is not None:
        return _register_out(conn, touched, created=False)
    total = conn.execute(
        "SELECT count(*) AS n FROM procure.devices WHERE workspace_id = %s AND hidden_at IS NULL "
        "AND COALESCE(last_seen_at, created_at) >= %s",
        (ws["id"], retention_cutoff(now)),
    ).fetchone()["n"]
    if total >= int(settings.get().max_devices):
        raise DomainError("RATE_LIMITED", MAX_DEVICES_MESSAGE, 429)
    inserted = conn.execute(
        """
        INSERT INTO procure.devices (workspace_id, name, token_hash, ext_version, user_agent, last_seen_at, created_at)
        VALUES (%s, '浏览器', %s, %s, %s, %s, %s)
        ON CONFLICT (token_hash) DO NOTHING RETURNING id
        """,
        (ws["id"], token_hash, ver, ua, now, now),
    ).fetchone()
    if inserted is None:
        # 另一个请求刚用同一个令牌登记成功（本句等它提交后才返回）：按幂等处理
        existing = conn.execute("SELECT * FROM procure.devices WHERE token_hash = %s", (token_hash,)).fetchone()
        if existing is None:  # pragma: no cover - 对方登记后又被删掉，窗口极小
            raise conflict("登记失败：请稍后重试")
        return _register_out(conn, existing, created=False)
    dev = conn.execute(
        "UPDATE procure.devices SET name = %s WHERE id = %s AND workspace_id = %s RETURNING *",
        (f"浏览器 {inserted['id']}", inserted["id"], ws["id"]),
    ).fetchone()
    events.record(conn, ws["id"], "device", dev["id"], "device.registered", actor_kind="device", actor_id=dev["id"],
                  detail={"ext_version": ver, "user_agent": ua[:200] if ua else None})
    return _register_out(conn, dev, created=True)


def authenticate(conn, token: Any) -> dict[str, Any] | None:
    """输入：浏览器令牌 → 输出：浏览器行（含 workspace_id）；令牌为空或没登记过 → None。只按令牌哈希查。"""
    if not isinstance(token, str) or not token.strip():
        return None
    return conn.execute("SELECT * FROM procure.devices WHERE token_hash = %s", (_sha(token.strip()),)).fetchone()


def require_active(conn, device: dict[str, Any], *, lock: bool = False) -> dict[str, Any]:
    """输入：浏览器行（authenticate 的结果，可能已过时）、是否加行锁 → 输出：库里最新的浏览器行。

    只按 (id, workspace_id) 重读；行已不在（被清扫删掉）→ UNAUTHORIZED（401，插件会重新登记）。
    lock=True 时 SELECT … FOR UPDATE：开始批次与提交登记用它和并发的身份上报串行（加锁顺序里浏览器行排最前）。
    """
    row = conn.execute(
        "SELECT * FROM procure.devices WHERE id = %s AND workspace_id = %s" + (" FOR UPDATE" if lock else ""),
        (device["id"], device["workspace_id"]),
    ).fetchone()
    if row is None:
        raise unauthorized()
    return row


# ── 身份、登录状态与解析 ──────────────────────────────────────────────────────


def _clean_page_url(v: Any) -> str | None:
    """输入：插件上报的页面地址 → 输出：`scheme://host[:port]/path`（最多 500 字）；当作没有 → None。绝不报错。

    只收 http / https 的绝对地址，由解析出的各部分重新拼成：用户名密码、查询串、片段都不存。别的协议
    （javascript:、chrome-extension: …）、没有主机的（相对路径）、解析不了的（方括号不配对、端口不是数字 …）
    一律当作没有——不退回去存原文，原文里可能带着查询串。
    """
    url = _short(v, 2000)
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in _PAGE_URL_SCHEMES or not host:
        return None
    if parts.netloc.rpartition("@")[2].startswith("["):   # IP 字面量（IPv6、IPvFuture）：hostname 去掉了方括号，拼回去
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path}"[:500]


def clean_identity(identity: Any) -> dict[str, Any]:
    """输入：插件上报的身份 {signed_in, customer_id, site, page_url, conflict?} → 输出：清洗后的同名字典。

    customer_id 去掉所有空白、转大写，须符合 accounts.CUSTOMER_ID_RE 且不超过 64 位，否则当作没读到（None），
    绝不报错；signed_in 只认布尔 true；conflict 取布尔；page_url 只留 http / https 的 origin + path（见
    _clean_page_url：不存用户名密码、查询串与片段，别的协议与解析不了的当作没有）；site 与 page_url 里 PostgreSQL
    存不了的字符（NUL、不成对的代理项）先去掉。只有整个身份不是对象时才 VALIDATION。
    """
    if not isinstance(identity, dict):
        raise validation("身份信息格式不对", field="identity")
    raw = identity.get("customer_id")
    cid = "".join(raw.split()).upper() if isinstance(raw, str) else None
    if not cid or len(cid) > 64 or not accounts.CUSTOMER_ID_RE.match(cid):
        cid = None
    return {
        "signed_in": identity.get("signed_in") is True,
        "customer_id": cid,
        "site": _short(identity.get("site"), 8),
        "page_url": _clean_page_url(identity.get("page_url")),
        "conflict": bool(identity.get("conflict")),
    }


def login_customer_id(identity: dict[str, Any] | None) -> str | None:
    """输入：清洗后的身份 → 输出：可以用来领任务的买家号 ID：已登录、读到 ID 且没有分歧时的 ID，否则 None。"""
    if not isinstance(identity, dict):
        return None
    if identity.get("signed_in") is True and identity.get("customer_id") and not identity.get("conflict"):
        return identity["customer_id"]
    return None


def login_state(dev: dict[str, Any]) -> str:
    """输入：浏览器行 → 输出：登录状态 unknown（从未上报）/ conflict（各标签页读数不一致）/ signed_out /
    no_id（已登录但没读到合格的买家号 ID）/ signed_in。纯函数，不看新鲜度，也不看账号。"""
    ident = dev.get("identity")
    if not isinstance(ident, dict) or not ident:
        return "unknown"
    if ident.get("conflict"):
        return "conflict"
    if ident.get("signed_in") is not True:
        return "signed_out"
    if not dev.get("login_customer_id"):
        return "no_id"
    return "signed_in"


def identity_stale(dev: dict[str, Any], now=None) -> bool:
    """输入：浏览器行 → 输出：最近一次身份上报是否已超过 identity_fresh_s。从未上报（状态 unknown）不算过期。"""
    at = dev.get("identity_at")
    if at is None:
        return False
    return ((now or clock.now()) - at).total_seconds() > int(settings.get().identity_fresh_s)


def _is_ignored(conn, workspace_id: int, customer_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM procure.ignored_buyer_ids WHERE workspace_id = %s AND customer_id = %s",
        (workspace_id, customer_id),
    ).fetchone() is not None


def resolve_login(conn, dev: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """输入：浏览器行 → 输出：(插件侧状态, 账号行 | None)。每次现算，不存。

    登录状态是 signed_in 时，在浏览器所属工作区找登记了 login_customer_id 的未归档账号：有 → ready（附账号行）；
    没有且 ID 在忽略名单里 → ignored；否则 unregistered。其余状态原样返回、没有账号。
    """
    state = login_state(dev)
    if state != "signed_in":
        return state, None
    ws = dev["workspace_id"]
    acc = accounts.find_by_customer_id(conn, ws, dev["login_customer_id"])
    if acc is not None:
        return "ready", acc
    return ("ignored" if _is_ignored(conn, ws, dev["login_customer_id"]) else "unregistered"), None


def _login_out(dev: dict[str, Any], state: str, now) -> dict[str, Any]:
    return {"state": state, "customer_id": dev.get("login_customer_id"), "at": _iso(dev.get("identity_at")),
            "stale": identity_stale(dev, now)}


def _store_identity(conn, dev: dict[str, Any], identity: Any, now) -> dict[str, Any]:
    """输入：浏览器行、插件上报的身份、现在 → 输出：更新后的浏览器行。

    存下清洗后的身份（附服务端时刻 at）、identity_at 与 login_customer_id；UPDATE 本身锁住这一行，和开始批次、
    提交登记（它们先 FOR UPDATE 锁浏览器行）串行。(登录状态, ID) 变化时写 'device.identity_changed' 事件。
    """
    ident = clean_identity(identity)
    ident["at"] = now.isoformat()
    new = conn.execute(
        "UPDATE procure.devices SET identity = %s, identity_at = %s, login_customer_id = %s "
        "WHERE id = %s AND workspace_id = %s RETURNING *",
        (Jsonb(ident), now, login_customer_id(ident), dev["id"], dev["workspace_id"]),
    ).fetchone()
    before = {"state": login_state(dev), "customer_id": dev.get("login_customer_id")}
    after = {"state": login_state(new), "customer_id": new["login_customer_id"]}
    if before != after:
        events.record(conn, dev["workspace_id"], "device", dev["id"], "device.identity_changed", actor_kind="device",
                      actor_id=dev["id"], detail={"from": before, "to": after})
    return new


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


def _run_of_device(conn, workspace_id: int, device_id: int) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT r.id, r.account_id, a.name AS account_name, r.started_at, r.simulated
        FROM procure.execution_runs r
        JOIN procure.buyer_accounts a ON a.id = r.account_id AND a.workspace_id = r.workspace_id
        WHERE r.workspace_id = %s AND r.device_id = %s AND r.ended_at IS NULL
        ORDER BY r.started_at DESC, r.id DESC LIMIT 1
        """,
        (workspace_id, device_id),
    ).fetchone()


def _heartbeat_account(conn, dev: dict[str, Any], acc: dict[str, Any], simulated: bool) -> dict[str, Any]:
    """输入：浏览器行、解析到的账号行、当前的模拟标记 → 输出：心跳里的 account（键见 HEARTBEAT_ACCOUNT_KEYS）。"""
    from services import execution      # 延迟导入：execution 在模块级导入本模块

    ws = acc["workspace_id"]
    today = clock.bj_today()
    counts = execution.claimable_counts(conn, ws, acc["id"], today)
    run = _active_run_of_account(conn, ws, acc["id"])
    return {
        "id": acc["id"], "name": acc["name"], "amazon_customer_id": acc["amazon_customer_id"],
        "purchase_enabled": acc["purchase_enabled"], "execution_paused": acc["execution_paused"],
        "pause_reason": acc["pause_reason"], "purchase_mode": acc["purchase_mode"],
        "schedule_times": list(acc["schedule_times"] or []),
        "effective_max_delivery_days": accounts.effective_max_delivery_days(conn, ws, acc),
        "quota_today": quota.summary(conn, acc, today, simulated),
        "executable_today": counts["executable_today"], "future_assigned": counts["future_assigned"],
        "active_run": ({"id": run["id"], "device_id": run["device_id"], "device_name": run["device_name"],
                        "started_at": _iso(run["started_at"]), "is_mine": run["device_id"] == dev["id"]}
                       if run is not None else None),
    }


def _status(conn, dev: dict[str, Any], now) -> dict[str, Any]:
    """输入：最新的浏览器行 → 输出：心跳 / 身份上报共用的响应（契约 §10「插件心跳」）。"""
    ws = dev["workspace_id"]
    state, acc = resolve_login(conn, dev)
    simulated = settings_svc.simulation_only(conn, ws)
    mine = _run_of_device(conn, ws, dev["id"])
    return {
        "device": {"id": dev["id"], "name": dev["name"]},
        "login": _login_out(dev, state, now),
        "account": _heartbeat_account(conn, dev, acc, simulated) if acc is not None else None,
        "device_run": ({"id": mine["id"], "account_id": mine["account_id"], "account_name": mine["account_name"],
                        "started_at": _iso(mine["started_at"])} if mine is not None else None),
        "server_time": now.isoformat(),
        "bj_date": clock.bj_today().isoformat(),
        "simulation_only": simulated,
    }


def heartbeat(conn, device: dict[str, Any], *, ext_version: Any = None, identity: Any = None) -> dict[str, Any]:
    """输入：浏览器行、插件版本、可选身份 → 输出：契约 §10「插件心跳」
    {device: {id, name}, login: {state, customer_id, at, stale}, account, device_run, server_time, bj_date,
    simulation_only}。

    - login.state 是解析后的状态（PLUGIN_STATES）；account 只在 ready 时非空（键见 HEARTBEAT_ACCOUNT_KEYS）：
      executable_today 与领取条件同口径（不与剩余额度取小），future_assigned 是已分配、建议采购日期在今天之后的
      笔数，active_run 是账号的未结束批次（is_mine = 是不是这台浏览器的）。
    - device_run 是这台浏览器自己的未结束批次（不论账号）。
    - 记最近联系时间与版本、清掉「已移除」；带身份时一并存下。不续执行批次的租约（那是批次心跳的事）。
    - 版本能解析且低于 MIN_EXT_VERSION → PLUGIN_OUTDATED（409），什么也不记。
    """
    check_ext_version(ext_version)
    dev = require_active(conn, device)
    now = clock.now()
    dev = _touch(conn, dev, now, _short(ext_version, 32))
    if dev is None:
        raise unauthorized()
    if identity is not None:
        dev = _store_identity(conn, dev, identity, now)
    return _status(conn, dev, now)


def report_identity(conn, device: dict[str, Any], identity: Any, *, ext_version: Any = None) -> dict[str, Any]:
    """输入：浏览器行、插件上报的身份 {signed_in, customer_id, site, page_url, conflict?}、可选插件版本
    → 输出：与 heartbeat 相同的形状（同样记最近联系时间、清掉「已移除」）。身份不是对象 → VALIDATION。"""
    if not isinstance(identity, dict):
        raise validation("身份信息格式不对", field="identity")
    return heartbeat(conn, device, ext_version=ext_version, identity=identity)


# ── 控制台：列表、改名、移除 ──────────────────────────────────────────────────

_DEVICE_SELECT = """
SELECT d.*, ba.id AS account_id, ba.name AS account_name, (ig.customer_id IS NOT NULL) AS id_ignored,
       r.id AS run_id, r.account_id AS run_account_id, r.started_at AS run_started_at, r.simulated AS run_simulated
FROM procure.devices d
LEFT JOIN procure.buyer_accounts ba ON ba.workspace_id = d.workspace_id
     AND ba.amazon_customer_id = d.login_customer_id AND ba.archived_at IS NULL
LEFT JOIN procure.ignored_buyer_ids ig ON ig.workspace_id = d.workspace_id AND ig.customer_id = d.login_customer_id
LEFT JOIN LATERAL (
    SELECT id, account_id, started_at, simulated FROM procure.execution_runs
    WHERE device_id = d.id AND workspace_id = d.workspace_id AND ended_at IS NULL
    ORDER BY started_at DESC, id DESC LIMIT 1
) r ON true
"""


def _device_out(r: dict[str, Any], now) -> dict[str, Any]:
    state = login_state(r)
    account = None
    if state == "signed_in":
        if r["account_id"] is not None:
            state, account = "ready", {"id": r["account_id"], "name": r["account_name"]}
        else:
            state = "ignored" if r["id_ignored"] else "unregistered"
    return {
        "id": r["id"],
        "name": r["name"],
        "ext_version": r["ext_version"],
        "online": _is_online(r, now),
        "last_seen_at": _iso(r["last_seen_at"]),
        "created_at": _iso(r["created_at"]),
        "hidden_at": _iso(r["hidden_at"]),
        "login": _login_out(r, state, now),
        "account": account,
        "active_run": ({"id": r["run_id"], "account_id": r["run_account_id"], "started_at": _iso(r["run_started_at"]),
                        "simulated": r["run_simulated"]} if r["run_id"] is not None else None),
    }


def list_devices(conn, workspace_id: int, include_hidden: bool = False) -> list[dict[str, Any]]:
    """输入：工作区、是否含已移除的 → 输出：浏览器列表（契约 §10「浏览器」）：
    {id, name, ext_version, online, last_seen_at, created_at, hidden_at, login: {state, customer_id, at, stale},
    account: {id, name} | None, active_run: {id, account_id, started_at, simulated} | None}。

    login.state 是解析后的状态；未移除的在前，按名称。
    """
    rows = conn.execute(
        _DEVICE_SELECT + " WHERE d.workspace_id = %s" + ("" if include_hidden else " AND d.hidden_at IS NULL")
        + " ORDER BY (d.hidden_at IS NOT NULL), d.name, d.id",
        (workspace_id,),
    ).fetchall()
    now = clock.now()
    return [_device_out(r, now) for r in rows]


def get_device(conn, workspace_id: int, device_id: Any) -> dict[str, Any]:
    """输入：工作区、浏览器 id → 输出：浏览器（形状同 list_devices，含已移除的）；不存在或跨工作区 → NOT_FOUND。"""
    did = _int_id(device_id, "浏览器")
    r = conn.execute(_DEVICE_SELECT + " WHERE d.workspace_id = %s AND d.id = %s", (workspace_id, did)).fetchone()
    if r is None:
        raise not_found("浏览器")
    return _device_out(r, clock.now())


def _locked(conn, workspace_id: int, device_id: Any) -> dict[str, Any]:
    did = _int_id(device_id, "浏览器")
    row = conn.execute("SELECT * FROM procure.devices WHERE id = %s AND workspace_id = %s FOR UPDATE",
                       (did, workspace_id)).fetchone()
    if row is None:
        raise not_found("浏览器")
    return row


def update_device(conn, workspace_id: int, device_id: Any, *, name: Any = UNSET,
                  actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、浏览器、新名称（UNSET = 不改）、操作人 → 输出：浏览器。只能改名；名称为空、超过 64 个字或
    含控制字符 → VALIDATION（field = name）。"""
    dev = _locked(conn, workspace_id, device_id)
    if name is not UNSET:
        n = _clean_name(name)
        if n != dev["name"]:
            conn.execute("UPDATE procure.devices SET name = %s WHERE id = %s AND workspace_id = %s",
                         (n, dev["id"], workspace_id))
            events.record(conn, workspace_id, "device", dev["id"], "device.updated",
                          actor_kind="user" if actor_user_id is not None else "system", actor_id=actor_user_id,
                          detail={"changes": {"name": [dev["name"], n]}})
    return get_device(conn, workspace_id, dev["id"])


def remove_device(conn, workspace_id: int, device_id: Any, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、浏览器、操作人 → 输出：浏览器（hidden_at 非空）。

    只是把它从列表里清掉：记 hidden_at、写 'device.removed' 事件，不动令牌、不结束任何批次；这个浏览器再次联系时
    自动回到列表。被移除的浏览器不再占登记名额（register 的上限只数没有移除的），所以名额满了时在这里就能腾出来。
    它有未结束的执行批次 → CONFLICT。已移除的原样返回。要停止某个买家号的采购，应暂停账号。
    """
    dev = _locked(conn, workspace_id, device_id)
    if dev["hidden_at"] is not None:
        return get_device(conn, workspace_id, dev["id"])
    busy = conn.execute(
        "SELECT id FROM procure.execution_runs WHERE device_id = %s AND workspace_id = %s AND ended_at IS NULL LIMIT 1",
        (dev["id"], workspace_id),
    ).fetchone()
    if busy is not None:
        raise conflict("这个浏览器正在执行，结束后才能移除", run_id=busy["id"])
    conn.execute("UPDATE procure.devices SET hidden_at = %s WHERE id = %s AND workspace_id = %s",
                 (clock.now(), dev["id"], workspace_id))
    events.record(conn, workspace_id, "device", dev["id"], "device.removed",
                  actor_kind="user" if actor_user_id is not None else "system", actor_id=actor_user_id,
                  detail={"name": dev["name"]})
    return get_device(conn, workspace_id, dev["id"])
