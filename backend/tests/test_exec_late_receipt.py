"""人工核实之后才送到的结果回执（评审 D1-05；契约 §5 第 11 步）：

- 尝试已由控制台核实（not_ordered / ordered），插件又送来不同的结果：不改尝试的状态、核实结论与金额，完整回执
  记进 'attempt.late_result'（另有 resolution、contradicts），返回 late_receipt（插件路由回 409
  late_receipt_recorded，插件不再补交）；同一份回执重复送达不重复记录；
- 与核实结论矛盾（核实未下单却送来带单号的成功、核实已下单却送来另一个单号）→ 任务置 needs_attention；非模拟尝试
  还按来源暂停的方式挡住重新领取（开始前 → 待核实，冲突种类 external_order_no；正被另一笔未提交的尝试执行 →
  冲突写在任务上，下一次进度 SOURCE_HOLD）；与核实结论一致的回执只记录；
- 中止、未核实的失败等其他已结束尝试收到不同结果 → 仍是普通 409 CONFLICT。

本阶段开不出真实批次，真实尝试用 tests/factories.make_run(simulated=False) 直接造。全部数据虚构
（DEMO 名称、假买家号 ID、111-0000000-00000xx 形式的假单号、SIM- 单号）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import accounts, clock, execution, orders, writeback
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
LATE_NO = "111-0000000-0000031"


@pytest.fixture(autouse=True)
def _frozen(clean_db, monkeypatch):
    monkeypatch.setattr(writeback, "enqueue_for_order", lambda c, ws, oid: None)
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _unknown_after_sweep(conn, workspace, no, *, simulated):
    """领取 → 过护栏 → 登记提交 → 租约过期清扫 → 结果待核实。"""
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    order = fx.assigned_order(conn, ws, acc, upstream_order_no=no)
    run = f.make_run(conn, ws, acc["id"], dev["id"], simulated=simulated)
    a = fx.claim(conn, dev, run["id"])
    assert a["simulated"] is simulated
    fx.to_submitted(conn, dev, a)
    clock.freeze(NOW + timedelta(seconds=181))
    assert execution.sweep(conn)["unknown"] == 1
    assert fx.order_row(conn, order["id"])["status"] == "result_unknown"
    return {"ws": ws, "uid": workspace["user_id"], "acc": acc, "dev": dev, "order": order, "a": a}


def _late_events(conn, attempt_id):
    return conn.execute("SELECT detail FROM procure.events WHERE entity = 'attempt' AND entity_id = %s "
                        "AND kind = 'attempt.late_result' ORDER BY id", (attempt_id,)).fetchall()


def test_late_success_after_not_ordered_holds_a_real_order_and_is_recorded_once(conn, workspace):
    e = _unknown_after_sweep(conn, workspace, "PO-DEMO-L1", simulated=False)
    orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "not_ordered", note="DEMO", actor_user_id=e["uid"])
    assert fx.order_row(conn, e["order"]["id"])["status"] == "assigned"

    body = fx.success_payload(e["a"], LATE_NO)
    out = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], body)
    assert out["late_receipt"] is True and out["attempt_status"] == "failed" and out["order_status"] == "conflict"
    att = fx.attempt_row(conn, e["a"]["attempt_id"])
    assert att["status"] == "failed" and att["resolution"] == "not_ordered"
    assert att["amazon_order_no"] is None and att["total"] is None             # 尝试与金额都不改
    evs = _late_events(conn, e["a"]["attempt_id"])
    assert len(evs) == 1
    d = evs[0]["detail"]
    assert d["resolution"] == "not_ordered" and d["contradicts"] is True and d["amazon_order_no"] == LATE_NO
    assert d["amounts"]["total"] is not None and d["card_last4"] == "0000"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["assigned_account_id"] is None and o["needs_attention"] is True
    assert LATE_NO in o["attention_note"] and "重复下单" in o["attention_note"]
    assert o["conflict"]["kind"] == "external_order_no"
    assert o["conflict"]["incoming"]["external_amz_order_no"] == LATE_NO
    # 同一份回执补交：不重复记录
    again = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], body)
    assert again["late_receipt"] is True and len(_late_events(conn, e["a"]["attempt_id"])) == 1
    # 控制台「接受」：按外部单号登记为已采购，不重新下单
    detail = orders.resolve_conflict(conn, e["ws"], o["id"], "accept", actor_user_id=e["uid"])
    assert detail["status"] == "purchased"


def test_late_success_after_not_ordered_on_a_simulated_attempt_only_flags(conn, workspace):
    e = _unknown_after_sweep(conn, workspace, "PO-DEMO-L2", simulated=True)
    orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "not_ordered", actor_user_id=e["uid"])
    no = fx.sim_order_no(e["a"])
    out = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.success_payload(e["a"], no))
    assert out["late_receipt"] is True and out["attempt_status"] == "failed" and out["order_status"] == "assigned"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "assigned" and o["conflict"] is None
    assert o["needs_attention"] is True and no in o["attention_note"]
    assert _late_events(conn, e["a"]["attempt_id"])[0]["detail"]["contradicts"] is True


def test_late_success_with_another_number_after_ordered_is_recorded_and_flagged(conn, workspace):
    e = _unknown_after_sweep(conn, workspace, "PO-DEMO-L3", simulated=False)
    orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "ordered", amazon_order_no="111-0000000-0000041",
                           actor_user_id=e["uid"])
    same = execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                                   fx.success_payload(e["a"], "111-0000000-0000041"))
    assert same.get("duplicate") is True and not same.get("late_receipt")      # 与核实单号相同：就是重复上报

    out = execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                                  fx.success_payload(e["a"], "111-0000000-0000042"))
    assert out["late_receipt"] is True and out["attempt_status"] == "succeeded" and out["order_status"] == "purchased"
    att = fx.attempt_row(conn, e["a"]["attempt_id"])
    assert att["status"] == "succeeded" and att["amazon_order_no"] == "111-0000000-0000041"
    d = _late_events(conn, e["a"]["attempt_id"])[-1]["detail"]
    assert d["resolution"] == "ordered" and d["contradicts"] is True
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "purchased" and o["needs_attention"] is True and "111-0000000-0000042" in o["attention_note"]


def test_late_failure_after_not_ordered_is_recorded_without_attention(conn, workspace):
    e = _unknown_after_sweep(conn, workspace, "PO-DEMO-L4", simulated=False)
    orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "not_ordered", actor_user_id=e["uid"])
    before = fx.order_row(conn, e["order"]["id"])
    out = execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                                  fx.failure_payload("PAGE_LOAD_FAILED", "DEMO 断网后页面没打开"))
    assert out["late_receipt"] is True and out["attempt_status"] == "failed"
    d = _late_events(conn, e["a"]["attempt_id"])[0]["detail"]
    assert d["resolution"] == "not_ordered" and d["contradicts"] is False and d["error_code"] == "PAGE_LOAD_FAILED"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "assigned" and o["conflict"] is None
    assert o["needs_attention"] == before["needs_attention"] and o["attention_note"] == before["attention_note"]


def test_late_success_while_a_new_attempt_is_running_stops_it_before_submit(conn, workspace):
    """核实未下单 → 恢复账号 → 新尝试已领取（未提交）时，旧尝试的成功回执送到：冲突写在任务上，新尝试进度
    SOURCE_HOLD、提交登记 409。"""
    e = _unknown_after_sweep(conn, workspace, "PO-DEMO-L5", simulated=False)
    orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "not_ordered", actor_user_id=e["uid"])
    accounts.resume(conn, e["ws"], e["acc"]["id"], actor_kind="user", actor_id=e["uid"])
    fx.refresh_identity(conn, e["dev"], e["acc"])
    run2 = f.make_run(conn, e["ws"], e["acc"]["id"], e["dev"]["id"], simulated=False)
    b = fx.claim(conn, e["dev"], run2["id"])
    assert b["order_id"] == e["order"]["id"]
    fx.to_guard_passed(conn, e["dev"], b)

    out = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.success_payload(e["a"], LATE_NO))
    assert out["late_receipt"] is True and out["order_status"] == "executing"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["conflict"]["kind"] == "external_order_no" and o["needs_attention"] is True
    p = execution.progress(conn, e["dev"], b["attempt_id"], stage="before_submit")
    assert p["continue"] is False and p["cancel_code"] == "SOURCE_HOLD" and LATE_NO in p["cancel_reason"]
    with pytest.raises(DomainError) as exc:
        execution.mark_submitting(conn, e["dev"], b["attempt_id"])
    assert exc.value.code == "SOURCE_CHANGED"
    execution.report_result(conn, e["dev"], b["attempt_id"],
                            fx.failure_payload("ABORTED_BY_SERVER", p["cancel_reason"]))
    assert fx.order_row(conn, e["order"]["id"])["status"] == "conflict"


def test_different_result_for_an_aborted_attempt_is_still_a_plain_conflict(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    fx.assigned_order(conn, ws, acc, upstream_order_no="PO-DEMO-L6")
    a = fx.claim(conn, dev, fx.start(conn, dev))
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("ABORTED_BY_USER", "DEMO"))
    with pytest.raises(DomainError) as exc:
        execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("OUT_OF_STOCK", "DEMO"))
    assert exc.value.code == "CONFLICT" and exc.value.detail == {"attempt_status": "aborted", "reported": "failed"}
    assert _late_events(conn, a["attempt_id"]) == []
