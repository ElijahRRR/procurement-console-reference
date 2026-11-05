"""概览（契约 §8 GET /overview，形状见 §10「概览」）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from server import deps
from server.serialize import respond
from services import overview as overview_svc

router = APIRouter(tags=["overview"])


@router.get("/overview")
def get_overview(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：{bj_date, counts, today_executable, future_assigned, accounts, devices, recent_exceptions,
    last_allocation, simulation_only, feishu_configured}。"""
    return respond(overview_svc.overview(ctx.conn, ctx.workspace_id))
