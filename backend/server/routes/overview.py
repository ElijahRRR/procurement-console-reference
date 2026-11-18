"""概览（契约 §8 GET /overview，形状见 §10「概览」）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from server import deps
from server.serialize import respond
from services import overview as overview_svc

router = APIRouter(tags=["overview"])


@router.get("/overview")
def get_overview(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：{bj_date, counts, needs_attention, today_executable, future_assigned, accounts, devices,
    discovered_buyers, recent_exceptions, last_allocation, simulation_only, feishu_configured}。

    devices（浏览器）只计未移除的；discovered_buyers = 未忽略的待登记买家号个数。"""
    return respond(overview_svc.overview(ctx.conn, ctx.workspace_id))
