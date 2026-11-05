"""HTTP：任务列表与详情、人工动作（人审、批量人审、改派、解除、覆盖值、取消 / 撤销、恢复）与 allowed_actions；
不允许的动作 → 409 INVALID_TRANSITION；事件可按任务查到。全部数据虚构。"""

from __future__ import annotations

from tests.api_helpers import (api, create_account, err, find_order, import_orders, import_row, ok,  # noqa: F401
                               owner)


def _detail(client, order_id):
    return ok(client.get(f"/api/v1/orders/{order_id}"))


def test_order_detail_has_original_sources_events_and_allowed_actions(owner):
    acc = create_account(owner)
    imp = import_orders(owner, [import_row("PO-DEMO-O0001")])
    row = find_order(owner, "PO-DEMO-O0001")
    d = _detail(owner, row["id"])
    assert d["status"] == "assigned" and d["status_zh"] == "已分配"
    assert d["assigned_account"]["id"] == acc["id"] and d["assignment_kind"] == "auto"
    assert d["original"]["price_cap"] == "40.00" and d["original"]["unit_price"] == "25.00"
    assert d["original"]["ship_days"] == 4 and d["address"]["postal_code"] == "32960"
    assert d["sources"][0]["kind"] == "file" and d["sources"][0]["batch_id"] == imp["upload"]["batch_id"]
    assert d["attempts"] == [] and d["events"]
    assert set(d["allowed_actions"]) == {"assign", "unassign", "override", "cancel"}

    evs = ok(owner.get("/api/v1/events", params={"order_id": row["id"]}))
    assert evs["total"] >= 1 and all(e["order_id"] == row["id"] for e in evs["rows"])
    assert {"id", "entity", "entity_id", "at", "actor_kind", "kind", "detail"} <= set(evs["rows"][0])
    by_entity = ok(owner.get("/api/v1/events", params={"entity": "account", "entity_id": acc["id"]}))
    assert by_entity["rows"] and all(e["entity"] == "account" for e in by_entity["rows"])


def test_review_assign_override_cancel_uncancel_via_api(owner):
    a = create_account(owner, name="环境-DEMO-A", amazon_customer_id="A1DEMO000001", priority=5)
    b = create_account(owner, name="环境-DEMO-B", amazon_customer_id="A1DEMO000002", priority=1)
    import_orders(owner, [import_row("PO-DEMO-O0002", 人工审核=None)])
    row = find_order(owner, "PO-DEMO-O0002")
    assert row["status"] == "pending_review"
    d = _detail(owner, row["id"])
    assert "review" in d["allowed_actions"] and "assign" not in d["allowed_actions"]

    err(owner.post(f"/api/v1/orders/{row['id']}/review", json={"decision": "maybe"}), 422, "VALIDATION")
    d = ok(owner.post(f"/api/v1/orders/{row['id']}/review", json={"decision": "approve", "note": "DEMO 人审"}))
    assert d["review_status"] == "approved" and d["status"] == "queued" and "review" not in d["allowed_actions"]

    d = ok(owner.post(f"/api/v1/orders/{row['id']}/assign", json={"account_id": b["id"]}))
    assert d["status"] == "assigned" and d["assignment_kind"] == "manual"
    assert d["assigned_account"] == {"id": b["id"], "name": "环境-DEMO-B"}
    err(owner.post(f"/api/v1/orders/{row['id']}/assign", json={"account_id": 999999}), 404, "NOT_FOUND")
    err(owner.post(f"/api/v1/orders/{row['id']}/assign", json={}), 422, "VALIDATION")

    d = ok(owner.post(f"/api/v1/orders/{row['id']}/override", json={"price_cap": "45.50", "note": "DEMO 提高限价"}))
    assert d["price_cap"] == "40.00" and d["price_cap_eff"] == "45.50" and d["has_override"] is True
    assert d["override"]["price_cap"] == "45.50" and d["override"]["note"] == "DEMO 提高限价"
    d = ok(owner.post(f"/api/v1/orders/{row['id']}/override", json={"quantity": 2, "note": "DEMO"}))
    assert d["quantity_eff"] == 2 and d["price_cap_eff"] == "45.50"          # 没传的限价覆盖值保持不变
    d = ok(owner.post(f"/api/v1/orders/{row['id']}/override", json={"price_cap": None, "quantity": None}))
    assert d["price_cap_eff"] == "40.00" and d["quantity_eff"] == 1 and d["has_override"] is False
    err(owner.post(f"/api/v1/orders/{row['id']}/override", json={"asin": "BAD"}), 422, "VALIDATION")
    err(owner.post(f"/api/v1/orders/{row['id']}/override", json={"note": "什么也没改"}), 422, "VALIDATION")

    d = ok(owner.post(f"/api/v1/orders/{row['id']}/unassign"))
    assert d["status"] in ("queued", "assigned")    # 解除后自动分配不会立刻再分

    d = ok(owner.post(f"/api/v1/orders/{row['id']}/cancel", json={"note": "DEMO 人工取消"}))
    assert d["status"] == "cancelled" and d["assigned_account"] is None
    assert d["allowed_actions"] == ["uncancel"]
    err(owner.post(f"/api/v1/orders/{row['id']}/cancel", json={"note": "再取消一次"}), 409, "INVALID_TRANSITION")
    err(owner.post(f"/api/v1/orders/{row['id']}/resume", json={}), 409, "INVALID_TRANSITION")
    err(owner.post(f"/api/v1/orders/{row['id']}/resolve-unknown", json={"outcome": "ordered"}), 409,
        "INVALID_TRANSITION")

    d = ok(owner.post(f"/api/v1/orders/{row['id']}/uncancel"))
    assert d["status"] in ("queued", "assigned") and "cancel" in d["allowed_actions"]
    assert a["id"] != b["id"]


def test_bulk_review_reports_updated_and_skipped(owner):
    import_orders(owner, [import_row("PO-DEMO-O0003", 人工审核=None), import_row("PO-DEMO-O0004", 人工审核=None)])
    ids = [find_order(owner, no)["id"] for no in ("PO-DEMO-O0003", "PO-DEMO-O0004")]
    out = ok(owner.post("/api/v1/orders/bulk-review", json={"ids": [*ids, 999999, "x"], "decision": "reject"}))
    assert out["updated"] == ids
    assert {s["id"] for s in out["skipped"]} == {999999, "x"}
    assert all(find_order(owner, no)["review_status"] == "rejected" for no in ("PO-DEMO-O0003", "PO-DEMO-O0004"))
    err(owner.post("/api/v1/orders/bulk-review", json={"ids": [], "decision": "approve"}), 422, "VALIDATION")


def test_order_list_filters_and_paging(owner):
    acc = create_account(owner)
    import_orders(owner, [import_row(f"PO-DEMO-P{i:04d}") for i in range(1, 6)])
    page1 = ok(owner.get("/api/v1/orders", params={"view": "all", "page": 1, "page_size": 2}))
    page3 = ok(owner.get("/api/v1/orders", params={"view": "all", "page": 3, "page_size": 2}))
    assert page1["total"] == 5 and len(page1["rows"]) == 2 and len(page3["rows"]) == 1
    assert ok(owner.get("/api/v1/orders", params={"q": "P0003"}))["total"] == 1
    assert ok(owner.get("/api/v1/orders", params={"account_id": acc["id"]}))["total"] == 5
    assert ok(owner.get("/api/v1/orders", params={"source": "feishu"}))["total"] == 0
    assert ok(owner.get("/api/v1/orders", params={"source": "file"}))["total"] == 5
    err(owner.get("/api/v1/orders", params={"source": "ftp"}), 422, "VALIDATION")
    err(owner.get("/api/v1/orders/999999"), 404, "NOT_FOUND")
