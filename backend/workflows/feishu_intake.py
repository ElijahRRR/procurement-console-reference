"""飞书读取：python cli.py feishu_intake [-p connection_id=1]。后台调度按各连接的 poll_minutes 调用。

不带 connection_id：读取全部工作区里「到期」的启用连接（trigger='schedule'）；带 connection_id：只读这一个（trigger='manual'）。
每个连接一个独立事务，一个失败不影响其他。读飞书失败时 services/feishu_intake 已把失败记录写进本连接的事务，
这里捕获 DomainError 后正常退出 with 块（提交），失败历史得以保留。飞书未配置时什么也不做。
"""

from __future__ import annotations

import logging
from typing import Any

from registry import db
from services import clock, feishu_connections, feishu_intake
from services.errors import DomainError

log = logging.getLogger(__name__)


def run(connection_id: int | str | None = None, trigger: str | None = None) -> dict[str, Any]:
    """输入：连接 id（缺省 = 全部到期连接）、触发方式（缺省：指定连接为 manual，否则 schedule）→
    输出：{connections: {连接 id: 读取统计 | {error, code}}}，飞书未配置时为 {skipped: 原因, connections: {}}。"""
    try:
        client = feishu_connections.make_client()
    except DomainError as exc:
        return {"skipped": exc.message, "connections": {}}
    out: dict[str, Any] = {}
    try:
        with db.pg_conn() as c:
            if connection_id not in (None, ""):
                row = c.execute("SELECT workspace_id, id FROM procure.feishu_connections WHERE id = %s",
                                (int(connection_id),)).fetchone()
                targets = [(row["workspace_id"], row["id"])] if row else []
                if row is None:
                    out[str(connection_id)] = {"error": "飞书连接不存在", "code": "NOT_FOUND"}
            else:
                targets = feishu_intake.due_connections(c, clock.now())
        how = trigger or ("manual" if connection_id not in (None, "") else "schedule")
        for ws, cid in targets:
            try:
                with db.pg_conn() as c:
                    try:
                        out[str(cid)] = feishu_intake.run_connection(c, ws, cid, trigger=how, client=client)
                    except DomainError as exc:
                        log.warning("飞书连接 %s 读取失败：%s", cid, exc.message)
                        out[str(cid)] = {"error": exc.message, "code": exc.code}
            except Exception as exc:  # 后台任务：记录并继续下一个连接
                log.exception("飞书连接 %s 读取异常", cid)
                out[str(cid)] = {"error": str(exc), "code": "INTERNAL"}
    finally:
        client.close()
    return {"connections": out}
