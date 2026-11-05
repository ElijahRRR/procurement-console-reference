"""买家账号的每日额度（契约 §3.1）。

定义（全部按北京日期）：

- ``used(A, D, simulated)``：账号 A 在 D 日创建、模拟标记与 ``simulated`` 相同、状态不在 (failed, aborted) 的
  采购尝试数。在途、待核实、成功都占用；人工核实「未下单」后尝试改为 failed，额度随之释放。
  一次成功计 1 单，与商品数量无关；``bj_date`` 是尝试创建当天，跨零点不变。外部单号形成的
  ``kind='external'`` 尝试不是本系统下单，不计额度。
- ``planned(A, D)``：已分配给 A、状态为 assigned、没有在途尝试、计划日期等于 D 的任务数。
  计划日期 ``plan = max(建议采购日期, 今天)``：过期未采的任务算在今天，未来任务算在它自己的日期。
- ``fixed(A, D)``：planned 中 ``assignment_kind='manual'`` 的部分（分配算法里不可重算、但占用容量）。
- ``summary(A, today)``：``{limit, used, planned, available}``，
  ``available = max(0, limit − used(A, today) − planned(A, today))``，即今天还能再分给 A 的单数。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from services import clock
from services.models import ATTEMPT_INFLIGHT, ATTEMPT_QUOTA_FREE

_QUOTA_FREE = tuple(sorted(ATTEMPT_QUOTA_FREE))
_INFLIGHT = tuple(sorted(ATTEMPT_INFLIGHT))


def used(conn, account_id: int, d: date, simulated: bool, *, workspace_id: int | None = None) -> int:
    """输入：账号、北京日期、模拟标记（可选工作区）→ 输出：当天已占用的额度（单数）。"""
    sql = (
        "SELECT count(*) AS n FROM procure.purchase_attempts "
        "WHERE account_id = %s AND bj_date = %s AND simulated = %s "
        "AND kind <> 'external' AND status <> ALL(%s)"
    )
    params: list[Any] = [account_id, d, simulated, list(_QUOTA_FREE)]
    if workspace_id is not None:
        sql += " AND workspace_id = %s"
        params.append(workspace_id)
    return int(conn.execute(sql, params).fetchone()["n"])


def used_map(conn, workspace_id: int, dates: Iterable[date], simulated: bool) -> dict[tuple[int, date], int]:
    """输入：工作区、一组北京日期、模拟标记 → 输出：{(账号 id, 日期): 已用额度}（没有尝试的组合不出现）。"""
    ds = sorted(set(dates))
    if not ds:
        return {}
    rows = conn.execute(
        """
        SELECT account_id, bj_date, count(*) AS n FROM procure.purchase_attempts
        WHERE workspace_id = %s AND account_id IS NOT NULL AND bj_date = ANY(%s) AND simulated = %s
          AND kind <> 'external' AND status <> ALL(%s)
        GROUP BY account_id, bj_date
        """,
        (workspace_id, ds, simulated, list(_QUOTA_FREE)),
    ).fetchall()
    return {(r["account_id"], r["bj_date"]): int(r["n"]) for r in rows}


def _planned_sql(manual_only: bool) -> str:
    return (
        "SELECT count(*) AS n FROM procure.purchase_orders o "
        "WHERE o.assigned_account_id = %s AND o.status = 'assigned' "
        + ("AND o.assignment_kind = 'manual' " if manual_only else "")
        + "AND GREATEST(COALESCE(o.suggested_purchase_date, %s::date), %s::date) = %s "
        "AND NOT EXISTS (SELECT 1 FROM procure.purchase_attempts pa "
        "                WHERE pa.purchase_order_id = o.id AND pa.status = ANY(%s))"
    )


def planned(conn, account_id: int, d: date, *, today: date | None = None,
            workspace_id: int | None = None) -> int:
    """输入：账号、北京日期（可选「今天」与工作区）→ 输出：计划在 D 日执行、尚未开始的已分配任务数。"""
    t = today or clock.bj_today()
    if d < t:
        return 0
    sql = _planned_sql(False)
    params: list[Any] = [account_id, t, t, d, list(_INFLIGHT)]
    if workspace_id is not None:
        sql += " AND o.workspace_id = %s"
        params.append(workspace_id)
    return int(conn.execute(sql, params).fetchone()["n"])


def fixed(conn, account_id: int, d: date, *, today: date | None = None,
          workspace_id: int | None = None) -> int:
    """输入：账号、北京日期 → 输出：D 日人工固定分配、尚未开始的任务数（分配算法的 fixed(A, D)）。"""
    t = today or clock.bj_today()
    if d < t:
        return 0
    sql = _planned_sql(True)
    params: list[Any] = [account_id, t, t, d, list(_INFLIGHT)]
    if workspace_id is not None:
        sql += " AND o.workspace_id = %s"
        params.append(workspace_id)
    return int(conn.execute(sql, params).fetchone()["n"])


def fixed_map(conn, workspace_id: int, today: date) -> dict[tuple[int, date], int]:
    """输入：工作区、今天 → 输出：{(账号 id, 计划日期): 人工固定分配且尚未开始的任务数}。"""
    rows = conn.execute(
        """
        SELECT o.assigned_account_id AS account_id,
               GREATEST(COALESCE(o.suggested_purchase_date, %s::date), %s::date) AS plan_date,
               count(*) AS n
        FROM procure.purchase_orders o
        WHERE o.workspace_id = %s AND o.status = 'assigned' AND o.assignment_kind = 'manual'
          AND NOT EXISTS (SELECT 1 FROM procure.purchase_attempts pa
                          WHERE pa.purchase_order_id = o.id AND pa.status = ANY(%s))
        GROUP BY 1, 2
        """,
        (today, today, workspace_id, list(_INFLIGHT)),
    ).fetchall()
    return {(r["account_id"], r["plan_date"]): int(r["n"]) for r in rows}


def summary(conn, account_row: dict[str, Any], today: date | None, simulated: bool) -> dict[str, int]:
    """输入：账号行、今天（None = 北京今天）、模拟标记 → 输出：{limit, used, planned, available}。

    available = max(0, limit − used − planned)：今天还能再分给该账号的单数（见模块说明）。
    """
    t = today or clock.bj_today()
    limit = int(account_row["daily_limit"])
    u = used(conn, account_row["id"], t, simulated, workspace_id=account_row["workspace_id"])
    p = planned(conn, account_row["id"], t, today=t, workspace_id=account_row["workspace_id"])
    return {"limit": limit, "used": u, "planned": p, "available": max(0, limit - u - p)}
