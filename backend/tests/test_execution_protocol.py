"""执行协议：开始批次的各项拒绝、执行权互斥、批次心跳、领取规则（日期门槛、额度、跨零点）、进度与上游取消、
提交登记前置条件、归属校验（跨工作区 404）、租约失效（v1 §10 / 方案 v2 §4 验收条目）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from services import accounts, clock, devices, execution, orders, quota
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _err(fn, *a, **kw) -> DomainError:
    with pytest.raises(DomainError) as e:
        fn(*a, **kw)
    return e.value


def _setup(conn, workspace, **acc_kw):
    acc = fx.exec_account(conn, workspace["id"], **acc_kw)
    dev = fx.paired_device(conn, workspace, account=acc)
    return acc, dev


# ── 开始批次 ──────────────────────────────────────────────────────────────────


def test_start_run_returns_lease_and_writes_event(conn, workspace):
    acc, dev = _setup(conn, workspace)
    out = execution.start_run(conn, dev, trigger="scheduled", simulated=True)
    assert set(out) == {"run_id", "lease_ttl_s", "lease_expires_at"} and out["lease_ttl_s"] == 180
    assert datetime.fromisoformat(out["lease_expires_at"]) == NOW + timedelta(seconds=180)
    run = fx.run_row(conn, out["run_id"])
    assert run["account_id"] == acc["id"] and run["device_id"] == dev["id"] and run["simulated"] is True
    assert run["trigger"] == "scheduled" and run["ended_at"] is None
    assert f.count(conn, "events", "entity = 'run' AND kind = 'run.started'") == 1


def test_start_run_refused_when_not_bound(conn, workspace):
    dev = fx.paired_device(conn, workspace)
    e = _err(execution.start_run, conn, dev, trigger="manual", simulated=True)
    assert e.code == "NOT_BOUND" and e.http_status == 403


def test_start_run_refused_for_archived_account(conn, workspace):
    acc, dev = _setup(conn, workspace)
    conn.execute("UPDATE procure.buyer_accounts SET archived_at = %s WHERE id = %s", (NOW, acc["id"]))
    e = _err(execution.start_run, conn, dev, trigger="manual", simulated=True)
    assert e.code == "NOT_BOUND" and "归档" in e.message


@pytest.mark.parametrize("report,status,word", [("mismatch", "mismatch", "不符"), ("signed_out", "signed_out", "没有登录"),
                                                 (None, "unknown", "识别")])
def test_start_run_refused_for_identity_problems(conn, workspace, report, status, word):
    """绑定环境实际登录了另一个买家账号（或未登录 / 未识别）时拒绝开始，说明各不相同。"""
    acc = fx.exec_account(conn, workspace["id"])
    dev = fx.paired_device(conn, workspace, account=acc, report=report)
    e = _err(execution.start_run, conn, dev, trigger="manual", simulated=True)
    assert e.code == "IDENTITY_MISMATCH" and e.http_status == 403
    assert e.detail["identity_status"] == status and e.detail["stale"] is False and word in e.message


def test_start_run_refused_when_account_has_no_customer_id(conn, workspace):
    acc, dev = _setup(conn, workspace, amazon_customer_id=None)
    e = _err(execution.start_run, conn, dev, trigger="manual", simulated=True)
    assert e.code == "IDENTITY_MISMATCH" and "还没有登记买家号 ID" in e.message


def test_start_run_refused_for_stale_identity(conn, workspace):
    acc, dev = _setup(conn, workspace)
    clock.freeze(NOW + timedelta(seconds=601))
    e = _err(execution.start_run, conn, dev, trigger="manual", simulated=True)
    assert e.code == "IDENTITY_MISMATCH" and e.detail["stale"] is True and "过期" in e.message
    clock.freeze(NOW + timedelta(seconds=600))
    assert fx.start(conn, dev)


def test_start_run_refused_when_account_paused(conn, workspace):
    acc, dev = _setup(conn, workspace)
    accounts.pause(conn, workspace["id"], acc["id"], "DEMO 人工暂停", actor_kind="user", actor_id=workspace["user_id"])
    e = _err(execution.start_run, conn, dev, trigger="manual", simulated=True)
    assert e.code == "ACCOUNT_PAUSED" and e.http_status == 403 and "DEMO 人工暂停" in e.message


def test_start_run_refuses_non_simulated(conn, workspace):
    acc, dev = _setup(conn, workspace)
    e = _err(execution.start_run, conn, dev, trigger="manual", simulated=False)
    assert e.code == "REAL_PURCHASE_DISABLED" and e.http_status == 403
    # 即使设置里的模拟开关被（绕过校验）改成 false，本阶段也一律拒绝
    conn.execute("UPDATE procure.workspace_settings SET simulation_only = false WHERE workspace_id = %s",
                 (workspace["id"],))
    assert _err(execution.start_run, conn, dev, trigger="manual", simulated=False).code == "REAL_PURCHASE_DISABLED"
    assert f.count(conn, "execution_runs") == 0


def test_start_run_validates_trigger_and_simulated(conn, workspace):
    acc, dev = _setup(conn, workspace)
    assert _err(execution.start_run, conn, dev, trigger="cron", simulated=True).code == "VALIDATION"
    assert _err(execution.start_run, conn, dev, trigger="manual", simulated="yes").code == "VALIDATION"


def test_two_devices_same_account_only_one_gets_execution_right(conn, workspace):
    """同账号在 Mac 和另一台电脑同时触发：只有一个执行者，另一端看到占用位置。"""
    acc = fx.exec_account(conn, workspace["id"])
    mac = fx.paired_device(conn, workspace, account=acc, name="Mac-DEMO")
    pc = fx.paired_device(conn, workspace, account=acc, name="PC-DEMO")
    run_id = fx.start(conn, mac)
    e = _err(execution.start_run, conn, pc, trigger="scheduled", simulated=True)
    assert e.code == "ACCOUNT_BUSY" and e.http_status == 409
    assert e.detail == {"holder_device_id": mac["id"], "holder_device_name": "Mac-DEMO", "since": NOW.isoformat()}
    assert "Mac-DEMO" in e.message
    # 同一台设备再开一次（例如两个插件页面）也被拒绝
    assert _err(execution.start_run, conn, mac, trigger="manual", simulated=True).code == "ACCOUNT_BUSY"
    execution.finish_run(conn, mac, run_id)
    assert fx.start(conn, pc)


def test_unique_index_backstops_busy_check_and_transaction_stays_usable(conn, workspace, monkeypatch):
    """预查漏掉时（并发）靠唯一索引拒绝；冲突在 SAVEPOINT 里捕获，调用方事务仍可继续使用。"""
    acc = fx.exec_account(conn, workspace["id"])
    mac = fx.paired_device(conn, workspace, account=acc, name="Mac-DEMO")
    pc = fx.paired_device(conn, workspace, account=acc, name="PC-DEMO")
    fx.start(conn, mac)
    real = execution._holder
    calls = {"n": 0}

    def flaky(c, ws, aid):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(c, ws, aid)

    monkeypatch.setattr(execution, "_holder", flaky)
    e = _err(execution.start_run, conn, pc, trigger="manual", simulated=True)
    assert e.code == "ACCOUNT_BUSY" and e.detail["holder_device_id"] == mac["id"]
    assert conn.execute("SELECT count(*) AS n FROM procure.execution_runs").fetchone()["n"] == 1


# ── 批次心跳 ──────────────────────────────────────────────────────────────────


def test_run_heartbeat_extends_lease_and_reports_stop(conn, workspace):
    acc, dev = _setup(conn, workspace)
    run_id = fx.start(conn, dev)
    clock.freeze(NOW + timedelta(seconds=100))
    out = execution.run_heartbeat(conn, dev, run_id)
    assert out == {"lease_expires_at": (NOW + timedelta(seconds=280)).isoformat(), "stop": False, "stop_reason": None}
    accounts.pause(conn, workspace["id"], acc["id"], "DEMO", actor_kind="user", actor_id=workspace["user_id"])
    out = execution.run_heartbeat(conn, dev, run_id)
    assert out["stop"] is True and out["stop_reason"] == "account_paused"
    accounts.resume(conn, workspace["id"], acc["id"], actor_kind="user", actor_id=workspace["user_id"])
    devices.report_identity(conn, dev, fx.identity("A9OTHER0001"))
    out = execution.run_heartbeat(conn, dev, run_id)
    assert out["stop"] is True and out["stop_reason"] == "identity_mismatch"


def test_lease_lost_after_expiry_or_finish(conn, workspace):
    acc, dev = _setup(conn, workspace)
    run_id = fx.start(conn, dev)
    clock.freeze(NOW + timedelta(seconds=180))
    e = _err(execution.run_heartbeat, conn, dev, run_id)
    assert e.code == "LEASE_LOST" and e.http_status == 409
    assert _err(execution.next_attempt, conn, dev, run_id).code == "LEASE_LOST"
    clock.freeze(NOW + timedelta(seconds=200))
    fx.refresh_identity(conn, dev, acc)
    execution.sweep(conn)
    run2 = fx.start(conn, dev)
    out = execution.finish_run(conn, dev, run2, "completed")
    assert out["end_reason"] == "completed" and out["ended_at"]
    assert _err(execution.next_attempt, conn, dev, run2).code == "LEASE_LOST"
    assert _err(execution.finish_run, conn, dev, run2).code == "LEASE_LOST"


# ── 领取 ──────────────────────────────────────────────────────────────────────


def test_next_attempt_payload_and_order_executing(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace, min_amount="20", max_amount="50", expected_card_last4="7860",
                      max_delivery_days=7)
    order = fx.assigned_order(conn, ws, acc, upstream_order_no="PO-DEMO-PAYLOAD", unit_price="24.00", quantity=2,
                              price_cap="60.00")
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    assert a["upstream_order_no"] == "PO-DEMO-PAYLOAD" and a["asin"] == "B0DEMO0001" and a["quantity"] == 2
    assert a["unit_price"] == "24.00" and a["subtotal"] == "48.00" and a["price_cap"] == "60.00"
    assert a["max_delivery_days"] == 7 and a["amount_range"] == {"min": "20.00", "max": "50.00"}
    assert a["expected_card_last4"] == "7860" and a["simulated"] is True and a["kind"] == "auto"
    assert a["recipient_name"] == "Demo Buyer" and a["postal_code"] == "32960" and a["bj_date"] == "2026-10-31"
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "claimed" and att["bj_date"] == TODAY and att["run_id"] == run_id
    assert att["task_snapshot"]["attempt_id"] == a["attempt_id"] and att["attempt_no"] == 1
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "executing" and o["current_attempt_id"] == a["attempt_id"]
    assert o["assigned_account_id"] == acc["id"]
    assert quota.used(conn, acc["id"], TODAY, True) == 1


def test_next_attempt_uses_override_values(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    orders.set_override(conn, ws, order["id"], asin="B0DEMO0099", price_cap="38.50", note="DEMO",
                        actor_user_id=workspace["user_id"])
    a = fx.claim(conn, dev, fx.start(conn, dev))
    assert a["asin"] == "B0DEMO0099" and a["price_cap"] == "38.50"


def test_next_attempt_refuses_while_run_has_attempt_in_flight(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    fx.claim(conn, dev, run_id)
    e = _err(execution.next_attempt, conn, dev, run_id)
    assert e.code == "ATTEMPT_IN_FLIGHT" and e.http_status == 409


def test_suggested_date_gate_and_claim_order(conn, workspace):
    """建议采购日期为明天不执行；今天或过去的按日期先后领取。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    tomorrow = fx.assigned_order(conn, ws, acc, suggested_purchase_date=TODAY + timedelta(days=1))
    today = fx.assigned_order(conn, ws, acc, suggested_purchase_date=TODAY)
    old = fx.assigned_order(conn, ws, acc, suggested_purchase_date=TODAY - timedelta(days=3))
    yesterday = fx.assigned_order(conn, ws, acc, suggested_purchase_date=TODAY - timedelta(days=1))
    run_id = fx.start(conn, dev)
    got = []
    for _ in range(3):
        a = fx.claim(conn, dev, run_id)
        got.append(a["order_id"])
        fx.complete_success(conn, dev, a)
    assert got == [old["id"], yesterday["id"], today["id"]]
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "1 笔建议采购日期在未来" in out["reason"]
    assert fx.order_row(conn, tomorrow["id"])["status"] == "assigned"
    # 第二天（北京时间）它就到期了
    clock.freeze(datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc))
    fx.refresh_identity(conn, dev, acc)
    execution.sweep(conn)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    assert a["order_id"] == tomorrow["id"]


def test_same_day_ordered_by_order_time_then_id(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    late = fx.assigned_order(conn, ws, acc, order_time=datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc))
    early = fx.assigned_order(conn, ws, acc, order_time=datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc))
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    assert a["order_id"] == early["id"]
    fx.complete_success(conn, dev, a)
    assert fx.claim(conn, dev, run_id)["order_id"] == late["id"]


def test_only_orders_assigned_to_this_account_and_eligible(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    other = fx.exec_account(conn, ws)
    fx.assigned_order(conn, ws, other)
    f.ingest_ready_order(conn, ws)                                  # 待分配
    cancelled = fx.assigned_order(conn, ws, acc)
    f.set_status(conn, cancelled["id"], "assigned", upstream_cancelled=True)
    out = execution.next_attempt(conn, dev, fx.start(conn, dev))
    assert out["attempt"] is None and out["reason"].startswith("今天没有可执行的任务")


def test_amount_out_of_range_at_claim_is_unassigned_and_skipped(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace, min_amount="20", max_amount="50")
    drifted = fx.assigned_order(conn, ws, acc, unit_price="30.00",
                                suggested_purchase_date=TODAY - timedelta(days=1))
    ok = fx.assigned_order(conn, ws, acc, unit_price="30.00")
    conn.execute("UPDATE procure.buyer_accounts SET max_amount = 29 WHERE id = %s", (acc["id"],))
    conn.execute("UPDATE procure.purchase_orders SET unit_price = 25 WHERE id = %s", (ok["id"],))
    a = fx.claim(conn, dev, fx.start(conn, dev))
    assert a["order_id"] == ok["id"]
    d = fx.order_row(conn, drifted["id"])
    assert d["status"] == "queued" and d["assigned_account_id"] is None and "已不在账号" in d["status_reason"]


def test_daily_limit_counts_inflight_unknown_and_succeeded(conn, workspace):
    """达到每日额度或额度被在途 / 待核实占满时不能再领取；未知结果不释放额度。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace, daily_limit=3)
    for status in ("unknown", "verifying", "succeeded"):
        o = f.ingest_ready_order(conn, ws)
        f.make_attempt(conn, ws, o["id"], acc["id"], status=status, submitted=True)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "额度已用完" in out["reason"] and "3 / 上限 3" in out["reason"]
    # 失败与中止的尝试不占额度
    conn.execute("UPDATE procure.purchase_attempts SET status = 'failed' WHERE status = 'succeeded'")
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "进行中的采购尝试" in out["reason"]     # 账号还有在途尝试（verifying）
    conn.execute("UPDATE procure.purchase_attempts SET status = 'aborted' WHERE status = 'verifying'")
    assert fx.claim(conn, dev, run_id)


def test_daily_limit_reached_within_run(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace, daily_limit=1)
    fx.assigned_order(conn, ws, acc)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    fx.complete_success(conn, dev, fx.claim(conn, dev, run_id))
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "额度已用完" in out["reason"]


def test_attempt_at_2358_beijing_counts_for_that_day_and_order_never_reclaimed(conn, workspace):
    """午夜切换时仍有提交结果不明的任务：保留关联与防重复保护，不能再次购买；额度算在开始那天。"""
    ws = workspace["id"]
    clock.freeze(datetime(2026, 10, 31, 15, 58, tzinfo=timezone.utc))          # 北京 10-31 23:58
    acc, dev = _setup(conn, workspace, daily_limit=5)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_submitted(conn, dev, a)
    clock.freeze(datetime(2026, 10, 31, 15, 59, 30, tzinfo=timezone.utc))      # 插件照常续租
    execution.run_heartbeat(conn, dev, run_id)
    clock.freeze(datetime(2026, 10, 31, 16, 2, tzinfo=timezone.utc))           # 北京 11-01 00:02
    execution.run_heartbeat(conn, dev, run_id)
    assert fx.attempt_row(conn, a["attempt_id"])["bj_date"] == TODAY
    assert quota.used(conn, acc["id"], TODAY, True) == 1
    assert quota.used(conn, acc["id"], TODAY + timedelta(days=1), True) == 0
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload(None, "DEMO 断线", outcome="unknown"))
    assert fx.order_row(conn, order["id"])["status"] == "result_unknown"
    assert quota.used(conn, acc["id"], TODAY, True) == 1                        # 未知结果不释放额度
    accounts.resume(conn, ws, acc["id"], actor_kind="user", actor_id=workspace["user_id"])
    execution.finish_run(conn, dev, run_id)
    fx.refresh_identity(conn, dev, acc)
    run2 = fx.start(conn, dev)
    out = execution.next_attempt(conn, dev, run2)
    assert out["attempt"] is None
    assert f.count(conn, "purchase_attempts", "purchase_order_id = %s", (order["id"],)) == 1


def test_next_attempt_stops_when_account_paused_or_identity_mismatch(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    accounts.pause(conn, ws, acc["id"], "DEMO 暂停", actor_kind="user", actor_id=workspace["user_id"])
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "已暂停执行" in out["reason"]
    accounts.resume(conn, ws, acc["id"], actor_kind="user", actor_id=workspace["user_id"])
    devices.report_identity(conn, dev, fx.identity("A9OTHER0001"))
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "不符" in out["reason"]


def test_retry_kind_after_page_load_failed(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("PAGE_LOAD_FAILED"))
    b = fx.claim(conn, dev, run_id)
    assert b["order_id"] == a["order_id"] and b["kind"] == "retry" and b["attempt_no"] == 2


def test_repurchase_kind_after_amazon_cancel_and_approval(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    fx.complete_success(conn, dev, fx.claim(conn, dev, run_id))
    orders.mark_amazon_cancelled(conn, ws, order["id"], "DEMO", actor_user_id=workspace["user_id"])
    orders.decide_repurchase(conn, ws, order["id"], "approve", actor_user_id=workspace["user_id"])
    row = fx.order_row(conn, order["id"])
    orders.transition(conn, row, "assigned", actor_kind="system",
                      updates={"assigned_account_id": acc["id"], "assignment_kind": "manual"})
    b = fx.claim(conn, dev, run_id)
    assert b["order_id"] == order["id"] and b["kind"] == "repurchase"


# ── 进度与上游取消 ────────────────────────────────────────────────────────────


def _cancel_upstream(conn, ws, order, src):
    rec = f.make_record(upstream_order_no=order["upstream_order_no"], upstream_cancelled=True,
                        after_sale_value="DEMO 退款")
    return orders.ingest(conn, ws, rec, src)


def test_progress_moves_to_checking_and_continues(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    out = execution.progress(conn, dev, a["attempt_id"], stage="started", detail={"outcome": "pass"})
    assert out == {"continue": True, "cancel_reason": None}
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "checking" and att["stage"] == "started"
    assert _err(execution.progress, conn, dev, a["attempt_id"], stage=" ").code == "VALIDATION"


def test_upstream_cancel_before_submit_stops_and_cancels(conn, workspace):
    """执行中收到取消、尚未提交：进度得到 continue=false → 插件上报中止 → 尝试 aborted、任务 cancelled。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    src = f.file_source(conn, ws, 2)
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-CANCEL")
    orders.transition(conn, order, "assigned", actor_kind="system",
                      updates={"assigned_account_id": acc["id"], "assignment_kind": "auto"})
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_guard_passed(conn, dev, a)
    _cancel_upstream(conn, ws, order, src)
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "executing" and o["upstream_cancelled"] is True
    out = execution.progress(conn, dev, a["attempt_id"], stage="before_submit")
    assert out["continue"] is False and "上游已取消" in out["cancel_reason"]
    e = _err(execution.mark_submitting, conn, dev, a["attempt_id"])
    assert e.http_status == 409 and "上游已取消" in e.message
    res = execution.report_result(conn, dev, a["attempt_id"],
                                  fx.failure_payload("ABORTED_UPSTREAM_CANCELLED", out["cancel_reason"]))
    assert res == {"attempt_status": "aborted", "order_status": "cancelled"}
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "cancelled" and o["assigned_account_id"] is None and o["current_attempt_id"] is None
    assert quota.used(conn, acc["id"], TODAY, True) == 0
    assert fx.account_row(conn, acc["id"])["execution_paused"] is False


def test_upstream_cancel_after_submit_goes_result_unknown(conn, workspace):
    """执行中收到取消、已经提交：先核实是否下单成功，不能直接判定为未采购。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    src = f.file_source(conn, ws, 2)
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-LATECANCEL")
    orders.transition(conn, order, "assigned", actor_kind="system",
                      updates={"assigned_account_id": acc["id"], "assignment_kind": "auto"})
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_submitted(conn, dev, a)
    _cancel_upstream(conn, ws, order, src)
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "result_unknown" and "执行中收到取消" in o["status_reason"]
    assert execution.progress(conn, dev, a["attempt_id"], stage="waiting_confirmation")["continue"] is True
    res = execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("UNEXPECTED_PAGE", "DEMO"))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    o = fx.order_row(conn, order["id"])
    assert "执行中收到取消" in o["status_reason"] and o["current_attempt_id"] == a["attempt_id"]
    assert fx.account_row(conn, acc["id"])["execution_paused"] is True


def test_upstream_cancel_after_submit_then_success_is_purchased_with_attention(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    src = f.file_source(conn, ws, 2)
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-LATECANCEL2")
    orders.transition(conn, order, "assigned", actor_kind="system",
                      updates={"assigned_account_id": acc["id"], "assignment_kind": "auto"})
    a = fx.claim(conn, dev, fx.start(conn, dev))
    fx.to_submitted(conn, dev, a)
    _cancel_upstream(conn, ws, order, src)
    res = execution.report_result(conn, dev, a["attempt_id"], fx.success_payload(a))
    assert res == {"attempt_status": "succeeded", "order_status": "purchased"}
    o = fx.order_row(conn, order["id"])
    assert o["needs_attention"] is True and "上游取消" in o["attention_note"]


def test_progress_on_finished_attempt_says_stop(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("OUT_OF_STOCK"))
    out = execution.progress(conn, dev, a["attempt_id"], stage="late")
    assert out["continue"] is False and "已结束" in out["cancel_reason"]


# ── 护栏与提交登记 ────────────────────────────────────────────────────────────


def test_submitting_without_guard_pass_is_refused(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    e = _err(execution.mark_submitting, conn, dev, a["attempt_id"])
    assert e.code == "CONFLICT" and e.http_status == 409 and "护栏" in e.message
    assert fx.attempt_row(conn, a["attempt_id"])["submitted_at"] is None


def test_guard_block_fails_attempt_sets_exception_and_refuses_submit(conn, workspace):
    """48/52/50：账号区间匹配，结算总额超限价 → 拦截，任务异常，提交登记 409。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace, min_amount="20", max_amount="50")
    order = fx.assigned_order(conn, ws, acc, unit_price="48.00", price_cap="50.00")
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    facts = fx.good_facts(fx.order_row(conn, order["id"]), total="52.00", tax="4.00")
    v = execution.submit_guard(conn, dev, a["attempt_id"], facts)
    assert v["verdict"] == "block" and v["code"] == "PRICE_OVER_CAP" and v["checks"]["subtotal"]["ok"] is True
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "failed" and att["error_code"] == "PRICE_OVER_CAP"
    assert att["guard_verdict"]["code"] == "PRICE_OVER_CAP" and att["guard_input"]["total"] == "52.00"
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "exception" and o["status_reason"].startswith("结算总额超过限价")
    assert _err(execution.mark_submitting, conn, dev, a["attempt_id"]).http_status == 409
    # 插件随后照例上报同样的失败：幂等
    res = execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("PRICE_OVER_CAP", "超限"))
    assert res["duplicate"] is True and res["attempt_status"] == "failed"
    assert quota.used(conn, acc["id"], TODAY, True) == 0


def test_guard_uses_latest_account_config(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    accounts.update(conn, ws, acc["id"], {"max_delivery_days": 2}, workspace["user_id"])
    v = execution.submit_guard(conn, dev, a["attempt_id"], fx.good_facts(fx.order_row(conn, order["id"])))
    assert v["code"] == "DELIVERY_TOO_LONG" and v["checks"]["delivery"]["expected"] == 2


def test_submitting_records_submitted_at_and_cannot_repeat(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    out = fx.to_submitted(conn, dev, a)
    assert out == {"ok": True, "submitted_at": NOW.isoformat()}
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "submitting" and att["submitted_at"] == NOW
    assert "已登记提交" in _err(execution.mark_submitting, conn, dev, a["attempt_id"]).message
    assert _err(execution.submit_guard, conn, dev, a["attempt_id"], {}).http_status == 409


def test_submitting_refused_when_account_paused_mid_attempt(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    fx.to_guard_passed(conn, dev, a)
    accounts.pause(conn, ws, acc["id"], "DEMO", actor_kind="user", actor_id=workspace["user_id"])
    assert execution.progress(conn, dev, a["attempt_id"], stage="before_submit")["continue"] is False
    assert _err(execution.mark_submitting, conn, dev, a["attempt_id"]).code == "ACCOUNT_PAUSED"


def test_verifying_keeps_task_and_quota(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    assert _err(execution.mark_verifying, conn, dev, a["attempt_id"]).http_status == 409
    fx.to_submitted(conn, dev, a)
    out = execution.mark_verifying(conn, dev, a["attempt_id"], "DEMO 银行验证")
    assert out == {"ok": True, "attempt_status": "verifying"}
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "executing" and "银行人工验证" in o["status_reason"]
    assert quota.used(conn, acc["id"], TODAY, True) == 1
    res = execution.report_result(conn, dev, a["attempt_id"], fx.success_payload(a))
    assert res["order_status"] == "purchased"


# ── 归属与跨工作区 ────────────────────────────────────────────────────────────


def test_cross_workspace_device_cannot_touch_attempt_or_run(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    other_ws = f.make_workspace(conn)
    other_acc = fx.exec_account(conn, other_ws["id"])
    intruder = fx.paired_device(conn, other_ws, account=other_acc)
    aid = a["attempt_id"]
    calls = [
        (execution.run_heartbeat, (run_id,), {}),
        (execution.next_attempt, (run_id,), {}),
        (execution.finish_run, (run_id,), {}),
        (execution.progress, (aid,), {"stage": "x"}),
        (execution.submit_guard, (aid, {}), {}),
        (execution.mark_submitting, (aid,), {}),
        (execution.mark_verifying, (aid,), {}),
        (execution.report_result, (aid, fx.success_payload(a)), {}),
    ]
    for fn, args, kw in calls:
        e = _err(fn, conn, intruder, *args, **kw)
        assert e.code == "NOT_FOUND" and e.http_status == 404, fn.__name__
    # 同工作区的另一台设备也碰不到别人的批次与尝试
    sibling = fx.paired_device(conn, workspace, account=fx.exec_account(conn, ws))
    assert _err(execution.progress, conn, sibling, aid, stage="x").code == "NOT_FOUND"
    assert _err(execution.run_heartbeat, conn, sibling, run_id).code == "NOT_FOUND"
    assert fx.attempt_row(conn, aid)["status"] == "claimed"
    assert _err(execution.progress, conn, dev, "abc", stage="x").code == "NOT_FOUND"


def test_revoked_device_calls_are_unauthorized(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    run_id = fx.start(conn, dev)
    devices.revoke(conn, ws, dev["id"], workspace["user_id"])
    e = _err(execution.run_heartbeat, conn, dev, run_id)
    assert e.code == "UNAUTHORIZED" and e.http_status == 401
