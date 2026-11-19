"""买家账号（契约 §8，形状见 §10「账号」）：列表、新建、详情、修改、暂停 / 恢复执行、归档，以及待登记的买家号
（插件读到的、或来源指定了的、还没有账号登记的买家号 ID）：列表、忽略、恢复。

- 「登记」一个待登记的买家号 = POST /accounts（带 amazon_customer_id）；「填到已有账号」= PATCH /accounts/{id}。
- 字段的类型与取值由 services/accounts 校验（中文原因、422 VALIDATION）；前端整份回传的只读键（quota_today、
  browsers …）由服务层忽略，未知键 → 422。
- 路由顺序：/accounts/discovered… 必须排在 /accounts/{account_id} 之前，否则会被当成账号 id。
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
    expected_card_last4: Any = None
    notes: Any = None


class PauseBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str | None = None


class BuyerIdBody(BaseModel):
    """买家号 ID（形状由服务层校验：大写、去空白后须以 A 开头、至少 9 位字母或数字）。"""

    model_config = ConfigDict(extra="ignore")

    customer_id: Any = None


@router.get("")
def list_accounts(include_archived: bool = False, pg: deps.Paging = Depends(deps.paging),
                  ctx: deps.Console = Depends(deps.console)):
    """输入：是否含已归档、分页 → 输出：{total, rows: [账号]}（按名称；含当天额度、所在浏览器 browsers、
    当前执行批次）。"""
    rows = accounts.list_accounts(ctx.conn, ctx.workspace_id, include_archived=include_archived)
    return respond(page(rows, pg.page, pg.page_size))


@router.post("")
def create_account(body: AccountBody, ctx: deps.Console = Depends(deps.console)):
    """输入：账号字段（name 必填）→ 输出：新账号（新建后立即重新分配，不论采购开关：来源按名称或买家号 ID 指定给它的
    任务随即落到它上面）。"""
    return respond(accounts.create(ctx.conn, ctx.workspace_id, body.model_dump(exclude_unset=True), ctx.user_id))


@router.get("/discovered")
def list_discovered(include_ignored: bool = False, ctx: deps.Console = Depends(deps.console)):
    """输入：是否含已忽略的（include_ignored=0|1）→ 输出：{items: [待登记的买家号]}（按最近上报时间倒序，没有浏览器
    上报的排在后面）。

    每项 {customer_id, ignored, browsers: [{id, name, online, last_seen_at}], last_reported_at, name_hint,
    source_orders}：还没有被任何未归档账号登记的买家号 ID，来自两处的并集——本工作区未移除的浏览器上报的登录买家号，
    与来源指定（未采购任务上形状合格的买家号 ID）。source_orders = 指定它的未采购任务数（0 = 只来自浏览器）；只来自
    来源的项 browsers 为空数组、last_reported_at 为 null。name_hint 优先取这些任务最近写的买家号，其次是来源数据里
    同一买家号 ID 最近出现的买家号（没有为 null）。include_ignored=1 时另外列出忽略名单里的**全部** ID（ignored: true，
    不要求此刻有浏览器登录着它），供恢复。
    """
    return respond({"items": accounts.discovered(ctx.conn, ctx.workspace_id, include_ignored=include_ignored)})


@router.post("/discovered/ignore")
def ignore_discovered(body: BuyerIdBody, ctx: deps.Console = Depends(deps.console)):
    """输入：{customer_id} → 输出：{ok: true}（忽略：不再出现在待登记列表里，登录着它的浏览器分不到任务；幂等）。
    ID 形状不对、或已登记在未归档账号上 → 422 VALIDATION。"""
    accounts.ignore_buyer_id(ctx.conn, ctx.workspace_id, body.customer_id, ctx.user_id)
    return respond({"ok": True})


@router.post("/discovered/unignore")
def unignore_discovered(body: BuyerIdBody, ctx: deps.Console = Depends(deps.console)):
    """输入：{customer_id} → 输出：{ok: true}（恢复：回到待登记列表；幂等）。ID 形状不对 → 422 VALIDATION。"""
    accounts.unignore_buyer_id(ctx.conn, ctx.workspace_id, body.customer_id, ctx.user_id)
    return respond({"ok": True})


@router.get("/{account_id}")
def get_account(account_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：账号 id → 输出：账号；别的工作区 → 404。"""
    return respond(accounts.get(ctx.conn, ctx.workspace_id, account_id))


@router.patch("/{account_id}")
def patch_account(account_id: int, body: AccountBody, ctx: deps.Console = Depends(deps.console)):
    """输入：账号 id、要改的字段 → 输出：改后的账号（影响分配的字段变化后立即重新分配；名称与买家号 ID 也算——
    来源指定靠它们对应账号）。

    修改或清空 amazon_customer_id 时，账号有未结束批次、在途或结果待核实的尝试 → 409 CONFLICT。
    """
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


@router.post("/{account_id}/archive")
def archive_account(account_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：账号 id → 输出：账号（已归档；有执行中或待核实的采购时拒绝）。"""
    return respond(accounts.archive(ctx.conn, ctx.workspace_id, account_id, ctx.user_id))
