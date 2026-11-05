"""飞书写回：python cli.py feishu_writeback。后台调度每 60 秒调用一次。

一个事务里发送全部工作区的到期写回（services/writeback.flush）：只发已验收连接的到期 pending 行，失败按退避重排。
没有到期行时不访问飞书；飞书未配置时到期行原样保留。
"""

from __future__ import annotations

from typing import Any

from registry import db
from services import writeback


def run() -> dict[str, Any]:
    """输入：无 → 输出：{due, sent, done, retrying, failed, superseded, waiting_verification[, error]}。"""
    with db.pg_conn() as c:
        return writeback.flush(c)
