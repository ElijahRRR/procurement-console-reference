"""HTTP 集成：账号输出的 pause_cause / open_result_unknown（I4）、概览的 needs_attention（I5）、
批次详情的 sheets / headers（I6）。全部数据虚构（DEMO 名称、假地址、PO-DEMO 订单号）。"""

from __future__ import annotations

from tests.api_helpers import (api, create_account, err, find_order, import_orders, import_row, ok, other,  # noqa: F401
                               owner, tx, upload, xlsx_bytes)


def test_account_rows_and_detail_carry_pause_cause_and_open_result_unknown(owner):
    acc = create_account(owner)
    import_orders(owner, [import_row("PO-DEMO-PU1"), import_row("PO-DEMO-PU2")])
    got = ok(owner.get(f"/api/v1/accounts/{acc['id']}"))
    assert got["pause_cause"] is None and got["open_result_unknown"] == 0

    paused = ok(owner.post(f"/api/v1/accounts/{acc['id']}/pause", json={"reason": "DEMO 人工暂停"}))
    assert paused["pause_cause"] == "manual"
    row = find_order(owner, "PO-DEMO-PU1")
    with tx() as conn:
        att = conn.execute(
            "INSERT INTO procure.purchase_attempts (workspace_id, purchase_order_id, attempt_no, kind, account_id, "
            "simulated, status, bj_date, claimed_at, submitted_at, finished_at) "
            "SELECT workspace_id, id, 1, 'auto', %s, true, 'unknown', DATE '2026-10-31', now(), now(), now() "
            "FROM procure.purchase_orders WHERE id = %s RETURNING id",
            (acc["id"], row["id"]),
        ).fetchone()["id"]
        conn.execute("UPDATE procure.purchase_orders SET status = 'result_unknown', current_attempt_id = %s "
                     "WHERE id = %s", (att, row["id"]))
    listed = ok(owner.get("/api/v1/accounts"))["rows"]
    assert listed[0]["id"] == acc["id"] and listed[0]["open_result_unknown"] == 1
    assert listed[0]["pause_cause"] == "manual"
    # 前端整份回传（含新的只读键）仍可修改
    patched = ok(owner.patch(f"/api/v1/accounts/{acc['id']}", json={**listed[0], "notes": "DEMO 备注"}))
    assert patched["notes"] == "DEMO 备注" and patched["open_result_unknown"] == 1

    resumed = ok(owner.post(f"/api/v1/accounts/{acc['id']}/resume"))
    assert resumed["pause_cause"] is None and resumed["open_result_unknown"] == 1


def test_overview_needs_attention_is_workspace_scoped(owner, other):
    import_orders(owner, [import_row(f"PO-DEMO-OA{i}") for i in range(3)])
    assert ok(owner.get("/api/v1/overview"))["needs_attention"] == 0
    ids = [find_order(owner, f"PO-DEMO-OA{i}")["id"] for i in range(2)]
    with tx() as conn:
        conn.execute("UPDATE procure.purchase_orders SET needs_attention = true, attention_note = 'DEMO' "
                     "WHERE id = ANY(%s)", (ids,))
        conn.execute("UPDATE procure.purchase_orders SET status = 'closed' WHERE id = %s", (ids[1],))
    assert ok(owner.get("/api/v1/overview"))["needs_attention"] == 2
    assert ok(other.get("/api/v1/overview"))["needs_attention"] == 0


def test_batch_detail_has_sheets_and_headers(owner, other):
    up = upload(owner, xlsx_bytes([import_row("PO-DEMO-BD1")]))
    d = ok(owner.get(f"/api/v1/imports/{up['batch_id']}"))
    assert d["id"] == up["batch_id"] and d["sheet_name"] == "采购订单"
    assert d["sheets"] == [{"name": "采购订单", "row_count": 1, "headers": up["headers"]}]
    assert d["headers"] == up["headers"]
    err(other.get(f"/api/v1/imports/{up['batch_id']}"), 404, "NOT_FOUND")
    ok(owner.post(f"/api/v1/imports/{up['batch_id']}/discard"))
    d = ok(owner.get(f"/api/v1/imports/{up['batch_id']}"))
    assert d["status"] == "discarded" and d["sheets"] == [] and d["headers"] == []
