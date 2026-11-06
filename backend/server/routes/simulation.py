"""模拟执行（契约 §5「模拟」）：一键清除模拟结果。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from server import deps
from server.serialize import respond
from services import simulation

router = APIRouter(prefix="/simulation", tags=["simulation"])


@router.post("/reset")
def reset_simulation(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：{runs_ended, attempts_reset, orders_restored, orders_skipped, accounts_resumed}。"""
    return respond(simulation.reset(ctx.conn, ctx.workspace_id, ctx.user_id))
