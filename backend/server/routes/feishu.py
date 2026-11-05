"""飞书接入（契约 §6、§8，形状见 §10「飞书连接」）：状态、系统字段、链接解析、字段、样本预览、连接管理、
立即读取、写回验收、读取记录。

飞书客户端一律由 services/feishu_connections.make_client 创建（服务函数不传 client 时自己调用它；测试 monkeypatch
它换成假客户端）。未配置应用凭证 → 409 FEISHU_NOT_CONFIGURED；飞书接口报错 → 502 FEISHU_ERROR。
DELETE 连接 = 停用（契约 §8），不删数据。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from server import deps
from server.serialize import page, respond
from services import feishu_connections, feishu_intake
from services.errors import DomainError

router = APIRouter(prefix="/feishu", tags=["feishu"])

#: 读取记录列表一次最多取的条数（服务层上限）
RUNS_WINDOW = 200


class LinkBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    link: str


class FieldsBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app_token: str
    table_id: str


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app_token: str
    table_id: str
    view_id: str | None = None
    field_map: Any = None
    admission: Any = None
    limit: Any = 20


class ConnectionBody(BaseModel):
    """连接参数（新建与修改共用；未知键、类型与取值由服务层校验并给出中文原因）。"""

    model_config = ConfigDict(extra="allow")

    name: Any = None
    link: Any = None
    app_token: Any = None
    source_table_id: Any = None
    source_view_id: Any = None
    result_table_id: Any = None
    field_map: Any = None
    admission: Any = None
    writeback_map: Any = None
    writeback_enabled: Any = None
    poll_minutes: Any = None
    enabled: Any = None


class VerifyBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    record_id: str


@router.get("/status")
def feishu_status(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：{configured}（服务端是否配置了 FEISHU_APP_ID / FEISHU_APP_SECRET）。"""
    return respond(feishu_connections.status())


@router.get("/system-fields")
def system_fields(ctx: deps.Console = Depends(deps.console)):
    """输入：当前会话 → 输出：{source_fields: [{key, label, required}], result_fields: [{key, label}]}。"""
    return respond(feishu_connections.system_fields())


@router.post("/resolve-link")
def resolve_link(body: LinkBody, ctx: deps.Console = Depends(deps.console)):
    """输入：{link} → 输出：{app_token, table_id, view_id, tables: [{table_id, name}]}。"""
    return respond(feishu_connections.resolve_link(body.link))


@router.post("/fields")
def list_fields(body: FieldsBody, ctx: deps.Console = Depends(deps.console)):
    """输入：{app_token, table_id} → 输出：{fields: [...], suggested_field_map, suggested_writeback_map}。"""
    return respond(feishu_connections.fields(body.app_token, body.table_id))


@router.post("/preview")
def preview(body: PreviewBody, ctx: deps.Console = Depends(deps.console)):
    """输入：{app_token, table_id, view_id, field_map, admission, limit} → 输出：{stats, records}（只读，不写库也不写飞书）。"""
    return respond(feishu_connections.preview(
        ctx.conn, ctx.workspace_id, app_token=body.app_token, table_id=body.table_id, view_id=body.view_id,
        field_map=body.field_map, admission=body.admission, limit=body.limit))


@router.get("/connections")
def list_connections(pg: deps.Paging = Depends(deps.paging), ctx: deps.Console = Depends(deps.console)):
    """输入：分页 → 输出：{total, rows: [飞书连接]}（启用的在前）。"""
    return respond(page(feishu_connections.list_connections(ctx.conn, ctx.workspace_id), pg.page, pg.page_size))


@router.post("/connections")
def create_connection(body: ConnectionBody, ctx: deps.Console = Depends(deps.console)):
    """输入：连接参数 {name, link, field_map, admission?, writeback_map?, …} → 输出：新连接（同一张来源表已连接 → 409）。"""
    return respond(feishu_connections.create(ctx.conn, ctx.workspace_id, body.model_dump(exclude_unset=True),
                                             ctx.user_id))


@router.get("/connections/{connection_id}")
def get_connection(connection_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：连接 id → 输出：飞书连接；别的工作区 → 404。"""
    return respond(feishu_connections.get(ctx.conn, ctx.workspace_id, connection_id))


@router.patch("/connections/{connection_id}")
def patch_connection(connection_id: int, body: ConnectionBody, ctx: deps.Console = Depends(deps.console)):
    """输入：连接 id、要改的项 → 输出：改后的连接（改结果表或写回列 → 写回验收作废）。"""
    return respond(feishu_connections.update(ctx.conn, ctx.workspace_id, connection_id,
                                             body.model_dump(exclude_unset=True), ctx.user_id))


@router.delete("/connections/{connection_id}")
def disable_connection(connection_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：连接 id → 输出：连接（enabled=false：停用而不删除，可再 PATCH 启用）。"""
    return respond(feishu_connections.disable(ctx.conn, ctx.workspace_id, connection_id, ctx.user_id))


@router.post("/connections/{connection_id}/sync")
def sync_connection(connection_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：连接 id → 输出：本次读取的统计 {read, new, update, …, run_id, problems, allocation}。

    读飞书失败时服务层已把失败的读取记录写进本事务：先提交它再返回错误（否则失败历史随回滚丢失）。
    """
    try:
        return respond(feishu_intake.run_connection(ctx.conn, ctx.workspace_id, connection_id, trigger="manual"))
    except DomainError as exc:
        if "run_id" in exc.detail:
            ctx.conn.commit()
        raise


@router.post("/connections/{connection_id}/verify-writeback")
def verify_writeback(connection_id: int, body: VerifyBody, ctx: deps.Console = Depends(deps.console)):
    """输入：连接 id、{record_id}（结果表里的测试记录）→ 输出：{ok, mismatches, written, restored, …, connection}。"""
    return respond(feishu_connections.verify_writeback(ctx.conn, ctx.workspace_id, connection_id, body.record_id,
                                                       ctx.user_id))


@router.get("/connections/{connection_id}/runs")
def list_runs(connection_id: int, pg: deps.Paging = Depends(deps.paging), ctx: deps.Console = Depends(deps.console)):
    """输入：连接 id、分页 → 输出：{total, rows: [读取记录]}（新的在前，最多最近 200 条）。"""
    found = feishu_connections.list_runs(ctx.conn, ctx.workspace_id, connection_id, limit=RUNS_WINDOW)
    return respond(page(found, pg.page, pg.page_size))
