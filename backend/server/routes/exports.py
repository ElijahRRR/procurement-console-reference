"""结果导出（契约 §7「导出」、§8 GET /exports/orders.xlsx）：按批次或按视图导出采购结果 xlsx。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from server import deps
from server.serialize import xlsx_response
from services import clock, exports

router = APIRouter(prefix="/exports", tags=["exports"])


@router.get("/orders.xlsx")
def export_orders(batch_id: int | None = Query(None, ge=1), view: str | None = None,
                  ctx: deps.Console = Depends(deps.console)):
    """输入：batch_id（优先）或视图（缺省 all）→ 输出：采购结果 xlsx 附件；批次不存在 → 404，未提交 → 422。"""
    data = exports.orders_xlsx(ctx.conn, ctx.workspace_id, batch_id=batch_id, view=view or None)
    stamp = f"{clock.bj_today():%Y%m%d}"
    name = f"采购结果-批次{batch_id}-{stamp}.xlsx" if batch_id is not None else f"采购结果-{view or 'all'}-{stamp}.xlsx"
    return xlsx_response(data, name)
