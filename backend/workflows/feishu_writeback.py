"""飞书写回：python cli.py feishu_writeback。后台调度（scheduler）每 60 秒调用同一个 run()。

一个事务里发送全部工作区的到期写回（services/writeback.flush）：只发已验收连接的到期 pending 行，失败按退避重排。
没有到期行时不访问飞书；飞书未配置时到期行原样保留。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from registry import db
from services import writeback

log = logging.getLogger(__name__)


def run(*, now: datetime | None = None) -> dict[str, Any]:
    """输入：当前时刻（缺省 clock.now()）→ 输出：{due, sent, done, retrying, failed, superseded, waiting_verification[, error]}。"""
    with db.pg_conn() as c:
        stats = writeback.flush(c, now=now)
    if stats.get("sent") or stats.get("failed"):
        log.info("飞书写回：%s", stats)
    return stats
