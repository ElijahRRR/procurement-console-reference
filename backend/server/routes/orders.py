"""采购任务（契约 §8）：列表、详情与全部人工动作。

动作成功一律返回最新的任务详情（含 allowed_actions，前端据此显示按钮）；不允许的状态迁移 → 409 INVALID_TRANSITION。
覆盖值接口区分「没传」与「传 null」：没传 = 不改，null = 清除（契约 §8）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from server import deps
from server.serialize import respond
from services import orders

router = APIRouter(prefix="/orders", tags=["orders"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class NoteBody(_Body):
    note: str | None = None


class ReviewBody(_Body):
    decision: str
    note: str | None = None


class BulkReviewBody(_Body):
    ids: list[Any]
    decision: str
    note: str | None = None


class AssignBody(_Body):
    account_id: int


class OverrideBody(_Body):
    """asin / quantity / price_cap：没传 = 不改，null = 清除；取值由服务层校验。"""

    asin: Any = None
    quantity: Any = None
    price_cap: Any = None
    note: str | None = None


class ResolveUnknownBody(_Body):
    outcome: str
    amazon_order_no: str | None = None
    amounts: dict[str, Any] | None = None
    note: str | None = None


class RepurchaseBody(_Body):
    decision: str
    note: str | None = None


class ResolveConflictBody(_Body):
    choice: str
    note: str | None = None


class WritebackTargetBody(_Body):
    source_id: int


def _note(body: NoteBody | None) -> str | None:
    return body.note if body is not None else None


@router.get("")
def list_orders(view: str = "all", q: str | None = Query(None, max_length=200),
                account_id: int | None = Query(None, ge=1), source: str | None = None,
                needs_attention: bool | None = None,
                pg: deps.Paging = Depends(deps.paging), ctx: deps.Console = Depends(deps.console)):
    """输入：视图（today / future / unassigned / 各状态 / all）、搜索词、账号、来源种类、needs_attention（true / false，
    可选）、分页 → 输出：{total, rows: [任务列表行]}（各条件可组合，total 按全部条件计数）。"""
    return respond(orders.list_orders(ctx.conn, ctx.workspace_id, view=view or "all", q=q, account_id=account_id,
                                      source=source or None, needs_attention=needs_attention, page=pg.page,
                                      page_size=pg.page_size))


@router.post("/bulk-review")
def bulk_review(body: BulkReviewBody, ctx: deps.Console = Depends(deps.console)):
    """输入：{ids, decision: approve | reject, note} → 输出：{updated: [id], skipped: [{id, reason}]}。"""
    return respond(orders.bulk_review(ctx.conn, ctx.workspace_id, body.ids, body.decision, body.note,
                                      actor_user_id=ctx.user_id))


@router.get("/{order_id}")
def get_order(order_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id → 输出：任务详情（原值、覆盖值、有效值、来源与快照、尝试、事件、allowed_actions）；别的工作区 → 404。"""
    return respond(orders.get_detail(ctx.conn, ctx.workspace_id, order_id))


@router.post("/{order_id}/review")
def review_order(order_id: int, body: ReviewBody, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{decision: approve | reject, note} → 输出：任务详情（控制台人审，只限开始前的状态）。"""
    return respond(orders.review(ctx.conn, ctx.workspace_id, order_id, body.decision, body.note,
                                 actor_user_id=ctx.user_id))


@router.post("/{order_id}/assign")
def assign_order(order_id: int, body: AssignBody, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{account_id} → 输出：任务详情（人工改派，assignment_kind='manual'）。"""
    return respond(orders.assign_manual(ctx.conn, ctx.workspace_id, order_id, body.account_id,
                                        actor_user_id=ctx.user_id))


@router.post("/{order_id}/unassign")
def unassign_order(order_id: int, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id → 输出：任务详情（已分配 → 待分配）。"""
    return respond(orders.unassign(ctx.conn, ctx.workspace_id, order_id, actor_user_id=ctx.user_id))


@router.post("/{order_id}/override")
def override_order(order_id: int, body: OverrideBody, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{asin?, quantity?, price_cap?, note}（没传 = 不改，null = 清除）→ 输出：任务详情。"""
    given = body.model_fields_set
    kw = {k: getattr(body, k) for k in ("asin", "quantity", "price_cap") if k in given}
    return respond(orders.set_override(ctx.conn, ctx.workspace_id, order_id, note=body.note,
                                       actor_user_id=ctx.user_id, **kw))


@router.post("/{order_id}/resume")
def resume_order(order_id: int, body: NoteBody | None = None, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{note} → 输出：任务详情（异常 → 按派生规则重新校验）。"""
    return respond(orders.resume(ctx.conn, ctx.workspace_id, order_id, _note(body), actor_user_id=ctx.user_id))


@router.post("/{order_id}/cancel")
def cancel_order(order_id: int, body: NoteBody | None = None, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{note} → 输出：任务详情（人工取消，只限开始前或异常）。"""
    return respond(orders.cancel(ctx.conn, ctx.workspace_id, order_id, _note(body), actor_user_id=ctx.user_id))


@router.post("/{order_id}/uncancel")
def uncancel_order(order_id: int, body: NoteBody | None = None, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{note?} → 输出：任务详情（撤销人工取消；上游仍为取消时拒绝）。"""
    return respond(orders.uncancel(ctx.conn, ctx.workspace_id, order_id, _note(body), actor_user_id=ctx.user_id))


@router.post("/{order_id}/resolve-unknown")
def resolve_unknown(order_id: int, body: ResolveUnknownBody, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{outcome: ordered | not_ordered, amazon_order_no?, amounts?, note} → 输出：任务详情（结果待核实的人工核实）。"""
    return respond(orders.resolve_unknown(ctx.conn, ctx.workspace_id, order_id, body.outcome,
                                          amazon_order_no=body.amazon_order_no, amounts=body.amounts,
                                          note=body.note, actor_user_id=ctx.user_id))


@router.post("/{order_id}/mark-amazon-cancelled")
def mark_amazon_cancelled(order_id: int, body: NoteBody | None = None, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{note} → 输出：任务详情（已采购 → 待决定重采）。"""
    return respond(orders.mark_amazon_cancelled(ctx.conn, ctx.workspace_id, order_id, _note(body),
                                                actor_user_id=ctx.user_id))


@router.post("/{order_id}/repurchase")
def decide_repurchase(order_id: int, body: RepurchaseBody, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{decision: approve | decline, note} → 输出：任务详情（同意重采回到派生状态 / 不重采关闭）。"""
    return respond(orders.decide_repurchase(ctx.conn, ctx.workspace_id, order_id, body.decision, body.note,
                                            actor_user_id=ctx.user_id))


@router.post("/{order_id}/resolve-conflict")
def resolve_conflict(order_id: int, body: ResolveConflictBody, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{choice: keep | accept, note} → 输出：任务详情（保留当前 / 接受来源新值）。"""
    return respond(orders.resolve_conflict(ctx.conn, ctx.workspace_id, order_id, body.choice, body.note,
                                           actor_user_id=ctx.user_id))


@router.post("/{order_id}/clear-attention")
def clear_attention(order_id: int, body: NoteBody | None = None, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{note} → 输出：任务详情（清除「需要留意」提醒，原提醒写进事件）。"""
    return respond(orders.clear_attention(ctx.conn, ctx.workspace_id, order_id, _note(body),
                                          actor_user_id=ctx.user_id))


@router.post("/{order_id}/writeback-target")
def set_writeback_target(order_id: int, body: WritebackTargetBody, ctx: deps.Console = Depends(deps.console)):
    """输入：任务 id、{source_id}（该任务的一条飞书来源）→ 输出：任务详情（指定为唯一写回目标；已采购的真实结果随即入队）。"""
    return respond(orders.set_writeback_target(ctx.conn, ctx.workspace_id, order_id, body.source_id,
                                               actor_user_id=ctx.user_id))
