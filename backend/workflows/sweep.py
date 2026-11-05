"""租约清扫：python cli.py sweep。后台调度每 30 秒（registry sweep_interval_s）调用一次。

过期批次 → 结束（lease_expired），在途尝试未登记提交 → 中止、任务回已分配；已登记提交 → 结果待核实、
账号暂停执行；设备已撤销的批次同样结束。业务判断见 services/execution.sweep。
"""

from __future__ import annotations

import logging
from typing import Any

from registry import db
from services import execution

log = logging.getLogger(__name__)


def run(conn=None) -> dict[str, Any]:
    """输入：可选外部连接 → 输出：清扫摘要 {expired_runs, revoked_runs, orphan_attempts, aborted, unknown}。

    不传 conn 时自己开一个事务（正常结束提交）；传入 conn 时在它上面执行且不提交。
    """
    if conn is not None:
        return execution.sweep(conn)
    with db.pg_conn() as c:
        summary = execution.sweep(c)
    if summary["expired_runs"] or summary["revoked_runs"] or summary["orphan_attempts"]:
        log.info("租约清扫：%s", summary)
    return summary
