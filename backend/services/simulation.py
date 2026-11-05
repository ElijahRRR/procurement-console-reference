"""清除模拟结果（契约 §5「模拟」、方案 v2 §2）：控制台一键把模拟执行留下的痕迹清掉，任务回到开始前的状态。

reset 依次做：

1. 结束本工作区未结束的模拟批次（end_reason='simulation_reset'；在途尝试不按超时处理，下一步统一清除）。
2. 全部模拟尝试（尚未清除过的）→ aborted，resolution='simulation_reset'，说明「已清除模拟结果」。
   它们不再占额度（额度只数非 failed / aborted 的尝试）。
3. 受影响的任务经 orders.revert_simulation 按派生规则回到开始前的状态（分配仍有效就保留），
   页面错误自动重试计数清零。任务有真实尝试时跳过并列出原因。
4. **只因**模拟尝试「结果待核实」而暂停的账号恢复执行（accounts.resume，会按今天重新分配）。
   判断依据：账号最近一次恢复之后的每一次 'account.paused'，都有一条执行模块写的
   PAUSE_MARKER_KIND 标记，且标记都是 {simulated: true, cause: 'result_unknown'}。人工暂停、真实结果、
   账号级错误（登录失效、验证码等）造成的暂停都不会被自动恢复。
5. 写 'simulation.reset' 事件（对象为工作区设置）。
"""

from __future__ import annotations

from typing import Any

from services import accounts, clock, events, execution, orders
from services.errors import DomainError


def _actor(actor_user_id: int | None) -> tuple[str, int | None]:
    return ("user", actor_user_id) if actor_user_id is not None else ("system", None)


def paused_only_by_simulation(conn, workspace_id: int, account_id: int) -> bool:
    """输入：工作区、账号 → 输出：该账号当前的暂停是否只由模拟尝试的「结果待核实」造成。"""
    last_resume = conn.execute(
        "SELECT COALESCE(max(id), 0) AS id FROM procure.events WHERE workspace_id = %s AND entity = 'account' "
        "AND entity_id = %s AND kind = 'account.resumed'",
        (workspace_id, account_id),
    ).fetchone()["id"]
    rows = conn.execute(
        "SELECT kind, detail FROM procure.events WHERE workspace_id = %s AND entity = 'account' AND entity_id = %s "
        "AND kind = ANY(%s) AND id > %s ORDER BY id",
        (workspace_id, account_id, ["account.paused", execution.PAUSE_MARKER_KIND], last_resume),
    ).fetchall()
    pauses = sum(1 for r in rows if r["kind"] == "account.paused")
    markers = [r["detail"] or {} for r in rows if r["kind"] == execution.PAUSE_MARKER_KIND]
    return (pauses > 0 and len(markers) == pauses
            and all(m.get("simulated") is True and m.get("cause") == "result_unknown" for m in markers))


def reset(conn, workspace_id: int, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、操作人 → 输出：{runs_ended, attempts_reset, orders_restored, orders_skipped, accounts_resumed}。

    见模块说明。只动本工作区的模拟数据；真实尝试、外部单号尝试不受影响。
    """
    ws = workspace_id
    ak, aid = _actor(actor_user_id)
    now = clock.now()

    runs = conn.execute(
        "SELECT * FROM procure.execution_runs WHERE workspace_id = %s AND simulated AND ended_at IS NULL "
        "ORDER BY id FOR UPDATE",
        (ws,),
    ).fetchall()
    for run in runs:
        execution.end_run(conn, run, "simulation_reset", actor_kind=ak, actor_id=aid, settle=False)

    order_ids = [r["purchase_order_id"] for r in conn.execute(
        "SELECT DISTINCT purchase_order_id FROM procure.purchase_attempts WHERE workspace_id = %s AND simulated "
        "AND resolution IS DISTINCT FROM 'simulation_reset' ORDER BY purchase_order_id",
        (ws,),
    ).fetchall()]
    for oid in order_ids:   # 先锁任务、再改尝试（与执行模块的加锁顺序一致）
        orders.get_row(conn, ws, oid, for_update=True)
    reset_rows = conn.execute(
        """
        UPDATE procure.purchase_attempts
        SET status = 'aborted', resolution = 'simulation_reset', resolution_note = '已清除模拟结果',
            resolved_by = %s, resolved_at = %s, finished_at = COALESCE(finished_at, %s)
        WHERE workspace_id = %s AND simulated AND resolution IS DISTINCT FROM 'simulation_reset'
        RETURNING id, purchase_order_id
        """,
        (actor_user_id, now, now, ws),
    ).fetchall()

    restored: list[int] = []
    skipped: list[dict[str, Any]] = []
    for oid in order_ids:
        try:
            with conn.transaction():
                row = orders.revert_simulation(conn, ws, oid, actor_kind=ak, actor_id=aid)
                if row["page_load_retries"] or row["current_attempt_id"] is not None:
                    row = orders.transition(conn, row, row["status"], actor_kind=ak, actor_id=aid,
                                            updates={"page_load_retries": 0, "current_attempt_id": None})
        except DomainError as exc:
            skipped.append({"order_id": oid, "reason": exc.message})
            continue
        restored.append(oid)

    resumed: list[int] = []
    for acc in conn.execute(
        "SELECT id FROM procure.buyer_accounts WHERE workspace_id = %s AND execution_paused AND archived_at IS NULL "
        "ORDER BY id",
        (ws,),
    ).fetchall():
        if paused_only_by_simulation(conn, ws, acc["id"]):
            accounts.resume(conn, ws, acc["id"], actor_kind=ak, actor_id=aid)
            resumed.append(acc["id"])

    summary = {
        "runs_ended": [r["id"] for r in runs],
        "attempts_reset": len(reset_rows),
        "orders_restored": restored,
        "orders_skipped": skipped,
        "accounts_resumed": resumed,
    }
    events.record(conn, ws, "settings", ws, "simulation.reset", actor_kind=ak, actor_id=aid, detail=summary)
    return summary
