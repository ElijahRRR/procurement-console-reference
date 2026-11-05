"""结果回报（契约 §5 结果处理表）：成功、提交后失败、账号级 / 任务级 / 页面错误、插件中止、单号校验、
写回入队（模拟永不写回）、重复上报幂等、结束批次时的在途尝试。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from services import accounts, clock, execution, orders, quota, writeback
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


@pytest.fixture()
def calls(monkeypatch):
    """记录 services.writeback.enqueue_for_order 的调用。"""
    seen: list[tuple[int, int]] = []

    def fake(conn, workspace_id, order_id):
        seen.append((workspace_id, order_id))
        return None

    monkeypatch.setattr(writeback, "enqueue_for_order", fake)
    return seen


@pytest.fixture()
def env(conn, workspace):
    acc = fx.exec_account(conn, workspace["id"])
    dev = fx.paired_device(conn, workspace, account=acc)
    return {"ws": workspace["id"], "acc": acc, "dev": dev, "uid": workspace["user_id"]}


def _claimed(conn, env, **kw):
    order = fx.assigned_order(conn, env["ws"], env["acc"], **kw)
    run_id = fx.start(conn, env["dev"]) if "run_id" not in env else env["run_id"]
    env["run_id"] = run_id
    return order, fx.claim(conn, env["dev"], run_id)


def _err(fn, *a, **kw) -> DomainError:
    with pytest.raises(DomainError) as e:
        fn(*a, **kw)
    return e.value


def _paused(conn, env):
    return fx.account_row(conn, env["acc"]["id"])["execution_paused"]


# ── 成功 ──────────────────────────────────────────────────────────────────────


def test_success_marks_purchased_increments_version_and_consumes_quota(conn, env, calls):
    order, a = _claimed(conn, env)
    fx.to_submitted(conn, env["dev"], a)
    body = fx.success_payload(a, card_last4="••7860", delivery={"text": "Arriving Nov 2", "date": "2026-11-02",
                                                               "days": None})
    res = execution.report_result(conn, env["dev"], a["attempt_id"], body)
    assert res == {"attempt_status": "succeeded", "order_status": "purchased"}
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["amazon_order_no"] == f"SIM-20261031-{a['attempt_id']}" and att["finished_at"] == NOW
    assert att["subtotal"] == Decimal("25.00") and att["tax"] == Decimal("1.50") and att["total"] == Decimal("26.50")
    assert att["card_last4"] == "7860" and att["delivery_date"] == date(2026, 11, 2) and att["delivery_days"] == 3
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "purchased" and o["purchased_attempt_id"] == a["attempt_id"]
    assert o["result_version"] == order["result_version"] + 1 and o["current_attempt_id"] is None
    assert "SIM-" in o["status_reason"]
    assert quota.used(conn, env["acc"]["id"], TODAY, True) == 1
    assert calls == []                                     # 模拟结果永不写回
    assert fx.run_row(conn, env["run_id"])["stats"] == {"claimed": 1, "succeeded": 1}


def test_non_simulated_success_enqueues_writeback(conn, env, calls):
    """真实批次本阶段开不出来；这里直接造一个非模拟批次，验证成功后会调用写回入队。"""
    ws, acc, dev = env["ws"], env["acc"], env["dev"]
    order = fx.assigned_order(conn, ws, acc)
    run = f.make_run(conn, ws, acc["id"], dev["id"], simulated=False)
    a = fx.claim(conn, dev, run["id"])
    assert a["simulated"] is False
    fx.to_submitted(conn, dev, a)
    bad = execution.report_result(conn, dev, a["attempt_id"], fx.success_payload(a, "SIM-20261031-1"))
    assert bad["attempt_status"] == "unknown"              # 真实尝试不能用模拟单号
    assert calls == []
    order2 = fx.assigned_order(conn, ws, acc)
    accounts.resume(conn, ws, acc["id"], actor_kind="user", actor_id=env["uid"])
    b = fx.claim(conn, dev, run["id"])
    fx.to_submitted(conn, dev, b)
    res = execution.report_result(conn, dev, b["attempt_id"], fx.success_payload(b, "111-0000000-0000001"))
    assert res["order_status"] == "purchased"
    assert calls == [(ws, order2["id"])]
    assert fx.order_row(conn, order["id"])["status"] == "result_unknown"


def test_simulated_success_requires_sim_prefix(conn, env, calls):
    order, a = _claimed(conn, env)
    fx.to_submitted(conn, env["dev"], a)
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.success_payload(a, "111-0000000-0000001"))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    att = fx.attempt_row(conn, a["attempt_id"])
    assert "SIM-" in att["error_detail"] and att["amazon_order_no"] == "111-0000000-0000001"
    assert _paused(conn, env)


def test_success_without_order_number_is_result_unknown(conn, env, calls):
    order, a = _claimed(conn, env)
    fx.to_submitted(conn, env["dev"], a)
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.success_payload(a, amazon_order_no=None))
    assert res["attempt_status"] == "unknown"
    assert "没有亚马逊订单号" in fx.attempt_row(conn, a["attempt_id"])["error_detail"]


def test_success_without_submit_registration_is_treated_as_possibly_ordered(conn, env, calls):
    """未登记提交却上报成功：不能当作未下单放回队列，按结果待核实处理。"""
    order, a = _claimed(conn, env)
    fx.to_guard_passed(conn, env["dev"], a)
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.success_payload(a))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    assert _paused(conn, env)


# ── 提交后的非成功 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("payload", [
    fx.failure_payload("OUT_OF_STOCK", "DEMO 缺货"),
    fx.failure_payload("PAGE_LOAD_FAILED"),
    fx.failure_payload(None, "DEMO 不明", outcome="unknown"),
    fx.failure_payload("ABORTED_BY_USER"),
    fx.failure_payload("GUARD_BLOCKED"),
    fx.failure_payload("SUBMIT_REJECTED"),
    fx.failure_payload("PLUGIN_SIMULATION_ONLY"),
    fx.failure_payload("CAPTCHA_ENCOUNTERED"),
    fx.failure_payload("SOMETHING_NEW"),
])
def test_any_non_success_after_submit_is_unknown_and_never_released(conn, env, calls, payload):
    """提交后断线 / 失败：进入结果待核实、暂停账号，不重买；额度不释放。"""
    order, a = _claimed(conn, env)
    fx.to_submitted(conn, env["dev"], a)
    res = execution.report_result(conn, env["dev"], a["attempt_id"], payload)
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "result_unknown" and o["current_attempt_id"] == a["attempt_id"]
    assert o["status_reason"].startswith("已登记提交但未确认结果")
    acc = fx.account_row(conn, env["acc"]["id"])
    assert acc["execution_paused"] is True and acc["pause_reason"] == f"结果待核实：{order['upstream_order_no']}"
    assert quota.used(conn, env["acc"]["id"], TODAY, True) == 1
    marker = conn.execute("SELECT detail FROM procure.events WHERE kind = %s", (execution.PAUSE_MARKER_KIND,)).fetchone()
    assert marker["detail"]["cause"] == "result_unknown" and marker["detail"]["simulated"] is True
    # 恢复账号后也不会再领到这一笔
    accounts.resume(conn, env["ws"], env["acc"]["id"], actor_kind="user", actor_id=env["uid"])
    assert execution.next_attempt(conn, env["dev"], env["run_id"])["attempt"] is None


def test_unknown_outcome_before_submit_is_also_result_unknown(conn, env, calls):
    order, a = _claimed(conn, env)
    res = execution.report_result(conn, env["dev"], a["attempt_id"],
                                  fx.failure_payload(None, "DEMO", outcome="unknown"))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    assert _paused(conn, env)


def test_resolve_unknown_not_ordered_releases_quota(conn, env, calls):
    order, a = _claimed(conn, env)
    fx.to_submitted(conn, env["dev"], a)
    execution.report_result(conn, env["dev"], a["attempt_id"], fx.failure_payload(None, outcome="unknown"))
    orders.resolve_unknown(conn, env["ws"], order["id"], "not_ordered", note="DEMO 核实未下单",
                           actor_user_id=env["uid"])
    assert quota.used(conn, env["acc"]["id"], TODAY, True) == 0
    assert fx.order_row(conn, order["id"])["status"] == "assigned"


# ── 提交前的失败 ──────────────────────────────────────────────────────────────


def test_page_load_failed_retried_once_then_exception(conn, env, calls):
    order, a = _claimed(conn, env)
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.failure_payload("PAGE_LOAD_FAILED"))
    assert res == {"attempt_status": "failed", "order_status": "assigned"}
    o = fx.order_row(conn, order["id"])
    assert o["page_load_retries"] == 1 and o["assigned_account_id"] == env["acc"]["id"]
    assert "自动重试一次" in o["status_reason"]
    b = fx.claim(conn, env["dev"], env["run_id"])
    assert b["order_id"] == order["id"] and b["kind"] == "retry"
    res = execution.report_result(conn, env["dev"], b["attempt_id"], fx.failure_payload("PAGE_LOAD_FAILED"))
    assert res == {"attempt_status": "failed", "order_status": "exception"}
    o = fx.order_row(conn, order["id"])
    assert "已自动重试一次仍失败" in o["status_reason"] and o["current_attempt_id"] is None
    assert quota.used(conn, env["acc"]["id"], TODAY, True) == 0
    assert not _paused(conn, env)


@pytest.mark.parametrize("code", ["LOGIN_REQUIRED", "ACCOUNT_RESTRICTED", "CARD_DECLINED", "CAPTCHA_ENCOUNTERED",
                                  "IDENTITY_MISMATCH", "PLUGIN_SIMULATION_ONLY"])
def test_account_level_codes_pause_account_and_keep_task_assigned(conn, env, calls, code):
    order, a = _claimed(conn, env)
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.failure_payload(code, "DEMO"))
    assert res == {"attempt_status": "failed", "order_status": "assigned"}
    o = fx.order_row(conn, order["id"])
    assert o["assigned_account_id"] == env["acc"]["id"] and o["assignment_kind"] == "auto"
    acc = fx.account_row(conn, env["acc"]["id"])
    assert acc["execution_paused"] is True
    hb = execution.run_heartbeat(conn, env["dev"], env["run_id"])
    assert hb["stop"] is True and hb["stop_reason"] == "account_paused"
    assert execution.next_attempt(conn, env["dev"], env["run_id"])["attempt"] is None
    marker = conn.execute("SELECT detail FROM procure.events WHERE kind = %s", (execution.PAUSE_MARKER_KIND,)).fetchone()
    assert marker["detail"]["cause"] == "account_error" and marker["detail"]["code"] == code


@pytest.mark.parametrize("code", ["OUT_OF_STOCK", "QUANTITY_UNAVAILABLE", "ADDRESS_FAILED", "CART_MISMATCH",
                                  "PRICE_OVER_CAP", "GUARD_BLOCKED", "SUBMIT_REJECTED", "SOMETHING_NEW"])
def test_task_level_codes_go_exception_and_run_continues(conn, env, calls, code):
    order, a = _claimed(conn, env)
    second = fx.assigned_order(conn, env["ws"], env["acc"])
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.failure_payload(code, "DEMO 原因"))
    assert res == {"attempt_status": "failed", "order_status": "exception"}
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "exception" and "DEMO 原因" in o["status_reason"]
    assert o["assigned_account_id"] == env["acc"]["id"]
    assert not _paused(conn, env)
    b = fx.claim(conn, env["dev"], env["run_id"])
    assert b["order_id"] == second["id"]


def test_plugin_abort_without_upstream_cancel_returns_task_to_assigned(conn, env, calls):
    order, a = _claimed(conn, env)
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.failure_payload("ABORTED_BY_USER", "DEMO"))
    assert res == {"attempt_status": "aborted", "order_status": "assigned"}
    assert quota.used(conn, env["acc"]["id"], TODAY, True) == 0


def test_invalid_result_payload(conn, env, calls):
    order, a = _claimed(conn, env)
    assert _err(execution.report_result, conn, env["dev"], a["attempt_id"], {"outcome": "great"}).code == "VALIDATION"
    assert _err(execution.report_result, conn, env["dev"], a["attempt_id"], "ok").code == "VALIDATION"
    assert fx.attempt_row(conn, a["attempt_id"])["status"] == "claimed"


def test_absurd_result_amounts_are_unreadable_not_errors(conn, env, calls):
    """位数大到放不进金额列的金额：按读不出（None）处理，不报 500，也不当 0。"""
    order, a = _claimed(conn, env)
    fx.to_submitted(conn, env["dev"], a)
    body = fx.success_payload(a, amounts={"subtotal": "9" * 30, "shipping": 1e300, "tax": "99999999999.00",
                                          "total_before_tax": "25.00", "total": "26.50"})
    assert execution.report_result(conn, env["dev"], a["attempt_id"], body)["attempt_status"] == "succeeded"
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["subtotal"] is None and att["shipping"] is None and att["tax"] is None
    assert att["total"] == Decimal("26.50")


def test_unrecognised_failure_code_without_detail_is_task_level(conn, env, calls):
    order, a = _claimed(conn, env)
    res = execution.report_result(conn, env["dev"], a["attempt_id"], fx.failure_payload("brand_new_code"))
    assert res == {"attempt_status": "failed", "order_status": "exception"}
    o = fx.order_row(conn, order["id"])
    assert o["status_reason"] == "插件上报失败（BRAND_NEW_CODE）"
    assert fx.attempt_row(conn, a["attempt_id"])["error_code"] == "BRAND_NEW_CODE"
    assert not _paused(conn, env)


@pytest.mark.parametrize("code,zh", [("GUARD_BLOCKED", "护栏拦截"), ("SUBMIT_REJECTED", "服务端拒绝了提交登记")])
def test_plugin_side_guard_and_submit_codes_go_exception_with_label(conn, env, calls, code, zh):
    order, a = _claimed(conn, env)
    execution.report_result(conn, env["dev"], a["attempt_id"], fx.failure_payload(code))
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "exception" and o["status_reason"] == zh
    assert fx.attempt_row(conn, a["attempt_id"])["status"] == "failed" and not _paused(conn, env)


def test_plugin_simulation_only_pauses_account_and_keeps_task(conn, env, calls):
    order, a = _claimed(conn, env)
    res = execution.report_result(conn, env["dev"], a["attempt_id"],
                                  fx.failure_payload("PLUGIN_SIMULATION_ONLY", "插件当前只做模拟执行"))
    assert res == {"attempt_status": "failed", "order_status": "assigned"}
    acc = fx.account_row(conn, env["acc"]["id"])
    assert acc["execution_paused"] is True and acc["pause_reason"] == "插件只执行模拟任务，却领到了真实任务"


# ── 幂等 ──────────────────────────────────────────────────────────────────────


def test_duplicate_result_report_is_idempotent_if_identical(conn, env, calls):
    order, a = _claimed(conn, env)
    fx.to_submitted(conn, env["dev"], a)
    body = fx.success_payload(a)
    first = execution.report_result(conn, env["dev"], a["attempt_id"], body)
    again = execution.report_result(conn, env["dev"], a["attempt_id"], body)
    assert again == {**first, "duplicate": True}
    assert fx.order_row(conn, order["id"])["result_version"] == order["result_version"] + 1
    e = _err(execution.report_result, conn, env["dev"], a["attempt_id"], fx.success_payload(a, "SIM-20261031-999"))
    assert e.code == "CONFLICT" and e.http_status == 409
    e = _err(execution.report_result, conn, env["dev"], a["attempt_id"], fx.failure_payload("OUT_OF_STOCK"))
    assert e.code == "CONFLICT"
    # 批次结束后重放同一份结果也不报错
    execution.finish_run(conn, env["dev"], env["run_id"])
    assert execution.report_result(conn, env["dev"], a["attempt_id"], body)["duplicate"] is True


def test_duplicate_failure_report_idempotent(conn, env, calls):
    order, a = _claimed(conn, env)
    body = fx.failure_payload("OUT_OF_STOCK", "DEMO")
    execution.report_result(conn, env["dev"], a["attempt_id"], body)
    assert execution.report_result(conn, env["dev"], a["attempt_id"], body)["duplicate"] is True
    assert f.count(conn, "events", "kind = 'attempt.failed'") == 1


# ── 结束批次 ──────────────────────────────────────────────────────────────────


def test_finish_run_with_unsubmitted_attempt_aborts_and_keeps_assignment(conn, env, calls):
    order, a = _claimed(conn, env)
    out = execution.finish_run(conn, env["dev"], env["run_id"], "stopped")
    assert out["end_reason"] == "stopped" and out["stats"]["aborted"] == 1
    assert fx.attempt_row(conn, a["attempt_id"])["status"] == "aborted"
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "assigned" and o["assigned_account_id"] == env["acc"]["id"]


def test_finish_run_with_submitted_attempt_goes_result_unknown(conn, env, calls):
    order, a = _claimed(conn, env)
    fx.to_submitted(conn, env["dev"], a)
    execution.finish_run(conn, env["dev"], env["run_id"], "success_rejected")
    assert fx.attempt_row(conn, a["attempt_id"])["status"] == "unknown"
    assert fx.order_row(conn, order["id"])["status"] == "result_unknown"
    assert _paused(conn, env)
