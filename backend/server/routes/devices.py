"""插件设备（契约 §8，形状见 §10「设备」）：列表、配对码、详情、改名 / 绑定账号、撤销。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from server import deps
from server.serialize import page, respond
from services import devices

router = APIRouter(prefix="/devices", tags=["devices"])


class DevicePatch(BaseModel):
    """name / bound_account_id：没传 = 不改；bound_account_id 传 null = 解除绑定。取值由服务层校验。"""

    model_config = ConfigDict(extra="ignore")

    name: Any = None
    bound_account_id: Any = None


@router.get("")
def list_devices(pg: deps.Paging = Depends(deps.paging), ctx: deps.Console = Depends(deps.console)):
    """输入：分页 → 输出：{total, rows: [设备]}（未撤销的在前；online = 3 分钟内有心跳）。"""
    return respond(page(devices.list_devices(ctx.conn, ctx.workspace_id), pg.page, pg.page_size))


@router.post("/pairing-codes")
def create_pairing_code(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：{code, expires_at}（8 位一次性配对码，10 分钟有效）。"""
    return respond(devices.create_pairing_code(ctx.conn, ctx.workspace_id, ctx.user_id))


@router.get("/{device_id}")
def get_device(device_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：设备 id → 输出：设备；别的工作区 → 404。"""
    return respond(devices.get_device(ctx.conn, ctx.workspace_id, device_id))


@router.patch("/{device_id}")
def patch_device(device_id: int, body: DevicePatch, ctx: deps.Console = Depends(deps.console)):
    """输入：设备 id、{name?, bound_account_id?} → 输出：设备（执行批次进行中不能改绑 → 409）。"""
    kw = {k: getattr(body, k) for k in ("name", "bound_account_id") if k in body.model_fields_set}
    return respond(devices.update_device(ctx.conn, ctx.workspace_id, device_id, actor_user_id=ctx.user_id, **kw))


@router.post("/{device_id}/revoke")
def revoke_device(device_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：设备 id → 输出：设备（撤销授权：令牌立即失效，进行中的批次按租约过期处理）。"""
    return respond(devices.revoke(ctx.conn, ctx.workspace_id, device_id, ctx.user_id))
