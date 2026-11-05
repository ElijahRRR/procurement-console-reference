"""清除模拟结果（契约 §5「模拟」）：模拟尝试 → aborted（simulation_reset），任务回到开始前的状态（分配仍有效
就保留），只因模拟结果（待核实、模拟出来的账号级错误）暂停的账号恢复执行，未结束的模拟批次结束；人工暂停与
真实尝试造成的暂停不动。"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from services import accounts, clock, execution, orders, quota, simulation
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _env(conn, workspace, **kw):
    acc = fx.exec_account(conn, workspace["id"], **kw)
    dev = fx.paired_device(conn, workspace, account=acc)
    return acc, dev


def test_reset_restores_orders_and_resumes_account_paused_by_simulated_unknown(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace)
    bought = fx.assigned_order(conn, ws, acc)
    blocked = fx.assigned_order(conn, ws, acc)
    retried = fx.assigned_order(conn, ws, acc)
    unknown = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    # ① 成功
    a = fx.claim(conn, dev, run_id)
    assert a["order_id"] == bought["id"]
    fx.complete_success(conn, dev, a)
    # ② 护栏拦截 → 异常
    b = fx.claim(conn, dev, run_id)
    execution.submit_guard(conn, dev, b["attempt_id"],
                           fx.good_facts(fx.order_row(conn, blocked["id"]), total="99.00"))
    # ③ 页面错误一次 → 已分配（重试计数 1）
    c = fx.claim(conn, dev, run_id)
    execution.report_result(conn, dev, c["attempt_id"], fx.failure_payload("PAGE_LOAD_FAILED"))
    # ④ 页面错误重试那一笔先放一边：领下一笔做成「结果待核实」→ 账号暂停
    conn.execute("UPDATE procure.purchase_orders SET suggested_purchase_date = %s WHERE id = %s",
                 (date(2026, 11, 5), retried["id"]))
    d = fx.claim(conn, dev, run_id)
    assert d["order_id"] == unknown["id"]
    fx.to_submitted(conn, dev, d)
    execution.report_result(conn, dev, d["attempt_id"], fx.failure_payload(None, outcome="unknown"))
    assert fx.account_row(conn, acc["id"])["execution_paused"] is True
    assert quota.used(conn, acc["id"], TODAY, True) == 2

    out = simulation.reset(conn, ws, uid)

    assert out["attempts_reset"] == 4 and out["accounts_resumed"] == [acc["id"]] and out["runs_ended"] == [run_id]
    assert sorted(out["orders_restored"]) == sorted([bought["id"], blocked["id"], retried["id"], unknown["id"]])
    assert out["orders_skipped"] == []
    rows = conn.execute("SELECT status, resolution, resolution_note, resolved_by FROM procure.purchase_attempts"
                        ).fetchall()
    assert all(r["status"] == "aborted" and r["resolution"] == "simulation_reset" for r in rows)
    assert all(r["resolution_note"] == "已清除模拟结果" and r["resolved_by"] == uid for r in rows)
    for o in (bought, blocked, retried, unknown):
        row = fx.order_row(conn, o["id"])
        assert row["status"] == "assigned", (o["id"], row["status"])
        assert row["assigned_account_id"] == acc["id"] and row["current_attempt_id"] is None
        assert row["purchased_attempt_id"] is None and row["page_load_retries"] == 0
    assert fx.account_row(conn, acc["id"])["execution_paused"] is False
    assert quota.used(conn, acc["id"], TODAY, True) == 0
    run = fx.run_row(conn, run_id)
    assert run["end_reason"] == "simulation_reset"
    assert f.count(conn, "events", "kind = 'simulation.reset'") == 1
    # 清除之后可以重新模拟执行
    fx.refresh_identity(conn, dev, acc)
    again = fx.claim(conn, dev, fx.start(conn, dev))
    assert again["kind"] == "auto"


def test_reset_with_inflight_submitted_attempt_does_not_pause_account(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_submitted(conn, dev, a)
    out = simulation.reset(conn, ws, uid)
    assert out["runs_ended"] == [run_id] and out["orders_restored"] == [order["id"]]
    assert fx.attempt_row(conn, a["attempt_id"])["status"] == "aborted"
    assert fx.order_row(conn, order["id"])["status"] == "assigned"
    assert fx.account_row(conn, acc["id"])["execution_paused"] is False


def test_reset_resumes_simulated_account_level_pause_but_keeps_manual_and_real_ones(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    manual, dev_m = _env(conn, workspace)
    captcha, dev_c = _env(conn, workspace)
    mixed, dev_x = _env(conn, workspace)
    real, dev_r = _env(conn, workspace)
    # 人工暂停
    accounts.pause(conn, ws, manual["id"], "DEMO 人工暂停", actor_kind="user", actor_id=uid)
    # 模拟中遇到验证码（模拟出来的账号级错误）→ 清除模拟结果后恢复
    fx.assigned_order(conn, ws, captcha)
    a = fx.claim(conn, dev_c, fx.start(conn, dev_c))
    execution.report_result(conn, dev_c, a["attempt_id"], fx.failure_payload("CAPTCHA_ENCOUNTERED"))
    assert fx.account_row(conn, captcha["id"])["execution_paused"] is True
    assert simulation.paused_only_by_simulation(conn, ws, captcha["id"]) is True
    # 模拟待核实 + 之后又人工暂停
    fx.assigned_order(conn, ws, mixed)
    b = fx.claim(conn, dev_x, fx.start(conn, dev_x))
    execution.report_result(conn, dev_x, b["attempt_id"], fx.failure_payload(None, outcome="unknown"))
    accounts.pause(conn, ws, mixed["id"], "DEMO 再暂停", actor_kind="user", actor_id=uid)
    # 真实批次（服务端本阶段不开放，这里直接造）里的账号级错误：不是模拟结果，不恢复
    fx.assigned_order(conn, ws, real)
    real_run = f.make_run(conn, ws, real["id"], dev_r["id"], simulated=False)
    c = fx.claim(conn, dev_r, real_run["id"])
    assert c["simulated"] is False
    execution.report_result(conn, dev_r, c["attempt_id"], fx.failure_payload("CAPTCHA_ENCOUNTERED"))

    out = simulation.reset(conn, ws, uid)
    assert out["accounts_resumed"] == [captcha["id"]]
    assert fx.account_row(conn, captcha["id"])["execution_paused"] is False
    for acc in (manual, mixed, real):
        assert fx.account_row(conn, acc["id"])["execution_paused"] is True


def test_account_paused_resumed_then_paused_by_simulation_only_is_resumed(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace)
    accounts.pause(conn, ws, acc["id"], "DEMO 早先暂停", actor_kind="user", actor_id=uid)
    accounts.resume(conn, ws, acc["id"], actor_kind="user", actor_id=uid)
    fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload(None, outcome="unknown"))
    assert simulation.paused_only_by_simulation(conn, ws, acc["id"]) is True
    assert simulation.reset(conn, ws, uid)["accounts_resumed"] == [acc["id"]]


def test_reset_leaves_real_and_external_attempts_and_other_workspaces(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace)
    ext = f.ingest_ready_order(conn, ws, external_amz_order_no="111-0000000-0000042")
    assert ext["status"] == "purchased"
    other = f.make_workspace(conn)
    o_acc, o_dev = _env(conn, other)
    o_order = fx.assigned_order(conn, other["id"], o_acc)
    fx.complete_success(conn, o_dev, fx.claim(conn, o_dev, fx.start(conn, o_dev)))
    out = simulation.reset(conn, ws, uid)
    assert out["attempts_reset"] == 0 and out["orders_restored"] == []
    assert fx.order_row(conn, ext["id"])["status"] == "purchased"
    assert fx.order_row(conn, o_order["id"])["status"] == "purchased"


def test_reset_is_idempotent(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    fx.complete_success(conn, dev, fx.claim(conn, dev, run_id))
    first = simulation.reset(conn, ws, uid)
    assert first["attempts_reset"] == 1
    second = simulation.reset(conn, ws, uid)
    assert second["attempts_reset"] == 0 and second["orders_restored"] == [] and second["runs_ended"] == []


def test_reset_unassigns_when_assignment_no_longer_valid(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace, min_amount="20", max_amount="50")
    order = fx.assigned_order(conn, ws, acc)
    fx.complete_success(conn, dev, fx.claim(conn, dev, fx.start(conn, dev)))
    conn.execute("UPDATE procure.buyer_accounts SET min_amount = 30 WHERE id = %s", (acc["id"],))
    simulation.reset(conn, ws, uid)
    row = fx.order_row(conn, order["id"])
    assert row["status"] == "queued" and row["assigned_account_id"] is None


def test_reset_restored_order_details_show_history(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    fx.complete_success(conn, dev, fx.claim(conn, dev, fx.start(conn, dev)))
    simulation.reset(conn, ws, uid)
    detail = orders.get_detail(conn, ws, order["id"])
    assert detail["status"] == "assigned"
    assert detail["attempts"][0]["status"] == "aborted" and detail["attempts"][0]["resolution"] == "simulation_reset"


def test_reset_rewrites_stale_reason_of_restored_orders(conn, workspace):
    """模拟遇到验证码后任务保留在已分配、原因写着「账号已暂停执行，任务保留」；清除后原因按派生规则重写。"""
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("CAPTCHA_ENCOUNTERED", "模拟"))
    assert "任务保留" in fx.order_row(conn, order["id"])["status_reason"]
    out = simulation.reset(conn, ws, uid)
    assert out["orders_restored"] == [order["id"]] and out["accounts_resumed"] == [acc["id"]]
    row = fx.order_row(conn, order["id"])
    assert row["status"] == "assigned" and row["status_reason"] is None
