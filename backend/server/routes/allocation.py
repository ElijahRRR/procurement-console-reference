"""分配（契约 §3.2、§8）：人工重新分配、最近一次分配摘要。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from server import deps
from server.serialize import respond
from services import allocation

router = APIRouter(prefix="/allocation", tags=["allocation"])


@router.post("/run")
def run_allocation(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：本次分配摘要（契约 §10「分配摘要」，trigger='manual'）。

    来源指定了买家号的任务按指定分配（第 0 步），其余自动分配的任务按当前账号配置重算，人工改派的保持不动。
    stats = {considered, assigned, coverage, fill, designated, unassigned}，assigned = coverage + fill + designated；
    unassigned 里第 0 步分不出去的项带 designated: true。"""
    return respond(allocation.allocate(ctx.conn, ctx.workspace_id, trigger="manual", actor_kind="user",
                                       actor_id=ctx.user_id))


@router.get("/last")
def last_allocation(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：最近一次分配摘要；从未分配过 → null（旧版本留下的记录补上 stats.designated = 0）。"""
    return respond(allocation.last_run(ctx.conn, ctx.workspace_id))
