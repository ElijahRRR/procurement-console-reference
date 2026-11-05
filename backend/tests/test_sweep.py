"""租约清扫（契约 §5）：过期批次结束；未登记提交的在途尝试中止、任务回已分配；已登记提交的进结果待核实、
账号暂停、不可再领取（v1 §10「插件提交后断网」「原电脑提交后失联，另一台电脑尝试接管」）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from registry import db
from services import accounts, clock, devices, execution, quota
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx
from workflows import sweep as sweep_wf

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)
LATER = NOW + timedelta(seconds=181)


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


@pytest.fixture()
def env(conn, workspace):
    acc = fx.exec_account(conn, workspace["id"])
    dev = fx.paired_device(conn, workspace, account=acc, name="Mac-DEMO")
    return {"ws": workspace["id"], "acc": acc, "dev": dev, "uid": workspace["user_id"], "workspace": workspace}


def test_sweep_ignores_live_runs(conn, env):
    run_id = fx.start(conn, env["dev"])
    clock.freeze(NOW + timedelta(seconds=179))
    out = execution.sweep(conn)
    assert out == {"expired_runs": [], "revoked_runs": [], "orphan_attempts": [], "aborted": 0, "unknown": 0}
    assert fx.run_row(conn, run_id)["ended_at"] is None


def test_expired_run_without_attempt_is_ended(conn, env):
    run_id = fx.start(conn, env["dev"])
    clock.freeze(LATER)
    out = execution.sweep(conn)
    assert out["expired_runs"] == [run_id]
    run = fx.run_row(conn, run_id)
    assert run["end_reason"] == "lease_expired" and run["ended_at"] == LATER
    assert f.count(conn, "events", "entity = 'run' AND kind = 'run.ended'") == 1
    assert execution.sweep(conn)["expired_runs"] == []          # 只处理一次


def test_unsubmitted_attempt_aborted_and_order_back_to_assigned(conn, env):
    order = fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    a = fx.claim(conn, env["dev"], run_id)
    fx.to_guard_passed(conn, env["dev"], a)
    clock.freeze(LATER)
    out = execution.sweep(conn)
    assert out["aborted"] == 1 and out["unknown"] == 0
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "aborted" and "租约过期" in att["error_detail"]
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "assigned" and o["assigned_account_id"] == env["acc"]["id"]
    assert o["assignment_kind"] == "auto" and o["current_attempt_id"] is None
    assert quota.used(conn, env["acc"]["id"], TODAY, True) == 0
    assert fx.account_row(conn, env["acc"]["id"])["execution_paused"] is False
    # 插件再上线后可以重新领取
    fx.refresh_identity(conn, env["dev"], env["acc"])
    b = fx.claim(conn, env["dev"], fx.start(conn, env["dev"]))
    assert b["order_id"] == order["id"] and b["attempt_no"] == 2


def test_submitted_then_lease_expiry_is_unknown_paused_and_not_reclaimable(conn, env):
    """提交后断线：执行权超时不直接触发再次下单；另一台电脑也接管不了。"""
    ws, acc = env["ws"], env["acc"]
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, env["dev"])
    a = fx.claim(conn, env["dev"], run_id)
    fx.to_submitted(conn, env["dev"], a)
    clock.freeze(LATER)
    out = execution.sweep(conn)
    assert out["expired_runs"] == [run_id] and out["unknown"] == 1
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "unknown" and att["submitted_at"] == NOW
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "result_unknown" and "需人工核实" in o["status_reason"]
    assert o["current_attempt_id"] == a["attempt_id"]
    row = fx.account_row(conn, acc["id"])
    assert row["execution_paused"] is True and row["pause_reason"] == f"结果待核实：{order['upstream_order_no']}"
    assert quota.used(conn, acc["id"], TODAY, True) == 1
    # 原插件迟到的请求：执行权已失效
    with pytest.raises(DomainError) as e:
        execution.progress(conn, env["dev"], a["attempt_id"], stage="late")
    assert e.value.code == "LEASE_LOST"
    # 另一台电脑：账号暂停 → 开不了批次；人工恢复账号后也领不到这一笔
    pc = fx.paired_device(conn, env["workspace"], account=acc, name="PC-DEMO")
    with pytest.raises(DomainError) as e:
        execution.start_run(conn, pc, trigger="manual", simulated=True)
    assert e.value.code == "ACCOUNT_PAUSED"
    accounts.resume(conn, ws, acc["id"], actor_kind="user", actor_id=env["uid"])
    out = execution.next_attempt(conn, pc, fx.start(conn, pc))
    assert out["attempt"] is None
    assert f.count(conn, "purchase_attempts", "purchase_order_id = %s", (order["id"],)) == 1


def test_late_result_after_sweep_is_recorded_not_applied(conn, env):
    """清扫后才送到的成功回执（v1 §4「插件与服务端均保存必要的结果回执」）：不改状态、不恢复账号、不自动核实，
    但回执（单号、金额）记在尝试与事件里，供人工核实时参考；同样的回执再送一次不重复记录。"""
    order = fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    a = fx.claim(conn, env["dev"], run_id)
    fx.to_submitted(conn, env["dev"], a)
    clock.freeze(LATER)
    execution.sweep(conn)
    no = fx.sim_order_no(a)
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.success_payload(a))
    assert res["late_receipt"] is True and res["attempt_status"] == "unknown"
    assert res["order_status"] == "result_unknown" and "迟到" in res["message"]
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "unknown" and att["amazon_order_no"] == no and att["total"] == Decimal("26.50")
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "result_unknown" and o["current_attempt_id"] == a["attempt_id"]
    assert no in o["status_reason"] and o["needs_attention"] is True and no in o["attention_note"]
    assert fx.account_row(conn, env["acc"]["id"])["execution_paused"] is True
    ev = conn.execute("SELECT detail FROM procure.events WHERE kind = 'attempt.late_result'").fetchall()
    assert len(ev) == 1 and ev[0]["detail"]["amazon_order_no"] == no and ev[0]["detail"]["outcome"] == "succeeded"
    assert ev[0]["detail"]["amounts"]["total"] == "26.50"
    # 插件补交同一份回执：照样回 late_receipt，不重复记录
    again = execution.report_result(conn, env["dev"], a["attempt_id"], fx.success_payload(a))
    assert again["late_receipt"] is True
    assert f.count(conn, "events", "kind = 'attempt.late_result'") == 1
    # 同样是「结果不明」的迟到上报：与已登记的一致，原样返回
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.failure_payload(None, outcome="unknown"))
    assert res["duplicate"] is True
    # 人工据回执核实已下单
    from services import orders
    detail = orders.resolve_unknown(conn, env["ws"], order["id"], "ordered", amazon_order_no=no,
                                    actor_user_id=env["uid"])
    assert detail["status"] == "purchased"


def test_late_success_after_lease_expiry_before_sweep_is_accepted(conn, env):
    """已登记提交、租约过期但还没被清扫：成功回执照常接收（不是 LEASE_LOST），任务已采购、账号不暂停。"""
    order = fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    a = fx.claim(conn, env["dev"], run_id)
    fx.to_submitted(conn, env["dev"], a)
    clock.freeze(LATER)
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.success_payload(a))
    assert res == {"attempt_status": "succeeded", "order_status": "purchased"}
    assert fx.order_row(conn, order["id"])["purchased_attempt_id"] == a["attempt_id"]
    out = execution.sweep(conn)
    assert out["expired_runs"] == [run_id] and out["unknown"] == 0 and out["aborted"] == 0
    assert fx.account_row(conn, env["acc"]["id"])["execution_paused"] is False


def test_late_failure_after_lease_expiry_before_submit_is_lease_lost(conn, env):
    """未登记提交的尝试：租约过期后的结果照旧拒绝（LEASE_LOST），由清扫中止、任务回已分配。"""
    order = fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    a = fx.claim(conn, env["dev"], run_id)
    clock.freeze(LATER)
    with pytest.raises(DomainError) as e:
        execution.report_result(conn, env["dev"], a["attempt_id"], fx.failure_payload("OUT_OF_STOCK"))
    assert e.value.code == "LEASE_LOST"
    execution.sweep(conn)
    assert fx.order_row(conn, order["id"])["status"] == "assigned"


def test_late_result_for_aborted_attempt_is_still_conflict(conn, env):
    fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    a = fx.claim(conn, env["dev"], run_id)
    clock.freeze(LATER)
    execution.sweep(conn)
    assert fx.attempt_row(conn, a["attempt_id"])["status"] == "aborted"
    with pytest.raises(DomainError) as e:
        execution.report_result(conn, env["dev"], a["attempt_id"], fx.failure_payload("OUT_OF_STOCK"))
    assert e.value.code == "CONFLICT"
    assert f.count(conn, "events", "kind = 'attempt.late_result'") == 0


def test_heartbeat_keeps_lease_alive(conn, env):
    fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    a = fx.claim(conn, env["dev"], run_id)
    for k in range(1, 6):
        clock.freeze(NOW + timedelta(seconds=150 * k))
        execution.run_heartbeat(conn, env["dev"], run_id)
        assert execution.sweep(conn)["expired_runs"] == []
    assert fx.attempt_row(conn, a["attempt_id"])["status"] == "claimed"


def test_sweep_ends_runs_of_revoked_devices(conn, env):
    order = fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    fx.claim(conn, env["dev"], run_id)
    # 模拟「撤销时没走 devices.revoke」的情况：直接置撤销
    conn.execute("UPDATE procure.devices SET revoked_at = %s, token_hash = NULL WHERE id = %s",
                 (NOW, env["dev"]["id"]))
    out = execution.sweep(conn)
    assert out["revoked_runs"] == [run_id] and out["aborted"] == 1
    assert fx.run_row(conn, run_id)["end_reason"] == "device_revoked"
    assert fx.order_row(conn, order["id"])["status"] == "assigned"


def test_revoke_submitted_attempt_goes_result_unknown(conn, env):
    order = fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    a = fx.claim(conn, env["dev"], run_id)
    fx.to_submitted(conn, env["dev"], a)
    devices.revoke(conn, env["ws"], env["dev"]["id"], env["uid"])
    assert fx.attempt_row(conn, a["attempt_id"])["status"] == "unknown"
    assert fx.order_row(conn, order["id"])["status"] == "result_unknown"
    assert fx.account_row(conn, env["acc"]["id"])["execution_paused"] is True


def test_orphan_inflight_attempt_of_ended_run_is_settled(conn, env):
    order = fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    a = fx.claim(conn, env["dev"], run_id)
    conn.execute("UPDATE procure.execution_runs SET ended_at = %s, end_reason = 'finished' WHERE id = %s",
                 (NOW, run_id))
    out = execution.sweep(conn)
    assert out["orphan_attempts"] == [a["attempt_id"]] and out["aborted"] == 1
    assert fx.order_row(conn, order["id"])["status"] == "assigned"


def test_upstream_cancelled_unsubmitted_attempt_expires_to_cancelled(conn, env):
    order = fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    fx.claim(conn, env["dev"], run_id)
    conn.execute("UPDATE procure.purchase_orders SET upstream_cancelled = true, upstream_cancel_note = 'DEMO' "
                 "WHERE id = %s", (order["id"],))
    clock.freeze(LATER)
    execution.sweep(conn)
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "cancelled" and o["assigned_account_id"] is None


def test_workflow_run_uses_own_transaction(clean_db, conn, env):
    order = fx.assigned_order(conn, env["ws"], env["acc"])
    run_id = fx.start(conn, env["dev"])
    fx.claim(conn, env["dev"], run_id)
    conn.commit()
    clock.freeze(LATER)
    out = sweep_wf.run()
    assert out["expired_runs"] == [run_id] and out["aborted"] == 1
    with db.pg_conn(clean_db) as c:
        assert c.execute("SELECT status FROM procure.purchase_orders WHERE id = %s",
                         (order["id"],)).fetchone()["status"] == "assigned"
    assert sweep_wf.run(conn=conn)["expired_runs"] == []


def test_workflow_passes_now_to_sweep(conn, env):
    """workflows.sweep.run(now=…) 按给定时刻判定租约是否过期（调度器把自己的「现在」传进来）。"""
    run_id = fx.start(conn, env["dev"])
    assert sweep_wf.run(conn=conn)["expired_runs"] == []                    # 时钟仍在 NOW：租约有效
    assert sweep_wf.run(conn=conn, now=NOW + timedelta(seconds=179))["expired_runs"] == []
    assert sweep_wf.run(conn=conn, now=LATER)["expired_runs"] == [run_id]
    assert fx.run_row(conn, run_id)["end_reason"] == "lease_expired"
