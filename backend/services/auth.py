"""控制台用户、工作区与登录会话。

- 密码：hashlib.scrypt，存储格式 `scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`（见 schema.sql users.password_hash）。
- 会话：随机令牌只交给浏览器（Cookie），库里只存 sha256(令牌) 的十六进制；有效期取 registry 的 session_days。
- 登录失败一律同一句「用户名或密码不正确」，不泄露用户名是否存在。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from typing import Any

from registry import settings
from services import clock
from services.errors import DomainError, conflict, validation

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
_SALT_BYTES = 16
_DKLEN = 32
_MAXMEM = 64 * 1024 * 1024

_USERNAME_RE = re.compile(r"^\S{2,64}$")
_LOGIN_FAILED = "用户名或密码不正确"

_dummy_hash: str | None = None


# ── 密码 ─────────────────────────────────────────────────────────────────────


def hash_password(pw: str) -> str:
    """输入：明文密码 → 输出：scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                        dklen=_DKLEN, maxmem=_MAXMEM)
    return "$".join([
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        base64.b64encode(salt).decode("ascii"), base64.b64encode(dk).decode("ascii"),
    ])


def verify_password(pw: str, stored: str) -> bool:
    """输入：明文密码、存储的哈希 → 输出：是否匹配（格式不对一律 False，不抛异常）。"""
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        if n < 2 or n > 2 ** 20 or r < 1 or r > 32 or p < 1 or p > 16:
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        if not expected:
            return False
        dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                            dklen=len(expected), maxmem=_MAXMEM)
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(dk, expected)


def _check_credentials_shape(username: str, password: str) -> str:
    if not isinstance(username, str) or not isinstance(password, str):
        raise validation("用户名和密码必须是文本")
    name = username.strip()
    if not _USERNAME_RE.match(name):
        raise validation("用户名需为 2 到 64 个字符，不能包含空白", field="username")
    if len(password) < 8:
        raise validation("密码至少 8 位", field="password")
    if len(password) > 256:
        raise validation("密码过长", field="password")
    return name


# ── 用户与工作区 ─────────────────────────────────────────────────────────────


def needs_setup(conn) -> bool:
    """输入：连接 → 输出：系统里是否还没有任何用户（true 时控制台显示初始化页）。"""
    row = conn.execute("SELECT EXISTS (SELECT 1 FROM procure.users) AS any_user").fetchone()
    return not row["any_user"]


def create_user(conn, username: str, password: str, *, workspace_id: int | None = None,
                workspace_name: str | None = None, role: str = "owner") -> dict[str, Any]:
    """输入：用户名、密码、已有工作区 id 或新工作区名、角色 → 输出：{user, workspace, role}。

    给了 workspace_id 就加入该工作区（不存在 → NOT_FOUND）；否则新建工作区（名称缺省为「<用户名> 的工作区」）
    并建默认设置行。用户名重复 → CONFLICT。
    """
    name = _check_credentials_shape(username, password)
    if role not in ("owner", "member"):
        raise validation("角色只能是 owner 或 member", field="role")
    exists = conn.execute("SELECT 1 FROM procure.users WHERE username = %s", (name,)).fetchone()
    if exists:
        raise conflict("用户名已存在", field="username")
    now = clock.now()
    if workspace_id is not None:
        ws = conn.execute("SELECT id, name FROM procure.workspaces WHERE id = %s",
                          (workspace_id,)).fetchone()
        if ws is None:
            raise DomainError("NOT_FOUND", "工作区不存在", 404)
    else:
        ws_name = (workspace_name or "").strip() or f"{name} 的工作区"
        if len(ws_name) > 100:
            raise validation("工作区名称过长", field="workspace_name")
        ws = conn.execute(
            "INSERT INTO procure.workspaces (name, created_at) VALUES (%s, %s) RETURNING id, name",
            (ws_name, now),
        ).fetchone()
    conn.execute(
        "INSERT INTO procure.workspace_settings (workspace_id, updated_at) VALUES (%s, %s) "
        "ON CONFLICT (workspace_id) DO NOTHING",
        (ws["id"], now),
    )
    user = conn.execute(
        "INSERT INTO procure.users (username, display_name, password_hash, created_at) "
        "VALUES (%s, %s, %s, %s) RETURNING id, username, display_name",
        (name, name, hash_password(password), now),
    ).fetchone()
    conn.execute(
        "INSERT INTO procure.workspace_members (workspace_id, user_id, role, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (ws["id"], user["id"], role, now),
    )
    return _me(user, ws, role)


def setup_owner(conn, username: str, password: str, workspace_name: str) -> dict[str, Any]:
    """输入：用户名、密码、工作区名 → 输出：{user, workspace, role}。只在系统里还没有任何用户时可用。

    并发两次初始化由事务级咨询锁串行化，第二次得到 FORBIDDEN。
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('procure.auth.setup'))")
    if not needs_setup(conn):
        raise DomainError("FORBIDDEN", "系统已初始化，不能再次创建所有者", 403)
    return create_user(conn, username, password, workspace_name=workspace_name, role="owner")


# ── 会话 ─────────────────────────────────────────────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn, user_id: int, workspace_id: int) -> str:
    """输入：用户、工作区 → 输出：会话令牌明文（库里只存其 sha256）。"""
    token = secrets.token_urlsafe(32)
    now = clock.now()
    expires = now + timedelta(days=settings.get().session_days)
    conn.execute(
        "INSERT INTO procure.sessions (token_hash, user_id, workspace_id, created_at, expires_at, last_seen_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (_token_hash(token), user_id, workspace_id, now, expires, now),
    )
    return token


def login(conn, username: str, password: str) -> tuple[str, dict[str, Any]]:
    """输入：用户名、密码 → 输出：(会话令牌, {user, workspace, role})；失败 → UNAUTHORIZED 401（统一文案）。"""
    global _dummy_hash
    name = (username or "").strip() if isinstance(username, str) else ""
    pw = password if isinstance(password, str) else ""
    user = conn.execute(
        "SELECT id, username, display_name, password_hash, disabled_at FROM procure.users WHERE username = %s",
        (name,),
    ).fetchone() if name else None
    if user is None:
        # 不存在的用户也算一次哈希，避免靠响应时间探测用户名
        if _dummy_hash is None:
            _dummy_hash = hash_password(secrets.token_hex(8))
        verify_password(pw, _dummy_hash)
        raise DomainError("UNAUTHORIZED", _LOGIN_FAILED, 401)
    if not verify_password(pw, user["password_hash"]) or user["disabled_at"] is not None:
        raise DomainError("UNAUTHORIZED", _LOGIN_FAILED, 401)
    member = conn.execute(
        """
        SELECT m.role, w.id, w.name
        FROM procure.workspace_members m JOIN procure.workspaces w ON w.id = m.workspace_id
        WHERE m.user_id = %s
        ORDER BY m.created_at, m.workspace_id
        LIMIT 1
        """,
        (user["id"],),
    ).fetchone()
    if member is None:
        raise DomainError("UNAUTHORIZED", _LOGIN_FAILED, 401)
    token = create_session(conn, user["id"], member["id"])
    return token, _me(user, member, member["role"])


def session_me(conn, token: str | None) -> dict[str, Any] | None:
    """输入：会话令牌 → 输出：{user, workspace, role}；令牌无效、过期、已退出、用户停用或不再是成员 → None。

    命中时更新 last_seen_at。
    """
    if not token or not isinstance(token, str):
        return None
    now = clock.now()
    row = conn.execute(
        """
        SELECT s.token_hash, s.user_id, s.workspace_id, u.username, u.display_name, w.name AS workspace_name, m.role
        FROM procure.sessions s
        JOIN procure.users u ON u.id = s.user_id
        JOIN procure.workspaces w ON w.id = s.workspace_id
        JOIN procure.workspace_members m ON m.workspace_id = s.workspace_id AND m.user_id = s.user_id
        WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > %s AND u.disabled_at IS NULL
        """,
        (_token_hash(token), now),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE procure.sessions SET last_seen_at = %s WHERE token_hash = %s",
                 (now, row["token_hash"]))
    return {
        "user": {"id": row["user_id"], "username": row["username"], "display_name": row["display_name"]},
        "workspace": {"id": row["workspace_id"], "name": row["workspace_name"]},
        "role": row["role"],
    }


def logout(conn, token: str | None) -> None:
    """输入：会话令牌 → 输出：无（把该会话标为已撤销；令牌无效时什么也不做）。"""
    if not token or not isinstance(token, str):
        return
    conn.execute(
        "UPDATE procure.sessions SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL",
        (clock.now(), _token_hash(token)),
    )


def _me(user: dict[str, Any], ws: dict[str, Any], role: str) -> dict[str, Any]:
    return {
        "user": {"id": user["id"], "username": user["username"], "display_name": user.get("display_name")},
        "workspace": {"id": ws["id"], "name": ws["name"]},
        "role": role,
    }
