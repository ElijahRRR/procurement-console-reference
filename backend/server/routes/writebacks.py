"""飞书写回队列（契约 §6、§8，形状见 §10「写回」）：列表与人工重试（只补写回，不重跑采购）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from server import deps
from server.serialize import respond
from services import writeback

router = APIRouter(prefix="/writebacks", tags=["writebacks"])


@router.get("")
def list_writebacks(status: str | None = None, pg: deps.Paging = Depends(deps.paging),
                    ctx: deps.Console = Depends(deps.console)):
    """输入：状态（pending / done / failed / superseded，缺省全部）、分页 → 输出：{total, rows: [写回]}（新的在前）。"""
    return respond(writeback.list_writebacks(ctx.conn, ctx.workspace_id, status or None, pg.page, pg.page_size))


@router.get("/{writeback_id}")
def get_writeback(writeback_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：写回 id → 输出：写回记录；别的工作区 → 404。"""
    return respond(writeback.get(ctx.conn, ctx.workspace_id, writeback_id))


@router.post("/{writeback_id}/retry")
def retry_writeback(writeback_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：写回 id → 输出：重新排队后的写回记录（或取代它的新版本）；已成功 / 已被取代 → 422。"""
    return respond(writeback.retry(ctx.conn, ctx.workspace_id, writeback_id, ctx.user_id))
