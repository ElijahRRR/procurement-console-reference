"""路由的公共依赖：数据库事务、控制台会话、插件设备令牌、分页、防跨站请求头、会话 Cookie。

事务边界（铁律 4）：每个请求一个 `registry.db.pg_conn()` 事务。依赖以生成器形式交出连接，路由正常返回 →
提交；路由抛异常（含 DomainError）→ 回滚。FastAPI 在发送响应**之前**执行依赖的收尾，所以提交失败会变成
错误响应，而不会出现「已回 200 但没提交」。

- 控制台：会话 Cookie `pc_session`（HttpOnly、SameSite=Strict、Secure 取 settings.cookie_secure、
  Max-Age 取 settings.session_days），由 services.auth.session_me 校验；无效 → 401 UNAUTHORIZED。
  工作区一律取会话里的工作区，请求不能指定工作区（跨工作区访问由服务层返回 404）。
- 插件：`Authorization: Bearer <设备令牌>`，由 services.devices.authenticate 校验；缺失、未知或已撤销 → 401。
- 防跨站：控制台的写请求必须带 `X-Requested-With: pc-console`（检查在 app.py 的中间件里，见 csrf_ok）。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import Query, Request, Response

from registry import db, settings
from services import auth, devices
from services.errors import DomainError

SESSION_COOKIE = "pc_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "pc-console"
#: 不改状态的方法不要求防跨站请求头
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PAGE_SIZE_MAX = 200

_UNAUTHORIZED_CONSOLE = "请先登录"
_UNAUTHORIZED_DEVICE = "设备未授权或授权已撤销，请在插件里重新配对"


@dataclass
class Console:
    """一次已登录的控制台请求：事务连接 + 当前用户与工作区（{user, workspace, role}）。"""

    conn: Any
    me: dict[str, Any]

    @property
    def user_id(self) -> int:
        """输入：无 → 输出：当前用户 id。"""
        return int(self.me["user"]["id"])

    @property
    def workspace_id(self) -> int:
        """输入：无 → 输出：当前会话所在的工作区 id（所有查询都按它过滤）。"""
        return int(self.me["workspace"]["id"])


@dataclass
class Plugin:
    """一次已认证的插件请求：事务连接 + 设备行（含 workspace_id）。"""

    conn: Any
    device: dict[str, Any]


@dataclass(frozen=True)
class Paging:
    """分页参数：page ≥ 1，page_size 1..200。"""

    page: int
    page_size: int


def db_conn() -> Iterator[Any]:
    """输入：无 → 输出：本请求的事务连接（不要求登录；正常结束提交，异常回滚）。"""
    with db.pg_conn() as conn:
        yield conn


def console(request: Request) -> Iterator[Console]:
    """输入：请求（读 pc_session Cookie）→ 输出：Console（事务连接 + 当前用户）；未登录或会话失效 → 401 UNAUTHORIZED。"""
    token = request.cookies.get(SESSION_COOKIE)
    with db.pg_conn() as conn:
        me = auth.session_me(conn, token) if token else None
        if me is None:
            raise DomainError("UNAUTHORIZED", _UNAUTHORIZED_CONSOLE, 401)
        # session_me 更新了会话行的 last_seen_at：立即提交，释放行锁。否则一个耗时的请求（如飞书立即读取）
        # 会让同一会话的其他请求都排队等它结束。路由的业务事务从这里重新开始。
        conn.commit()
        yield Console(conn=conn, me=me)


def bearer_token(request: Request) -> str | None:
    """输入：请求 → 输出：`Authorization: Bearer <令牌>` 里的令牌；没有或格式不对 → None。"""
    raw = request.headers.get("authorization") or ""
    scheme, _, token = raw.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def plugin(request: Request) -> Iterator[Plugin]:
    """输入：请求（读设备令牌）→ 输出：Plugin（事务连接 + 设备行）；令牌缺失、未知或已撤销 → 401 UNAUTHORIZED。"""
    token = bearer_token(request)
    with db.pg_conn() as conn:
        device = devices.authenticate(conn, token) if token else None
        if device is None:
            raise DomainError("UNAUTHORIZED", _UNAUTHORIZED_DEVICE, 401)
        yield Plugin(conn=conn, device=device)


def paging(page: int = Query(1, ge=1, description="页码，从 1 开始"),
           page_size: int = Query(20, ge=1, le=PAGE_SIZE_MAX, description="每页条数，1–200")) -> Paging:
    """输入：查询参数 page、page_size → 输出：Paging（越界 → 422 VALIDATION）。"""
    return Paging(page=page, page_size=page_size)


def csrf_ok(request: Request) -> bool:
    """输入：请求 → 输出：是否满足防跨站要求（安全方法免检；写请求须带 X-Requested-With: pc-console）。"""
    if request.method.upper() in SAFE_METHODS:
        return True
    return (request.headers.get(CSRF_HEADER) or "").strip().lower() == CSRF_VALUE


def set_session_cookie(response: Response, token: str) -> None:
    """输入：响应、会话令牌 → 输出：无（写 pc_session Cookie：HttpOnly、SameSite=Strict、Secure 按设置、Max-Age 按会话天数）。"""
    s = settings.get()
    response.set_cookie(SESSION_COOKIE, token, max_age=int(s.session_days) * 86400, path="/", httponly=True,
                        samesite="strict", secure=bool(s.cookie_secure))


def clear_session_cookie(response: Response) -> None:
    """输入：响应 → 输出：无（让浏览器删除 pc_session Cookie）。"""
    s = settings.get()
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict", secure=bool(s.cookie_secure))
