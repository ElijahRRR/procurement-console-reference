"""事件流水（契约 §8 GET /events，形状见 §10「事件」）。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from server import deps
from server.serialize import page, respond
from services import events

router = APIRouter(prefix="/events", tags=["events"])

#: services/events.list_for 没有偏移量，最多取这么多条再切页
EVENTS_WINDOW = 1000

Entity = Literal["order", "attempt", "account", "device", "connection", "batch", "settings", "run"]


def _event_out(e: dict) -> dict:
    return {"id": e["id"], "entity": e["entity"], "entity_id": e["entity_id"], "order_id": e["order_id"],
            "at": e["at"], "actor_kind": e["actor_kind"], "actor_id": e["actor_id"],
            "actor_name": e.get("actor_name"), "kind": e["kind"], "detail": e["detail"]}


@router.get("")
def list_events(entity: Entity | None = None, entity_id: int | None = Query(None, ge=1),
                order_id: int | None = Query(None, ge=1), pg: deps.Paging = Depends(deps.paging),
                ctx: deps.Console = Depends(deps.console)):
    """输入：对象种类、对象 id、任务 id（全部可选）、分页 → 输出：{total, rows: [事件]}（新的在前，最多最近 1000 条）。"""
    found = events.list_for(ctx.conn, ctx.workspace_id, entity=entity, entity_id=entity_id, order_id=order_id,
                            limit=EVENTS_WINDOW)
    return respond(page([_event_out(e) for e in found], pg.page, pg.page_size))
