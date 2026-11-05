"""采购开始后才到的暂停（评审 NEW-RB-6；契约 §2「人审拒绝从不悄悄丢掉」「准入变化」）：

已登记提交的采购中、结果待核实、已采购时，另一种来源的人审拒绝或准入变化只能先提醒（ingest 记
'order.hold_deferred' 事件，attempt_id 为当时的尝试）。任务之后回到开始前（核实未下单、同意重采、清除模拟结果）
时 execution.drift_updates 把仍成立的暂停转成 conflict，任务不会带着被拒绝的人审结论回到可领取。

全部数据虚构（DEMO 名称、假地址、SIM- 单号）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb

from services import accounts, clock, events, execution, orders, simulation
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _env(conn, workspace, no, *, primary="file"):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    src = f.file_source(conn, ws, 2) if primary == "file" else f.feishu_source(conn, ws, f"rec{no}")
    order = fx.assigned_order(conn, ws, acc, source=src, upstream_order_no=no)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    return {"ws": ws, "uid": workspace["user_id"], "acc": acc, "dev": dev, "src": src, "order": order,
            "run_id": run_id, "a": a}


def _feishu_reject(conn, e, record_id="recDEMOREJ1", review="rejected", value="钓鱼"):
    rec = f.make_record(upstream_order_no=e["order"]["upstream_order_no"], review=review, review_value=value)
    return orders.ingest(conn, e["ws"], rec, f.feishu_source(conn, e["ws"], record_id))


def _not_claimable(conn, e):
    accounts.resume(conn, e["ws"], e["acc"]["id"], actor_kind="user", actor_id=e["uid"])
    fx.refresh_identity(conn, e["dev"], e["acc"])
    run = fx.run_row(conn, e["run_id"])
    run_id = fx.start(conn, e["dev"]) if run["ended_at"] is not None else e["run_id"]
    return execution.next_attempt(conn, e["dev"], run_id)["attempt"] is None


def _unknown_then_not_ordered(conn, e):
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                                  fx.failure_payload(None, "DEMO 断网", outcome="unknown"))
    assert res["order_status"] == "result_unknown"
    orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "not_ordered", actor_user_id=e["uid"])
    return fx.order_row(conn, e["order"]["id"])


def test_rejection_after_submit_becomes_conflict_when_not_ordered(conn, workspace):
    e = _env(conn, workspace, "PO-DEMO-DH1")
    fx.to_submitted(conn, e["dev"], e["a"])
    _feishu_reject(conn, e)
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "executing" and o["conflict"] is None and o["needs_attention"] is True
    o = _unknown_then_not_ordered(conn, e)
    assert o["status"] == "conflict" and o["conflict"]["kind"] == "review" and o["assigned_account_id"] is None
    assert "钓鱼" in o["conflict"]["reason"]
    assert _not_claimable(conn, e)


def test_admission_hold_after_submit_becomes_conflict_when_not_ordered(conn, workspace):
    e = _env(conn, workspace, "PO-DEMO-DH2", primary="feishu")
    fx.to_submitted(conn, e["dev"], e["a"])
    rec = f.make_record(upstream_order_no="PO-DEMO-DH2", purchase_status_value="已拍单")
    orders.ingest(conn, e["ws"], rec, f.feishu_source(conn, e["ws"], "recPO-DEMO-DH2"), admission={})
    assert fx.order_row(conn, e["order"]["id"])["conflict"] is None
    o = _unknown_then_not_ordered(conn, e)
    assert o["status"] == "conflict" and o["conflict"]["kind"] == "admission" and "已拍单" in o["conflict"]["reason"]
    assert _not_claimable(conn, e)


def test_rejection_while_purchased_becomes_conflict_on_repurchase(conn, workspace):
    e = _env(conn, workspace, "PO-DEMO-DH3")
    fx.complete_success(conn, e["dev"], e["a"])
    _feishu_reject(conn, e)
    assert fx.order_row(conn, e["order"]["id"])["status"] == "purchased"
    orders.mark_amazon_cancelled(conn, e["ws"], e["order"]["id"], "DEMO 亚马逊取消", actor_user_id=e["uid"])
    detail = orders.decide_repurchase(conn, e["ws"], e["order"]["id"], "approve", actor_user_id=e["uid"])
    assert detail["status"] == "conflict"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["conflict"]["kind"] == "review" and o["needs_attention"] is True


def test_rejection_while_sim_purchased_becomes_conflict_on_simulation_reset(conn, workspace):
    e = _env(conn, workspace, "PO-DEMO-DH4")
    fx.complete_success(conn, e["dev"], e["a"])
    _feishu_reject(conn, e)
    simulation.reset(conn, e["ws"], e["uid"])
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["conflict"]["kind"] == "review"
    assert o["purchased_attempt_id"] is None and o["assigned_account_id"] is None


def test_deferred_rejection_withdrawn_by_the_source_is_not_applied(conn, workspace):
    """采购期间那条飞书记录又改回「通过」：推迟的暂停不再成立，核实未下单后任务照常回到已分配。"""
    e = _env(conn, workspace, "PO-DEMO-DH5")
    fx.to_submitted(conn, e["dev"], e["a"])
    _feishu_reject(conn, e)
    _feishu_reject(conn, e, review="approved", value="通过")
    o = _unknown_then_not_ordered(conn, e)
    assert o["status"] == "assigned" and o["conflict"] is None


def test_deferred_hold_of_another_attempt_is_ignored_and_applied_once(conn, workspace):
    """只取 attempt_id 是这笔尝试的推迟暂停；同一笔尝试只落一次。"""
    e = _env(conn, workspace, "PO-DEMO-DH6")
    fx.to_submitted(conn, e["dev"], e["a"])
    hold = {"kind": "review", "source_kind": "feishu", "reason": "DEMO 别的尝试时的拒绝",
            "fields": {"review_status": ["approved", "rejected"]}, "incoming": {"review_status": "rejected"},
            "source": {"kind": "feishu", "connection_id": None, "record_id": "recDEMONONE"}}
    events.record(conn, e["ws"], "order", e["order"]["id"], execution.HOLD_DEFERRED_KIND, actor_kind="source",
                  detail={"conflict": hold, "attempt_id": e["a"]["attempt_id"] + 1000, "status": "executing"})
    o = _unknown_then_not_ordered(conn, e)
    assert o["status"] == "assigned" and o["conflict"] is None
    # 同一笔尝试已处理过：之后再补的事件不会被重复落地
    conn.execute("UPDATE procure.events SET detail = %s WHERE kind = %s",
                 (Jsonb({"conflict": hold, "attempt_id": e["a"]["attempt_id"], "status": "executing"}),
                  execution.HOLD_DEFERRED_KIND))
    att = fx.attempt_row(conn, e["a"]["attempt_id"])
    assert execution.drift_updates(conn, fx.order_row(conn, e["order"]["id"]), att, actor_kind="system",
                                   actor_id=None) is None
