"""只由文件快照说出的上游取消可以在控制台撤销（NEW-D1-business-rules-1）；控制台保留过的准入冲突不因新一批文件重传
而再次暂停（NEW-D1-business-rules-2）。全部数据虚构。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services import clock, execution, imports, normalize, orders
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx
from tests.api_helpers import (IMPORT_HEADERS, api, find_order, import_orders, import_row, ok,  # noqa: F401
                               owner, xlsx_bytes)

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _file_rec(conn, ws, no, **vals):
    adm = imports._file_admission(conn, ws, None)
    base = {"upstream_order_no": no, "asin": "B0DEMO0001", "quantity": 1, "unit_price": "25.00", "price_cap": "40.00",
            "review_value": "通过", "recipient_name": "Demo Buyer", "address1": "100 Demo Street",
            "city": "Demo City", "state": "FL", "postal_code": "32960", "phone": "555-0100",
            "suggested_purchase_date": "2026-10-31"}
    base.update(vals)
    return normalize.normalize(base, adm), adm


def _file_ingest(conn, ws, no, **vals):
    rec, adm = _file_rec(conn, ws, no, **vals)
    return orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2), admission=adm)


def _row(conn, ws, oid):
    return orders.get_row(conn, ws, oid)


def _assign(conn, row, acc):
    return orders.transition(conn, row, "assigned", actor_kind="system",
                             updates={"assigned_account_id": acc["id"], "assignment_kind": "auto",
                                      "assigned_at": clock.now(), "assignment_note": "DEMO 分配"})


# ── 撤销文件快照的取消 ──────────────────────────────────────────────────────────


def test_file_only_cancel_can_be_uncancelled_from_the_console(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    out = _file_ingest(conn, ws, "PO-DEMO-UC1", after_sale_value="已取消")
    assert out.status == "cancelled" and _row(conn, ws, out.order_id)["upstream_cancelled"]
    later = _file_ingest(conn, ws, "PO-DEMO-UC1", after_sale_value="正常")     # 新批次说正常：不自动撤销
    assert later.status == "cancelled"
    d = orders.get_detail(conn, ws, out.order_id)
    assert "uncancel" in d["allowed_actions"]
    assert [s["upstream_cancelled"] for s in d["sources"]] == [True, False]
    d = orders.uncancel(conn, ws, out.order_id, "客户恢复了订单（DEMO）", uid)
    assert d["status"] == "queued" and d["upstream_cancelled"] is False and d["upstream_cancel_note"] is None
    ev = conn.execute("SELECT detail FROM procure.events WHERE order_id = %s AND kind = 'order.uncancelled'",
                      (out.order_id,)).fetchone()["detail"]
    first_link = d["sources"][0]["id"]
    assert ev == {"note": "客户恢复了订单（DEMO）", "overrode_upstream_cancel": True, "file_link_ids": [first_link],
                  "previous_cancel_note": "已取消"}


def test_uncancelled_assigned_task_rederives_and_stale_file_does_not_cancel_again(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    o = _file_ingest(conn, ws, "PO-DEMO-UC2")
    _assign(conn, _row(conn, ws, o.order_id), acc)
    cancelled = _file_ingest(conn, ws, "PO-DEMO-UC2", after_sale_value="已取消")
    assert cancelled.status == "cancelled" and _row(conn, ws, o.order_id)["assigned_account_id"] is None
    orders.uncancel(conn, ws, o.order_id, None, uid)
    assert _row(conn, ws, o.order_id)["status"] == "queued"
    again = _file_ingest(conn, ws, "PO-DEMO-UC2", after_sale_value="已取消")     # 同样的旧文件再上传
    row = _row(conn, ws, o.order_id)
    assert again.status == "queued" and row["status"] == "queued" and not row["upstream_cancelled"]
    assert "文件来源的售后状态为「已取消」，控制台已撤销取消，未再取消" in again.issues


def test_feishu_cancel_after_a_console_uncancel_still_cancels(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = _file_ingest(conn, ws, "PO-DEMO-UC3", after_sale_value="已取消")
    orders.uncancel(conn, ws, o.order_id, None, uid)
    src = f.feishu_source(conn, ws, "recUC3")
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-UC3", upstream_cancelled=True,
                                                after_sale_value="已取消"), src)
    assert out.status == "cancelled"
    with pytest.raises(DomainError) as e:
        orders.uncancel(conn, ws, o.order_id, None, uid)
    assert e.value.code == "VALIDATION" and e.value.message == "飞书来源仍为取消状态，不能撤销取消"


def test_stale_file_cancel_no_longer_blocks_after_feishu_says_normal(conn, workspace):
    """飞书取消 → 文件快照也说已取消 → 飞书改回正常：任务仍取消（另一条来源还说取消），但控制台现在可以撤销。"""
    ws, uid = workspace["id"], workspace["user_id"]
    src = f.feishu_source(conn, ws, "recUC4")
    o = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-UC4"), src)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-UC4", upstream_cancelled=True,
                                          after_sale_value="已取消"), src)
    with pytest.raises(DomainError):
        orders.uncancel(conn, ws, o.order_id, None, uid)
    _file_ingest(conn, ws, "PO-DEMO-UC4", after_sale_value="已取消")
    back = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-UC4", after_sale_value="正常"), src)
    assert back.status == "cancelled"
    assert "uncancel" in orders.get_detail(conn, ws, o.order_id)["allowed_actions"]
    d = orders.uncancel(conn, ws, o.order_id, None, uid)
    assert d["status"] == "queued" and d["upstream_cancelled"] is False


def test_http_uncancel_of_a_file_only_cancel(owner):
    headers = [*IMPORT_HEADERS, "售后状态"]
    first = import_orders(owner, data=xlsx_bytes([import_row("PO-DEMO-UC5", 售后状态="正常")], headers=headers))
    assert first["commit"]["stats"]["new"] == 1
    imp = import_orders(owner, data=xlsx_bytes([import_row("PO-DEMO-UC5", 售后状态="已取消")], headers=headers))
    assert imp["commit"]["stats"]["update"] == 1
    row = find_order(owner, "PO-DEMO-UC5")
    assert row["status"] == "cancelled"
    d = ok(owner.get(f"/api/v1/orders/{row['id']}"))
    assert "uncancel" in d["allowed_actions"]
    assert [s["upstream_cancelled"] for s in d["sources"]] == [False, True]
    d = ok(owner.post(f"/api/v1/orders/{row['id']}/uncancel", json={"note": "DEMO 恢复"}))
    assert d["status"] in ("queued", "assigned") and d["upstream_cancelled"] is False


# ── 控制台保留过的准入冲突 ───────────────────────────────────────────────────


def test_kept_admission_conflict_survives_a_reupload_of_the_same_file(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = _file_ingest(conn, ws, "PO-DEMO-AK1")
    held = _file_ingest(conn, ws, "PO-DEMO-AK1", purchase_status_value="已拍单")
    assert held.status == "conflict" and _row(conn, ws, o.order_id)["conflict"]["kind"] == "admission"
    orders.resolve_conflict(conn, ws, o.order_id, "keep", "DEMO 核实过，本系统负责", uid)
    assert _row(conn, ws, o.order_id)["status"] == "queued"
    again = _file_ingest(conn, ws, "PO-DEMO-AK1", purchase_status_value="已拍单")
    row = _row(conn, ws, o.order_id)
    assert again.verdict != "conflict" and row["status"] == "queued" and row["conflict"] is None
    assert any("控制台已保留，未暂停" in i for i in again.issues)


def test_kept_admission_conflict_does_not_abort_a_claimed_attempt_on_reupload(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    o = _file_ingest(conn, ws, "PO-DEMO-AK2")
    _file_ingest(conn, ws, "PO-DEMO-AK2", purchase_status_value="已拍单")
    orders.resolve_conflict(conn, ws, o.order_id, "keep", None, uid)
    _assign(conn, _row(conn, ws, o.order_id), acc)
    run = fx.start(conn, dev)
    a = fx.claim(conn, dev, run)
    _file_ingest(conn, ws, "PO-DEMO-AK2", purchase_status_value="已拍单")
    pr = execution.progress(conn, dev, a["attempt_id"], stage="checkout")
    assert pr["continue"] is True and pr["cancel_code"] is None
    assert _row(conn, ws, o.order_id)["conflict"] is None


def test_a_different_hold_value_after_keep_holds_again(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = _file_ingest(conn, ws, "PO-DEMO-AK3")
    _file_ingest(conn, ws, "PO-DEMO-AK3", purchase_status_value="已拍单")
    orders.resolve_conflict(conn, ws, o.order_id, "keep", None, uid)
    other = _file_ingest(conn, ws, "PO-DEMO-AK3", purchase_status_value="拿回")
    row = _row(conn, ws, o.order_id)
    assert other.status == "conflict" and row["status"] == "conflict" and row["conflict"]["kind"] == "admission"
