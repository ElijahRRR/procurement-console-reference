"""任务列表视图、行形状与详情（契约 §8、§10）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services import allocation, clock, orders
from services.errors import DomainError
from tests import factories as f

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)

ROW_KEYS = {
    "id", "upstream_order_no", "store_name", "sku", "asin", "asin_eff", "quantity", "quantity_eff", "unit_price",
    "subtotal_eff", "price_cap", "price_cap_eff", "has_override", "suggested_purchase_date", "order_time",
    "recipient_name", "state", "postal_code", "status", "status_zh", "status_reason", "missing_fields",
    "review_status", "needs_attention", "attention_note", "assigned_account", "assignment_kind", "assignment_note",
    "primary_source_kind", "source_kinds", "latest_attempt", "updated_at",
}


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def test_suggested_date_tomorrow_not_in_today_view_today_and_past_are(conn, workspace):
    """v1 §10：建议采购日期为明天 → 可查看但不在今日可采购；今天或过去 → 在今日可采购。"""
    ws = workspace["id"]
    f.make_account(conn, ws)
    tomorrow = f.ingest_ready_order(conn, ws, suggested_purchase_date=TODAY + timedelta(days=1))
    today = f.ingest_ready_order(conn, ws, suggested_purchase_date=TODAY)
    past = f.ingest_ready_order(conn, ws, suggested_purchase_date=TODAY - timedelta(days=2))
    allocation.allocate(conn, ws, trigger="manual")
    ids = lambda view: [r["id"] for r in orders.list_orders(conn, ws, view=view)["rows"]]  # noqa: E731
    assert ids("today") == [past["id"], today["id"]]
    assert ids("future") == [tomorrow["id"]]
    clock.freeze(NOW + timedelta(days=1))
    assert tomorrow["id"] in ids("today") and ids("future") == []


def test_row_shape_and_money_strings(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws, name="环境-DEMO")
    o = f.ingest_ready_order(conn, ws, unit_price=Decimal("41.97"), price_cap=Decimal("78.69"),
                             source=f.feishu_source(conn, ws, "recROW"))
    allocation.allocate(conn, ws, trigger="manual")
    f.make_attempt(conn, ws, o["id"], acc["id"], status="failed")
    row = orders.list_orders(conn, ws)["rows"][0]
    assert set(row) == ROW_KEYS
    assert row["unit_price"] == "41.97" and row["subtotal_eff"] == "41.97" and row["price_cap_eff"] == "78.69"
    assert row["status"] == "assigned" and row["status_zh"] == "已分配"
    assert row["assigned_account"] == {"id": acc["id"], "name": "环境-DEMO"}
    assert row["assignment_note"] == allocation.NOTE_COVERAGE and row["assignment_kind"] == "auto"
    assert row["source_kinds"] == ["feishu"] and row["primary_source_kind"] == "feishu"
    assert row["latest_attempt"]["status"] == "failed" and row["latest_attempt"]["simulated"] is True
    assert row["suggested_purchase_date"] == TODAY.isoformat()


def test_views_search_filters_and_paging(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    for i in range(5):
        f.ingest_ready_order(conn, ws, upstream_order_no=f"PO-DEMO-PG-{i}")
    pend = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-REVIEW", review_value=None, review="unknown")
    feishu = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-FS", source=f.feishu_source(conn, ws, "rFS"),
                                  recipient_name="Searchable Person")
    assert orders.list_orders(conn, ws, view="pending_review")["rows"][0]["id"] == pend["id"]
    assert orders.list_orders(conn, ws, view="unassigned")["total"] == 6
    page = orders.list_orders(conn, ws, view="all", page=2, page_size=3)
    assert page["total"] == 7 and len(page["rows"]) == 3
    assert orders.list_orders(conn, ws, q="searchable")["rows"][0]["id"] == feishu["id"]
    assert orders.list_orders(conn, ws, q="PO-DEMO-PG")["total"] == 5
    assert orders.list_orders(conn, ws, q="100%")["total"] == 0
    assert orders.list_orders(conn, ws, source="feishu")["total"] == 1
    assert orders.list_orders(conn, ws, source="file")["total"] == 6
    allocation.allocate(conn, ws, trigger="manual")
    assert orders.list_orders(conn, ws, account_id=acc["id"])["total"] == 6
    with pytest.raises(DomainError) as e:
        orders.list_orders(conn, ws, view="nope")
    assert e.value.code == "VALIDATION"


def test_detail_shape_sources_attempts_events_and_actions(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-DET", source=f.feishu_source(conn, ws, "rDET"))
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-DET"), f.file_source(conn, ws, 7))
    orders.set_override(conn, ws, o["id"], price_cap="70", note="DEMO", actor_user_id=uid)
    allocation.allocate(conn, ws, trigger="manual")
    f.make_attempt(conn, ws, o["id"], acc["id"], status="failed")
    d = orders.get_detail(conn, ws, o["id"])
    assert ROW_KEYS <= set(d)
    for k in ("original", "override", "address", "conflict", "sources", "attempts", "events", "allowed_actions"):
        assert k in d
    assert d["original"]["price_cap"] == "40.00" and d["original"]["suggested_date_basis"] == "source"
    assert d["override"]["price_cap"] == "70.00" and d["override"]["by_name"] == "测试所有者"
    assert d["address"]["postal_code"] == "32960"
    assert [s["kind"] for s in d["sources"]] == ["feishu", "file"]
    assert d["sources"][0]["connection_name"] == "DEMO 订单中心" and d["sources"][0]["is_writeback_target"]
    assert d["sources"][1]["filename"] == "demo-orders.xlsx" and d["sources"][1]["row_no"] == 7
    assert d["attempts"][0]["status"] == "failed" and d["attempts"][0]["account_name"] == acc["name"]
    kinds = [e["kind"] for e in d["events"]]
    assert "order.created" in kinds and "order.override_set" in kinds
    ev = next(e for e in d["events"] if e["kind"] == "order.override_set")
    assert ev["actor_name"] == "测试所有者"
    assert set(d["allowed_actions"]) == {"assign", "unassign", "override", "cancel"}


@pytest.mark.parametrize("status,expected", [
    ("pending_review", {"review", "override", "cancel"}),
    ("needs_info", {"override", "cancel"}),
    ("queued", {"assign", "override", "cancel"}),
    ("executing", set()),
    ("result_unknown", {"resolve_unknown"}),
    ("exception", {"override", "resume", "cancel"}),
    ("purchased", {"mark_amazon_cancelled"}),
    ("cancelled", {"uncancel"}),
    ("repurchase_pending", {"repurchase"}),
    ("closed", set()),
])
def test_allowed_actions_per_status(status, expected):
    assert set(orders.allowed_actions({"status": status, "upstream_cancelled": False, "conflict": None})) == expected


def test_conflict_status_offers_resolve_conflict():
    acts = orders.allowed_actions({"status": "conflict", "upstream_cancelled": False, "conflict": {"fields": {}}})
    assert "resolve_conflict" in acts
