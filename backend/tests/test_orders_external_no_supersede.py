"""领取后来源变化落地之后的后续（NEW-RB-3、NEW-D2-execution-safety-2）：

- 同种来源随后又改了冲突里的字段：冲突里过期的「来源新值」作废，「接受」不会把更旧的值写回去；
- 之后登记了可信的外部单号（同一单号、或之前格式不对的单号被新的有效单号取代）：外部单号冲突作废；
- 亚马逊取消后同意重采：不再拿刚取消的那一单当外部单号冲突；接受一个已登记在本任务的单号 → VALIDATION（不是 500）。

全部数据虚构（DEMO 名称、假地址、假 AMZ 订单号）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb

from services import clock, execution, orders
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx
from tests.api_helpers import (IMPORT_HEADERS, api, err, find_order, import_orders, import_row, ok,  # noqa: F401
                               owner, tx, xlsx_bytes)

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
EXT_NO = "111-0000000-0000123"


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _claimed(conn, workspace, no):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    src = f.file_source(conn, ws, 2)
    order = fx.assigned_order(conn, ws, acc, source=src, upstream_order_no=no)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    return {"ws": ws, "uid": workspace["user_id"], "acc": acc, "dev": dev, "src": src, "order": order, "a": a}


def _same_row(conn, e):
    return f.file_source(conn, e["ws"], 2, batch_id=e["src"].batch_id)


def _abort_after_progress(conn, e):
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="checkout")
    assert p["continue"] is False
    return execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                                   fx.failure_payload("ABORTED_BY_SERVER", p["cancel_reason"]))


def _rec(e, **kw):
    return f.make_record(upstream_order_no=e["order"]["upstream_order_no"], **kw)


# ── 冲突里过期的来源值（NEW-RB-3）────────────────────────────────────────────


def test_same_source_change_after_drift_conflict_replaces_the_stale_incoming(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-STALE1")
    orders.ingest(conn, e["ws"], _rec(e, asin="B0DEMO0X01"), _same_row(conn, e))
    _abort_after_progress(conn, e)
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["conflict"]["incoming"] == {"asin": "B0DEMO0X01"}
    out = orders.ingest(conn, e["ws"], _rec(e, asin="B0DEMO0X02"), _same_row(conn, e))
    o = fx.order_row(conn, e["order"]["id"])
    assert out.verdict == "update" and o["asin"] == "B0DEMO0X02"
    assert o["conflict"] is None and o["status"] == "queued"         # 冲突只剩过期的 ASIN：作废，离开待核实


def test_new_file_batch_back_to_the_original_value_drops_the_stale_incoming(conn, workspace):
    """新一批文件（新关联）送来原来的 ASIN：任务已持有同种来源最新的值，冲突里过期的 X01 作废。"""
    e = _claimed(conn, workspace, "PO-DEMO-STALE4")
    orders.ingest(conn, e["ws"], _rec(e, asin="B0DEMO0X01"), _same_row(conn, e))
    _abort_after_progress(conn, e)
    assert fx.order_row(conn, e["order"]["id"])["status"] == "conflict"
    orders.ingest(conn, e["ws"], _rec(e), f.file_source(conn, e["ws"], 2))
    o = fx.order_row(conn, e["order"]["id"])
    assert o["asin"] == "B0DEMO0001" and o["conflict"] is None and o["status"] == "queued"


def test_partial_refresh_keeps_the_other_fields_and_accept_keeps_the_newer_value(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-STALE2")
    orders.ingest(conn, e["ws"], _rec(e, asin="B0DEMO0X01", quantity=2), _same_row(conn, e))
    _abort_after_progress(conn, e)
    o = fx.order_row(conn, e["order"]["id"])
    assert set(o["conflict"]["incoming"]) == {"asin", "quantity"}
    orders.ingest(conn, e["ws"], _rec(e, asin="B0DEMO0X02", quantity=2), _same_row(conn, e))
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["asin"] == "B0DEMO0X02"
    assert o["conflict"]["incoming"] == {"quantity": 2} and set(o["conflict"]["fields"]) == {"quantity"}
    orders.resolve_conflict(conn, e["ws"], e["order"]["id"], "accept", None, e["uid"])
    o = fx.order_row(conn, e["order"]["id"])
    assert o["asin"] == "B0DEMO0X02" and o["quantity"] == 2 and o["conflict"] is None


def test_accept_skips_fields_the_primary_source_changed_after_the_conflict(conn, workspace):
    """兜底：主来源写的冲突里，任务原值在冲突之后已变的字段，「接受」跳过。"""
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-STALE3")
    stale = {"kind": "fields", "source_kind": "file", "reason": "采购过程中来源修改了 ASIN，需人工核实",
             "fields": {"asin": ["B0DEMO0001", "B0DEMO0X01"]}, "incoming": {"asin": "B0DEMO0X01"},
             "source": {"kind": "file", "batch_id": 1, "row_no": 2}, "at": NOW.isoformat()}
    f.set_status(conn, o["id"], "conflict", conflict=Jsonb(stale), asin="B0DEMO0X02")
    d = orders.resolve_conflict(conn, ws, o["id"], "accept", None, uid)
    assert fx.order_row(conn, o["id"])["asin"] == "B0DEMO0X02" and d["status"] == "queued"
    ev = conn.execute("SELECT detail FROM procure.events WHERE order_id = %s AND kind = 'order.conflict_resolved'",
                      (o["id"],)).fetchone()["detail"]
    assert ev["applied"] == {"skipped_stale": ["asin"]}


# ── 外部单号冲突被可信单号取代（NEW-D2-execution-safety-2）──────────────────


def test_reimport_of_the_same_external_number_clears_the_drift_conflict(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-XC1")
    orders.ingest(conn, e["ws"], _rec(e, external_amz_order_no=EXT_NO), _same_row(conn, e))
    _abort_after_progress(conn, e)
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["conflict"]["kind"] == "external_order_no"
    out = orders.ingest(conn, e["ws"], _rec(e, external_amz_order_no=EXT_NO), f.file_source(conn, e["ws"], 2))
    o = fx.order_row(conn, e["order"]["id"])
    assert out.verdict == "historical" and o["status"] == "purchased" and o["external_amz_order_no"] == EXT_NO
    assert o["conflict"] is None
    ev = conn.execute("SELECT detail FROM procure.events WHERE order_id = %s AND kind = 'order.conflict_superseded'",
                      (o["id"],)).fetchone()["detail"]
    assert ev["amazon_order_no"] == EXT_NO and ev["old_conflict"]["kind"] == "external_order_no"


def test_valid_number_supersedes_an_earlier_malformed_number_conflict(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recXC2")
    o = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-XC2"), src)
    bad = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-XC2", external_amz_order_no="BAD-NO-DEMO"),
                        src)
    assert bad.status == "conflict"
    good = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-XC2", external_amz_order_no=EXT_NO), src)
    row = fx.order_row(conn, o.order_id)
    assert good.status == "purchased" and row["status"] == "purchased" and row["conflict"] is None


def test_repurchase_after_amazon_cancel_does_not_land_in_its_own_number_conflict(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-XC3", external_amz_order_no=EXT_NO)
    assert o["status"] == "purchased"
    stale = {"kind": "external_order_no", "source_kind": "file", "reason": f"采购过程中来源出现 AMZ 订单号 {EXT_NO}",
             "fields": {"external_amz_order_no": [None, EXT_NO]},
             "incoming": {"external_amz_order_no": EXT_NO, "external_buyer_account": None},
             "source": {"kind": "file", "batch_id": 1, "row_no": 2}, "at": NOW.isoformat()}
    f.set_status(conn, o["id"], "purchased", conflict=Jsonb(stale))     # 旧版本留下的过期冲突
    orders.mark_amazon_cancelled(conn, ws, o["id"], None, uid)
    d = orders.decide_repurchase(conn, ws, o["id"], "approve", None, uid)
    assert d["status"] in ("queued", "assigned") and d["conflict"] is None


def test_accepting_a_number_already_registered_on_this_task_is_a_validation_error(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-XC4", external_amz_order_no=EXT_NO)
    orders.mark_amazon_cancelled(conn, ws, o["id"], None, uid)
    orders.decide_repurchase(conn, ws, o["id"], "approve", None, uid)
    again = {"kind": "external_order_no", "source_kind": "file", "reason": f"来源出现 AMZ 订单号 {EXT_NO}",
             "fields": {"external_amz_order_no": [None, EXT_NO]},
             "incoming": {"external_amz_order_no": EXT_NO, "external_buyer_account": None},
             "source": {"kind": "file", "batch_id": 1, "row_no": 2}, "at": NOW.isoformat()}
    f.set_status(conn, o["id"], "conflict", conflict=Jsonb(again))
    with pytest.raises(DomainError) as e:
        orders.resolve_conflict(conn, ws, o["id"], "accept", None, uid)
    assert e.value.code == "VALIDATION" and "已登记在本任务" in e.value.message
    n = f.count(conn, "purchase_attempts", "purchase_order_id = %s AND amazon_order_no = %s", (o["id"], EXT_NO))
    assert n == 1


def test_http_accept_of_own_registered_number_returns_422(owner):
    imp = import_orders(owner, data=xlsx_bytes([import_row("PO-DEMO-XC5", AMZ订单号=EXT_NO)],
                                               headers=[*IMPORT_HEADERS, "AMZ订单号"]))
    assert imp["commit"]["stats"]["historical"] == 1
    row = find_order(owner, "PO-DEMO-XC5")
    ok(owner.post(f"/api/v1/orders/{row['id']}/mark-amazon-cancelled", json={}))
    ok(owner.post(f"/api/v1/orders/{row['id']}/repurchase", json={"decision": "approve"}))
    again = {"kind": "external_order_no", "source_kind": "file", "reason": f"来源出现 AMZ 订单号 {EXT_NO}",
             "fields": {"external_amz_order_no": [None, EXT_NO]},
             "incoming": {"external_amz_order_no": EXT_NO, "external_buyer_account": None},
             "source": {"kind": "file", "batch_id": 1, "row_no": 2}, "at": NOW.isoformat()}
    with tx() as c:
        c.execute("UPDATE procure.purchase_orders SET status = 'conflict', conflict = %s WHERE id = %s",
                  (Jsonb(again), row["id"]))
    body = err(owner.post(f"/api/v1/orders/{row['id']}/resolve-conflict", json={"choice": "accept"}), 422,
               "VALIDATION")
    assert "已登记在本任务" in body["message"]
