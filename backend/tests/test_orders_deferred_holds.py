"""模拟采购期间被挡住的「这笔任务不该再执行」的来源变化（NEW-D4-sources-2、NEW-RB-4、NEW-RB-5）：

- 模拟成功不算「本系统采购成功」：之后来源说「已拍单」照样记准入暂停（提醒 + 推迟），清除模拟结果后转待核实；
- 采购方变了、平台订单号变了同样推迟，清除模拟结果后转待核实（平台订单号冲突带 link_id，「接受」后按新订单号接收）；
- 执行中未登记提交时平台订单号变了 → 写在任务上，插件进度得到 SOURCE_HOLD，中止后转待核实；
- 来源又改回去了的推迟暂停，按来源现状不再成立（orders.deferred_hold_applies）。

全部数据虚构（DEMO 名称、假地址、SIM- 单号）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb

from services import clock, execution, orders, simulation
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
ADM = {"purchaser_values": ["自采购DEMO"]}


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _env(conn, workspace, no):
    """飞书来源（连接的准入规则：只负责「自采购DEMO」）的已分配任务、已配对设备。"""
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    src = f.feishu_source(conn, ws, f"rec{no}")
    conn.execute("UPDATE procure.feishu_connections SET admission = %s WHERE id = %s", (Jsonb(ADM), src.connection_id))
    order = fx.assigned_order(conn, ws, acc, source=src, upstream_order_no=no, purchaser="自采购DEMO")
    assert order["status"] == "assigned"
    return {"ws": ws, "uid": workspace["user_id"], "acc": acc, "dev": dev, "src": src, "order": order}


def _sim_purchase(conn, e):
    run_id = fx.start(conn, e["dev"])
    a = fx.claim(conn, e["dev"], run_id)
    res = fx.complete_success(conn, e["dev"], a)
    assert res["order_status"] == "purchased"
    return a


def _ingest(conn, e, **kw):
    kw.setdefault("upstream_order_no", e["order"]["upstream_order_no"])
    kw.setdefault("purchaser", "自采购DEMO")
    return orders.ingest(conn, e["ws"], f.make_record(**kw), e["src"], admission=ADM)


def _kinds(conn, oid):
    return [r["kind"] for r in conn.execute("SELECT kind FROM procure.events WHERE order_id = %s ORDER BY id",
                                            (oid,)).fetchall()]


def _deferred(conn, oid):
    return [r["detail"] for r in conn.execute("SELECT detail FROM procure.events WHERE order_id = %s AND kind = %s "
                                              "ORDER BY id", (oid, orders.HOLD_DEFERRED_KIND)).fetchall()]


def _claim_none(conn, e):
    fx.refresh_identity(conn, e["dev"], e["acc"])
    run_id = fx.start(conn, e["dev"])
    return execution.next_attempt(conn, e["dev"], run_id)["attempt"]


def test_bought_status_after_a_simulated_success_is_held_and_lands_on_reset(conn, workspace):
    e = _env(conn, workspace, "PO-DEMO-SH1")
    a = _sim_purchase(conn, e)
    out = _ingest(conn, e, purchase_status_value="已拍单")
    row = fx.order_row(conn, e["order"]["id"])
    assert out.verdict == "conflict" and row["status"] == "purchased" and row["needs_attention"]
    assert "order.admission_hold" in _kinds(conn, row["id"])
    [d] = _deferred(conn, row["id"])
    assert d["attempt_id"] == a["attempt_id"] and d["conflict"]["kind"] == "admission"
    assert _ingest(conn, e, purchase_status_value="已拍单").verdict == "unchanged"     # 同样的值只提醒一次
    assert len(_deferred(conn, row["id"])) == 1

    simulation.reset(conn, e["ws"], e["uid"])
    row = fx.order_row(conn, e["order"]["id"])
    assert row["status"] == "conflict" and row["conflict"]["kind"] == "admission"
    assert row["assigned_account_id"] is None
    assert _claim_none(conn, e) is None


def test_purchaser_change_after_a_simulated_success_lands_on_reset(conn, workspace):
    e = _env(conn, workspace, "PO-DEMO-SH2")
    _sim_purchase(conn, e)
    out = _ingest(conn, e, purchaser="外采购DEMO")
    row = fx.order_row(conn, e["order"]["id"])
    assert out.verdict == "conflict" and row["status"] == "purchased" and row["needs_attention"]
    simulation.reset(conn, e["ws"], e["uid"])
    row = fx.order_row(conn, e["order"]["id"])
    assert row["status"] == "conflict" and row["conflict"]["kind"] == "admission"
    assert "purchaser" in row["conflict"]["fields"] and row["assigned_account_id"] is None
    assert _claim_none(conn, e) is None


def test_order_no_change_after_a_simulated_success_lands_on_reset_and_accept_relinks(conn, workspace):
    e = _env(conn, workspace, "PO-DEMO-SH3")
    _sim_purchase(conn, e)
    out = _ingest(conn, e, upstream_order_no="PO-DEMO-SH3-R")
    row = fx.order_row(conn, e["order"]["id"])
    assert out.verdict == "conflict" and row["status"] == "purchased" and row["needs_attention"]
    simulation.reset(conn, e["ws"], e["uid"])
    row = fx.order_row(conn, e["order"]["id"])
    assert row["status"] == "conflict" and row["conflict"]["kind"] == "source_key_changed"
    assert row["conflict"]["link_id"] is not None
    d = orders.resolve_conflict(conn, e["ws"], row["id"], "accept", None, e["uid"])
    assert d["status"] == "cancelled"
    again = _ingest(conn, e, upstream_order_no="PO-DEMO-SH3-R")
    assert again.verdict == "new" and again.order_id != row["id"]


def test_order_no_change_while_executing_unsubmitted_aborts_and_goes_conflict(conn, workspace):
    """NEW-RB-5：执行中未登记提交 → 冲突写在任务上 → 进度 SOURCE_HOLD → 中止后待核实（不会带着旧单号回到可领取）。"""
    e = _env(conn, workspace, "PO-DEMO-SH4")
    run_id = fx.start(conn, e["dev"])
    a = fx.claim(conn, e["dev"], run_id)
    _ingest(conn, e, upstream_order_no="PO-DEMO-SH4-R")
    p = execution.progress(conn, e["dev"], a["attempt_id"], stage="checkout")
    assert p["continue"] is False and p["cancel_code"] == "SOURCE_HOLD"
    res = execution.report_result(conn, e["dev"], a["attempt_id"],
                                  fx.failure_payload("ABORTED_BY_SERVER", p["cancel_reason"]))
    row = fx.order_row(conn, e["order"]["id"])
    assert res["order_status"] == "conflict" and row["conflict"]["kind"] == "source_key_changed"
    assert _ingest(conn, e, upstream_order_no="PO-DEMO-SH4-R").verdict == "conflict"   # 再读：不新建、只处理一次
    assert f.count(conn, "purchase_orders") == 1


def test_deferred_hold_applies_follows_the_current_source(conn, workspace):
    e = _env(conn, workspace, "PO-DEMO-SH5")
    _sim_purchase(conn, e)
    _ingest(conn, e, purchaser="外采购DEMO")
    [d] = _deferred(conn, e["order"]["id"])
    row = fx.order_row(conn, e["order"]["id"])
    assert orders.deferred_hold_applies(conn, row, d["conflict"]) is True
    _ingest(conn, e, purchaser="自采购DEMO")                                   # 采购期间又改回来了
    assert orders.deferred_hold_applies(conn, row, d["conflict"]) is False
    assert orders.deferred_hold_applies(conn, row, {"kind": "fields"}) is True


def test_purchaser_change_reverted_before_reset_returns_assigned(conn, workspace):
    """采购期间采购方改走又改回：清除模拟结果时按来源现状不再成立，任务回到已分配（不转待核实）。"""
    e = _env(conn, workspace, "PO-DEMO-SH6")
    _sim_purchase(conn, e)
    _ingest(conn, e, purchaser="外采购DEMO")
    _ingest(conn, e, purchaser="自采购DEMO")
    simulation.reset(conn, e["ws"], e["uid"])
    row = fx.order_row(conn, e["order"]["id"])
    assert row["status"] == "assigned" and row["conflict"] is None and row["assigned_account_id"] == e["acc"]["id"]
