"""账号分配：python cli.py allocate [-p workspace_id=1]（等同控制台「重新分配」，trigger='manual'）。

后台调度每天北京时间 00:01 以 trigger='daily' 调用：每个工作区每个北京日期只做一次（allocation.allocate_daily
查 allocation_runs，进程重启后不会重做；当天还没做过的会补做一次）。
"""

from __future__ import annotations

import logging
from typing import Any

from registry import db
from services import allocation

log = logging.getLogger(__name__)

DAILY_SKIPPED = "今天已做过每日分配"


def _one(conn, workspace_id: int, trigger: str) -> dict[str, Any]:
    if trigger == "daily":
        out = allocation.allocate_daily(conn, workspace_id)
        return out if out is not None else {"skipped": DAILY_SKIPPED}
    return allocation.allocate(conn, workspace_id, trigger=trigger)


def run(workspace_id: int | str | None = None, trigger: str = "manual", *, conn=None) -> dict[str, Any]:
    """输入：工作区 id（缺省 = 全部工作区）、触发方式（缺省 manual；daily = 每日分配，当天已做过的工作区跳过）、
    可选外部连接 → 输出：{workspaces: {id: 分配摘要 | {skipped} | {error}}}。

    每个工作区一个独立事务（一个失败不影响其他）；传入 conn 时全部在该连接上执行且不提交。
    """
    if conn is not None:
        ids = _workspace_ids(conn, workspace_id)
        return {"workspaces": {str(w): _one(conn, w, trigger) for w in ids}}
    with db.pg_conn() as c:
        ids = _workspace_ids(c, workspace_id)
    out: dict[str, Any] = {}
    for w in ids:
        try:
            with db.pg_conn() as c:
                out[str(w)] = _one(c, w, trigger)
        except Exception as exc:  # noqa: BLE001 - 后台任务：记录并继续下一个工作区
            log.exception("工作区 %s 分配失败", w)
            out[str(w)] = {"error": str(exc)}
    return {"workspaces": out}


def _workspace_ids(conn, workspace_id: int | str | None) -> list[int]:
    if workspace_id not in (None, ""):
        return [int(workspace_id)]
    return [r["id"] for r in conn.execute("SELECT id FROM procure.workspaces ORDER BY id").fetchall()]
