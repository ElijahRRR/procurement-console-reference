"""浏览器（契约 §8，形状见 §10「浏览器」；接口路径沿用 /devices）：列表、详情、改名、移除。

浏览器由插件自动登记（POST /plugin/v1/register），控制台这边没有任何审批或绑定：这里只能看、改名、把不用的从列表里
移除。移除只记 hidden_at、不动令牌，那个浏览器的插件再次联系服务端时自动回到列表；要停止某个买家号的采购，应暂停账号。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from server import deps
from server.serialize import page, respond
from services import devices

router = APIRouter(prefix="/devices", tags=["devices"])


class DevicePatch(BaseModel):
    """只能改名：name 没传 = 不改；取值由服务层校验。带了别的键（例如旧版的 bound_account_id）→ 422，
    不会悄悄忽略：浏览器与账号之间没有绑定可改。"""

    model_config = ConfigDict(extra="forbid")

    name: Any = None


@router.get("")
def list_devices(include_hidden: bool = False, pg: deps.Paging = Depends(deps.paging),
                 ctx: deps.Console = Depends(deps.console)):
    """输入：是否含已移除的（include_hidden=0|1）、分页 → 输出：{total, rows: [浏览器]}（未移除的在前，按名称）。

    浏览器：{id, name, ext_version, online, last_seen_at, created_at, hidden_at, login: {state, customer_id, at, stale},
    account: {id, name} | null, active_run: {id, account_id, started_at, simulated} | null}；online = 3 分钟内有联系；
    login.state 是解析后的状态（unknown / signed_out / no_id / conflict / unregistered / ignored / ready）。
    """
    rows = devices.list_devices(ctx.conn, ctx.workspace_id, include_hidden=include_hidden)
    return respond(page(rows, pg.page, pg.page_size))


@router.get("/{device_id}")
def get_device(device_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：浏览器 id → 输出：浏览器（含已移除的）；别的工作区 → 404。"""
    return respond(devices.get_device(ctx.conn, ctx.workspace_id, device_id))


@router.patch("/{device_id}")
def patch_device(device_id: int, body: DevicePatch, ctx: deps.Console = Depends(deps.console)):
    """输入：浏览器 id、{name} → 输出：浏览器（只能改名；名称为空或超过 64 个字、或带了别的键 → 422）。"""
    kw = {"name": body.name} if "name" in body.model_fields_set else {}
    return respond(devices.update_device(ctx.conn, ctx.workspace_id, device_id, actor_user_id=ctx.user_id, **kw))


@router.post("/{device_id}/remove")
def remove_device(device_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：浏览器 id → 输出：浏览器（hidden_at 非空）。只是从列表里清掉；它再次联系服务端时自动回到列表。

    这个浏览器有未结束的执行批次 → 409 CONFLICT「这个浏览器正在执行，结束后才能移除」（detail.run_id）。
    """
    return respond(devices.remove_device(ctx.conn, ctx.workspace_id, device_id, ctx.user_id))
