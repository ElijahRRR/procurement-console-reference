"""文件导入（契约 §7、§8，形状见 §10「导入」）：标准模板、上传、预览、批次行、提交 / 放弃、批次列表、问题行、个人模板。

上传用 multipart 字段 `file`，10 MB 以内（超过 → 413，app.py 的中间件还会按 Content-Length 提前拒绝）；
格式、编码、表头的校验在 services/sheet_io 与 services/imports。不修改用户的原文件。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, ConfigDict

from server import deps
from server.serialize import page, respond, xlsx_response
from services import imports, sheet_io
from services.errors import DomainError

router = APIRouter(tags=["imports"])

MAX_UPLOAD_BYTES = sheet_io.MAX_BYTES
TEMPLATE_FILENAME = "采购导入模板.xlsx"


def too_large() -> DomainError:
    """输入：无 → 输出：上传文件过大的错误（413，code 为 VALIDATION）。"""
    mb = MAX_UPLOAD_BYTES // (1024 * 1024)
    return DomainError("VALIDATION", f"文件超过 {mb} MB，请拆分后再上传", 413, {"max_bytes": MAX_UPLOAD_BYTES})


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sheet: str | None = None
    mapping: dict[str, Any] | None = None
    admission: dict[str, Any] | None = None


class TemplateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Any = None
    mapping: Any = None


@router.get("/imports/template.xlsx")
def download_template(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：标准模板 xlsx 附件（列名与飞书统一，只有平台订单号必填）。"""
    return xlsx_response(imports.template_xlsx(), TEMPLATE_FILENAME)


@router.get("/imports")
def list_batches(pg: deps.Paging = Depends(deps.paging), ctx: deps.Console = Depends(deps.console)):
    """输入：分页 → 输出：{total, rows: [批次]}（新的在前）。"""
    return respond(imports.list_batches(ctx.conn, ctx.workspace_id, pg.page, pg.page_size))


@router.post("/imports")
def upload(file: UploadFile = File(...), ctx: deps.Console = Depends(deps.console)):
    """输入：multipart 字段 file（.xlsx / .csv，≤10 MB）→ 输出：{batch_id, filename, sheets, sheet, headers,
    suggested_mapping, system_fields, templates, …}；超过 10 MB → 413。"""
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise too_large()
    return respond(imports.upload(ctx.conn, ctx.workspace_id, filename=file.filename or "", data=data,
                                  actor_user_id=ctx.user_id))


@router.get("/imports/{batch_id}")
def get_batch(batch_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：批次 id → 输出：批次 + sheets: [{name, row_count, headers}] + headers（所选工作表的表头；原件已清掉时
    为空列表）；别的工作区 → 404。"""
    return respond(imports.get_batch(ctx.conn, ctx.workspace_id, batch_id, with_sheets=True))


@router.post("/imports/{batch_id}/preview")
def preview(batch_id: int, body: PreviewBody | None = None, ctx: deps.Console = Depends(deps.console)):
    """输入：批次 id、{sheet, mapping: {表头: 系统字段 | null}, admission?} → 输出：{batch, stats, rows: {total, rows}}。"""
    b = body or PreviewBody()
    return respond(imports.preview(ctx.conn, ctx.workspace_id, batch_id, sheet=b.sheet, mapping=b.mapping,
                                   admission=b.admission))


@router.get("/imports/{batch_id}/rows")
def batch_rows(batch_id: int, verdict: str | None = None, pg: deps.Paging = Depends(deps.paging),
               ctx: deps.Console = Depends(deps.console)):
    """输入：批次 id、结论过滤（new … invalid，或 problems）、分页 → 输出：{total, rows}（按行号）。"""
    return respond(imports.rows(ctx.conn, ctx.workspace_id, batch_id, verdict=verdict or None, page=pg.page,
                                page_size=pg.page_size))


@router.post("/imports/{batch_id}/commit")
def commit(batch_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：批次 id → 输出：{batch, stats, allocation}（逐行进入任务流，随后分配一次）；重复提交 → 409。"""
    return respond(imports.commit(ctx.conn, ctx.workspace_id, batch_id, ctx.user_id))


@router.post("/imports/{batch_id}/discard")
def discard(batch_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：批次 id → 输出：批次（已放弃；已提交的批次不能放弃 → 422）。"""
    return respond(imports.discard(ctx.conn, ctx.workspace_id, batch_id, ctx.user_id))


@router.get("/imports/{batch_id}/problems.xlsx")
def download_problems(batch_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：批次 id → 输出：问题行 xlsx 附件（原列 + 结论 + 原因 + 原行号）。"""
    data = imports.problems_xlsx(ctx.conn, ctx.workspace_id, batch_id)
    return xlsx_response(data, f"问题行-批次{batch_id}.xlsx")


@router.get("/import-templates")
def list_templates(pg: deps.Paging = Depends(deps.paging), ctx: deps.Console = Depends(deps.console)):
    """输入：分页 → 输出：{total, rows: [{id, name, mapping, created_at, created_by_name}]}（按名称）。"""
    return respond(page(imports.list_templates(ctx.conn, ctx.workspace_id), pg.page, pg.page_size))


@router.post("/import-templates")
def save_template(body: TemplateBody, ctx: deps.Console = Depends(deps.console)):
    """输入：{name, mapping: {表头: 系统字段 | null}} → 输出：模板（同名覆盖）。"""
    return respond(imports.save_template(ctx.conn, ctx.workspace_id, body.name, body.mapping, ctx.user_id))


@router.delete("/import-templates/{template_id}")
def delete_template(template_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：模板 id → 输出：{id}；别的工作区 → 404。"""
    return respond(imports.delete_template(ctx.conn, ctx.workspace_id, template_id, ctx.user_id))
