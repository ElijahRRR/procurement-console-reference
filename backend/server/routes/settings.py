"""全局设置（契约 §8 GET/PATCH /settings，形状见 §10「设置」）。

校验在 services/settings_svc：默认配送上限 1–60 的整数、物流同步时间 HH:MM、本阶段 simulation_only 只能为 true、
未知键 → 422 VALIDATION；只读键（real_purchase_available）忽略。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from server import deps
from server.serialize import respond
from services import settings_svc

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsPatch(BaseModel):
    """要改的设置项（全部可选；类型与取值由服务层校验并给出中文原因）。"""

    model_config = ConfigDict(extra="allow")

    default_max_delivery_days: Any = None
    simulation_only: Any = None
    logistics_sync_time: Any = None


@router.get("")
def get_settings(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：{default_max_delivery_days, simulation_only, logistics_sync_time, real_purchase_available}。"""
    return respond(settings_svc.get(ctx.conn, ctx.workspace_id))


@router.patch("")
def patch_settings(body: SettingsPatch, ctx: deps.Console = Depends(deps.console)):
    """输入：要改的设置项 → 输出：改后的设置；取值不合格 → 422 VALIDATION。"""
    patch = body.model_dump(exclude_unset=True)
    return respond(settings_svc.update(ctx.conn, ctx.workspace_id, patch, ctx.user_id))
