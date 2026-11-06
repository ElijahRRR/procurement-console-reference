"""飞书读取：python cli.py feishu_intake [-p connection_id=1]。后台调度（scheduler）按各连接的 poll_minutes 调用同一个 run()。

不带 connection_id：读取全部工作区里「到期」的启用连接（trigger='schedule'）；带 connection_id：只读这一个（trigger='manual'）。
先查要读的连接，没有就不建飞书客户端（后台每 30 秒查一次，不必每次都建客户端）。每个连接一个独立事务，一个失败不影响
其他。读飞书失败 / 接收出错时 services/feishu_intake 已把失败记录写进本连接的事务，这里捕获 DomainError 后正常退出
with 块（提交），失败历史得以保留。飞书未配置时什么也不读（结果带 skipped）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from registry import db
from services import clock, feishu_connections, feishu_intake
from services.errors import DomainError

log = logging.getLogger(__name__)


def _summary(res: dict[str, Any]) -> dict[str, Any]:
    return {k: res[k] for k in (*feishu_intake.STAT_KEYS, "run_id")}


def run(connection_id: int | str | None = None, trigger: str | None = None, *,
        now: datetime | None = None) -> dict[str, Any]:
    """输入：连接 id（缺省 = 全部到期连接）、触发方式（缺省：指定连接为 manual，否则 schedule）、当前时刻（缺省 clock.now()）→
    输出：{due: 要读的连接数, connections: {连接 id: {read, new, update, unchanged, conflict, historical, skipped, invalid,
    run_id} | {error, code}}}；飞书未配置时另有 skipped（原因），不读任何连接。"""
    out: dict[str, Any] = {"due": 0, "connections": {}}
    with db.pg_conn() as c:
        if connection_id not in (None, ""):
            row = c.execute("SELECT workspace_id, id FROM procure.feishu_connections WHERE id = %s",
                            (int(connection_id),)).fetchone()
            targets = [(row["workspace_id"], row["id"])] if row else []
            if row is None:
                out["connections"][str(connection_id)] = {"error": "飞书连接不存在", "code": "NOT_FOUND"}
        else:
            targets = feishu_intake.due_connections(c, now or clock.now())
    out["due"] = len(targets)
    if not targets:
        return out
    try:
        client = feishu_connections.make_client()
    except DomainError as exc:
        out["skipped"] = exc.message
        return out
    how = trigger or ("manual" if connection_id not in (None, "") else "schedule")
    try:
        for ws, cid in targets:
            try:
                with db.pg_conn() as c:
                    try:
                        out["connections"][str(cid)] = _summary(
                            feishu_intake.run_connection(c, ws, cid, trigger=how, client=client))
                    except DomainError as exc:
                        log.warning("飞书连接 %s 读取失败：%s", cid, exc.message)
                        out["connections"][str(cid)] = {"error": exc.message, "code": exc.code}
            except Exception as exc:  # noqa: BLE001 - 后台任务：记录并继续下一个连接
                log.exception("飞书连接 %s 读取异常", cid)
                out["connections"][str(cid)] = {"error": str(exc), "code": "INTERNAL"}
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()
    return out
