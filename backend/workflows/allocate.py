"""账号分配：python cli.py allocate [-p workspace_id=1]。后台调度每天北京时间 00:01 以 trigger='daily' 调用。"""

from __future__ import annotations

import logging
from typing import Any

from registry import db
from services import allocation

log = logging.getLogger(__name__)


def run(workspace_id: int | str | None = None, trigger: str = "daily", *, conn=None) -> dict[str, Any]:
    """输入：工作区 id（缺省 = 全部工作区）、触发方式（可选外部连接）→ 输出：{workspaces: {id: 分配摘要 | {error}}}。

    每个工作区一个独立事务（一个失败不影响其他）；传入 conn 时全部在该连接上执行且不提交。
    """
    if conn is not None:
        ids = _workspace_ids(conn, workspace_id)
        return {"workspaces": {str(w): allocation.allocate(conn, w, trigger=trigger) for w in ids}}
    with db.pg_conn() as c:
        ids = _workspace_ids(c, workspace_id)
    out: dict[str, Any] = {}
    for w in ids:
        try:
            with db.pg_conn() as c:
                out[str(w)] = allocation.allocate(c, w, trigger=trigger)
        except Exception as exc:  # noqa: BLE001 - 后台任务：记录并继续下一个工作区
            log.exception("工作区 %s 分配失败", w)
            out[str(w)] = {"error": str(exc)}
    return {"workspaces": out}


def _workspace_ids(conn, workspace_id: int | str | None) -> list[int]:
    if workspace_id not in (None, ""):
        return [int(workspace_id)]
    return [r["id"] for r in conn.execute("SELECT id FROM procure.workspaces ORDER BY id").fetchall()]
