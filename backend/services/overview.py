"""控制台概览（契约 §10「概览」）。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from registry import settings
from services import allocation, clock, devices, orders, settings_svc
from services.models import ORDER_STATUSES

ONLINE_WINDOW = timedelta(seconds=devices.ONLINE_WINDOW_S)
RECENT_EXCEPTIONS = 5


def overview(conn, workspace_id: int) -> dict[str, Any]:
    """输入：工作区 → 输出：{bj_date, counts, needs_attention, today_executable, future_assigned, accounts, devices,
    recent_exceptions, last_allocation, simulation_only, feishu_configured}。

    needs_attention = 置了「需要留意」提醒的任务数（不论状态）；today_executable = 已分配且建议采购日期 ≤ 北京今天（「today」视图）；future_assigned = 已分配且日期在今天之后；
    accounts.enabled = 未归档且采购开关开启；accounts.paused = 未归档且暂停执行；devices 只计未撤销的，
    online = 3 分钟内有心跳；recent_exceptions = 最近变为「异常」的任务（列表行形状）。
    """
    today = clock.bj_today()
    counts = {s: 0 for s in ORDER_STATUSES}
    for r in conn.execute(
        "SELECT status, count(*) AS n FROM procure.purchase_orders WHERE workspace_id = %s GROUP BY status",
        (workspace_id,),
    ).fetchall():
        counts[r["status"]] = int(r["n"])
    attention = conn.execute(
        "SELECT count(*) AS n FROM procure.purchase_orders WHERE workspace_id = %s AND needs_attention",
        (workspace_id,),
    ).fetchone()["n"]
    dated = conn.execute(
        """
        SELECT count(*) FILTER (WHERE suggested_purchase_date <= %s) AS today_n,
               count(*) FILTER (WHERE suggested_purchase_date > %s) AS future_n
        FROM procure.purchase_orders WHERE workspace_id = %s AND status = 'assigned'
        """,
        (today, today, workspace_id),
    ).fetchone()
    acc = conn.execute(
        """
        SELECT count(*) AS total, count(*) FILTER (WHERE purchase_enabled) AS enabled,
               count(*) FILTER (WHERE execution_paused) AS paused
        FROM procure.buyer_accounts WHERE workspace_id = %s AND archived_at IS NULL
        """,
        (workspace_id,),
    ).fetchone()
    dev = conn.execute(
        """
        SELECT count(*) AS total, count(*) FILTER (WHERE last_seen_at >= %s) AS online
        FROM procure.devices WHERE workspace_id = %s AND revoked_at IS NULL
        """,
        (clock.now() - ONLINE_WINDOW, workspace_id),
    ).fetchone()
    recent = conn.execute(
        f"{orders._ROW_SELECT} WHERE o.workspace_id = %s AND o.status = 'exception' "
        "ORDER BY o.status_changed_at DESC, o.id DESC LIMIT %s",
        (workspace_id, RECENT_EXCEPTIONS),
    ).fetchall()
    return {
        "bj_date": today.isoformat(),
        "counts": counts,
        "needs_attention": int(attention),
        "today_executable": int(dated["today_n"]),
        "future_assigned": int(dated["future_n"]),
        "accounts": {"total": int(acc["total"]), "enabled": int(acc["enabled"]), "paused": int(acc["paused"])},
        "devices": {"total": int(dev["total"]), "online": int(dev["online"])},
        "recent_exceptions": [orders.order_row_out(r) for r in recent],
        "last_allocation": allocation.last_run(conn, workspace_id),
        "simulation_only": settings_svc.simulation_only(conn, workspace_id),
        "feishu_configured": settings.get().feishu_configured,
    }
