"""认证（契约 §8）：首次设置、登录、退出、当前用户。

/auth/state、/auth/setup、/auth/login 不要求会话（仍要求写请求的防跨站请求头）；其余要求会话。
登录成功与首次设置成功都写 pc_session Cookie，响应体为 {user, workspace, role}。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from server import deps
from server.serialize import respond
from services import auth

router = APIRouter(prefix="/auth", tags=["auth"])


class SetupBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    username: Any = None
    password: Any = None
    workspace_name: Any = None


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    username: Any = None
    password: Any = None


@router.get("/state")
def auth_state(conn=Depends(deps.db_conn)):
    """输入：无 → 输出：{needs_setup}（尚无任何用户时为 true）。"""
    return respond({"needs_setup": auth.needs_setup(conn)})


@router.post("/setup")
def auth_setup(body: SetupBody, conn=Depends(deps.db_conn)):
    """输入：{username, password, workspace_name} → 输出：{user, workspace, role}（创建所有者与工作区并登录）。

    已有用户 → 403 FORBIDDEN；用户名 / 密码不合格 → 422 VALIDATION。
    """
    me = auth.setup_owner(conn, body.username, body.password, body.workspace_name)
    token = auth.create_session(conn, me["user"]["id"], me["workspace"]["id"])
    resp = respond(me)
    deps.set_session_cookie(resp, token)
    return resp


@router.post("/login")
def auth_login(body: LoginBody, conn=Depends(deps.db_conn)):
    """输入：{username, password} → 输出：{user, workspace, role}；失败 → 401 UNAUTHORIZED（统一文案）。"""
    token, me = auth.login(conn, body.username, body.password)
    resp = respond(me)
    deps.set_session_cookie(resp, token)
    return resp


@router.post("/logout")
def auth_logout(request: Request, ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：{ok: true}（撤销该会话并删除 Cookie）。"""
    auth.logout(ctx.conn, request.cookies.get(deps.SESSION_COOKIE))
    resp = respond({"ok": True})
    deps.clear_session_cookie(resp)
    return resp


@router.get("/me")
def auth_me(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：{user, workspace, role}。"""
    return respond(ctx.me)
