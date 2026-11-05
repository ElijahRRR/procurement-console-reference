"""买家账号（契约 §8，形状见 §10「账号」）：列表、新建、详情、修改、暂停 / 恢复执行、采用插件识别的买家号 ID、归档。

字段的类型与取值由 services/accounts 校验（中文原因、422 VALIDATION）；前端整份回传的只读键（quota_today、devices …）
由服务层忽略，未知键 → 422。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from server import deps
from server.serialize import page, respond
from services import accounts

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountBody(BaseModel):
    """账号字段（全部可选，PATCH 只改传了的键）。"""

    model_config = ConfigDict(extra="allow")

    name: Any = None
    amazon_customer_id: Any = None
    site: Any = None
    min_amount: Any = None
    max_amount: Any = None
    daily_limit: Any = None
    priority: Any = None
    max_delivery_days: Any = None
    purchase_enabled: Any = None
    sync_enabled: Any = None
    purchase_mode: Any = None
    schedule_times: Any = None
    default_device_id: Any = None
    expected_card_last4: Any = None
    notes: Any = None


class PauseBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str | None = None


class AdoptBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    device_id: int


@router.get("")
def list_accounts(include_archived: bool = False, pg: deps.Paging = Depends(deps.paging),
                  ctx: deps.Console = Depends(deps.console)):
    """输入：是否含已归档、分页 → 输出：{total, rows: [账号]}（按名称；含当天额度、绑定设备、当前执行批次）。"""
    rows = accounts.list_accounts(ctx.conn, ctx.workspace_id, include_archived=include_archived)
    return respond(page(rows, pg.page, pg.page_size))


@router.post("")
def create_account(body: AccountBody, ctx: deps.Console = Depends(deps.console)):
    """输入：账号字段（name 必填）→ 输出：新账号（新建后立即重新分配）。"""
    return respond(accounts.create(ctx.conn, ctx.workspace_id, body.model_dump(exclude_unset=True), ctx.user_id))


@router.get("/{account_id}")
def get_account(account_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：账号 id → 输出：账号；别的工作区 → 404。"""
    return respond(accounts.get(ctx.conn, ctx.workspace_id, account_id))


@router.patch("/{account_id}")
def patch_account(account_id: int, body: AccountBody, ctx: deps.Console = Depends(deps.console)):
    """输入：账号 id、要改的字段 → 输出：改后的账号（影响分配的字段变化后立即重新分配）。"""
    return respond(accounts.update(ctx.conn, ctx.workspace_id, account_id, body.model_dump(exclude_unset=True),
                                   ctx.user_id))


@router.post("/{account_id}/pause")
def pause_account(account_id: int, body: PauseBody | None = None, ctx: deps.Console = Depends(deps.console)):
    """输入：账号 id、{reason} → 输出：账号（暂停执行；已分配的任务保留）。"""
    reason = body.reason if body is not None else None
    return respond(accounts.pause(ctx.conn, ctx.workspace_id, account_id, reason, actor_kind="user",
                                  actor_id=ctx.user_id))


@router.post("/{account_id}/resume")
def resume_account(account_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：账号 id → 输出：账号（恢复执行并立即重新分配）。"""
    return respond(accounts.resume(ctx.conn, ctx.workspace_id, account_id, actor_kind="user", actor_id=ctx.user_id))


@router.post("/{account_id}/adopt-customer-id")
def adopt_customer_id(account_id: int, body: AdoptBody, ctx: deps.Console = Depends(deps.console)):
    """输入：账号 id、{device_id} → 输出：账号（把该设备识别到的买家号 ID 登记为账号的 ID）。"""
    return respond(accounts.adopt_customer_id(ctx.conn, ctx.workspace_id, account_id, body.device_id, ctx.user_id))


@router.post("/{account_id}/archive")
def archive_account(account_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：账号 id → 输出：账号（已归档；有执行中或待核实的采购时拒绝）。"""
    return respond(accounts.archive(ctx.conn, ctx.workspace_id, account_id, ctx.user_id))
