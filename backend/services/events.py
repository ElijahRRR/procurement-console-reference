"""事件流水：每次状态变化、人工动作、来源变化都写一条。"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from services import clock

ENTITIES = ("order", "attempt", "account", "device", "connection", "batch", "settings", "run")
ACTOR_KINDS = ("user", "device", "system", "source")


def record(conn, workspace_id: int, entity: str, entity_id: int | None, kind: str, *,
           actor_kind: str = "system", actor_id: int | None = None,
           detail: dict[str, Any] | None = None, order_id: int | None = None) -> int:
    """输入：工作区、对象、事件种类、操作者、细节 → 输出：事件 id。"""
    if entity not in ENTITIES:
        raise ValueError(f"未知事件对象 {entity}")
    if actor_kind not in ACTOR_KINDS:
        raise ValueError(f"未知操作者种类 {actor_kind}")
    if entity == "order" and order_id is None:
        order_id = entity_id
    row = conn.execute(
        """
        INSERT INTO procure.events (workspace_id, entity, entity_id, order_id, at, actor_kind, actor_id, kind, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (workspace_id, entity, entity_id, order_id, clock.now(), actor_kind, actor_id, kind,
         Jsonb(_jsonable(detail or {}))),
    ).fetchone()
    return row["id"]


def list_for(conn, workspace_id: int, *, entity: str | None = None, entity_id: int | None = None,
             order_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """输入：工作区与过滤条件 → 输出：事件列表（新的在前）。"""
    where = ["workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if order_id is not None:
        where.append("order_id = %s")
        params.append(order_id)
    if entity is not None:
        where.append("entity = %s")
        params.append(entity)
    if entity_id is not None:
        where.append("entity_id = %s")
        params.append(entity_id)
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM procure.events WHERE {' AND '.join(where)} ORDER BY at DESC, id DESC LIMIT %s",
        params,
    ).fetchall()


def _jsonable(v: Any) -> Any:
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v
