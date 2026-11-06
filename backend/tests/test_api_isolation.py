"""HTTP：跨工作区一律 404（v1 §10「甲尝试访问乙的任务」、方案 v2 §4「跨工作区拒绝访问」）——工作区 B 的用户读不到、
也动不了工作区 A 的任务、账号、设备、飞书连接、导入批次、模板与事件；列表里也看不到。全部数据虚构。"""

from __future__ import annotations

from tests import fake_feishu as ff
from tests.api_helpers import (api, create_account, err, find_order, import_orders, import_row, ok, other,  # noqa: F401
                               owner, pair_device, plugin_client, tx, upload, xlsx_bytes)


def test_workspace_b_cannot_read_or_act_on_workspace_a(owner, other, api):
    acc = create_account(owner)
    imp = import_orders(owner, [import_row("PO-DEMO-W0001")])
    order = find_order(owner, "PO-DEMO-W0001")
    dev = pair_device(owner, plugin_client(api), account=acc)
    pending = upload(owner, xlsx_bytes([import_row("PO-DEMO-W0002")]))
    tpl = ok(owner.post("/api/v1/import-templates", json={"name": "A 的模板", "mapping": {"单号": "upstream_order_no"}}))
    client = ff.FakeFeishuClient.with_order_table()
    with tx() as conn:
        a_ws = conn.execute("SELECT id FROM procure.workspaces WHERE name = %s", ("DEMO 工作区 A",)).fetchone()["id"]
        cid = ff.create_demo_connection(conn, a_ws, client)["id"]

    oid, aid, did, bid = order["id"], acc["id"], dev["device_id"], imp["upload"]["batch_id"]
    for method, path, body in [
        ("GET", f"/api/v1/orders/{oid}", None),
        ("POST", f"/api/v1/orders/{oid}/review", {"decision": "approve"}),
        ("POST", f"/api/v1/orders/{oid}/cancel", {"note": "越权"}),
        ("POST", f"/api/v1/orders/{oid}/unassign", {}),
        ("POST", f"/api/v1/orders/{oid}/override", {"price_cap": "1.00"}),
        ("GET", f"/api/v1/accounts/{aid}", None),
        ("PATCH", f"/api/v1/accounts/{aid}", {"daily_limit": 1}),
        ("POST", f"/api/v1/accounts/{aid}/pause", {"reason": "越权"}),
        ("POST", f"/api/v1/accounts/{aid}/resume", {}),
        ("GET", f"/api/v1/devices/{did}", None),
        ("PATCH", f"/api/v1/devices/{did}", {"name": "越权"}),
        ("POST", f"/api/v1/devices/{did}/revoke", {}),
        ("GET", f"/api/v1/feishu/connections/{cid}", None),
        ("PATCH", f"/api/v1/feishu/connections/{cid}", {"name": "越权"}),
        ("DELETE", f"/api/v1/feishu/connections/{cid}", None),
        ("POST", f"/api/v1/feishu/connections/{cid}/sync", {}),
        ("GET", f"/api/v1/feishu/connections/{cid}/runs", None),
        ("GET", f"/api/v1/imports/{bid}", None),
        ("GET", f"/api/v1/imports/{bid}/rows", None),
        ("GET", f"/api/v1/imports/{bid}/problems.xlsx", None),
        ("POST", f"/api/v1/imports/{pending['batch_id']}/preview", {}),
        ("POST", f"/api/v1/imports/{pending['batch_id']}/discard", {}),
        ("GET", "/api/v1/exports/orders.xlsx?batch_id=" + str(bid), None),
        ("DELETE", f"/api/v1/import-templates/{tpl['id']}", None),
    ]:
        resp = other.request(method, path, json=body)
        err(resp, 404, "NOT_FOUND")

    # B 改派 A 的任务给 B 自己的账号：同样 404
    b_acc = create_account(other, name="环境-DEMO-B")
    err(other.post(f"/api/v1/orders/{oid}/assign", json={"account_id": b_acc["id"]}), 404, "NOT_FOUND")
    # B 不能把 A 的账号绑到自己的设备上，也不能采用 A 的设备识别的 ID
    b_dev = pair_device(other, plugin_client(api), name="Mac-DEMO-B")
    err(other.patch(f"/api/v1/devices/{b_dev['device_id']}", json={"bound_account_id": aid}), 422, "VALIDATION")
    err(other.post(f"/api/v1/accounts/{b_acc['id']}/adopt-customer-id", json={"device_id": did}), 404, "NOT_FOUND")

    # 列表只有自己的
    assert ok(other.get("/api/v1/orders"))["total"] == 0
    assert [a["id"] for a in ok(other.get("/api/v1/accounts"))["rows"]] == [b_acc["id"]]
    assert [d["id"] for d in ok(other.get("/api/v1/devices"))["rows"]] == [b_dev["device_id"]]
    assert ok(other.get("/api/v1/feishu/connections"))["total"] == 0
    assert ok(other.get("/api/v1/imports"))["total"] == 0
    assert ok(other.get("/api/v1/import-templates"))["total"] == 0
    assert ok(other.get("/api/v1/events", params={"order_id": oid}))["total"] == 0
    assert ok(other.get("/api/v1/overview"))["counts"]["assigned"] == 0

    # A 的数据原封不动
    assert ok(owner.get(f"/api/v1/orders/{oid}"))["status"] == "assigned"
    assert ok(owner.get(f"/api/v1/accounts/{aid}"))["execution_paused"] is False
    assert ok(owner.get(f"/api/v1/devices/{did}"))["revoked_at"] is None
    assert ok(owner.get(f"/api/v1/feishu/connections/{cid}"))["enabled"] is True


def test_plugin_of_workspace_b_cannot_touch_runs_or_attempts_of_workspace_a(owner, other, api):
    acc = create_account(owner)
    import_orders(owner, [import_row("PO-DEMO-W0101")])
    plugin = plugin_client(api)
    a_dev = pair_device(owner, plugin, account=acc)
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True},
                         headers={"Authorization": f"Bearer {a_dev['token']}"}))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next",
                         headers={"Authorization": f"Bearer {a_dev['token']}"}))["attempt"]
    b_acc = create_account(other, name="环境-DEMO-B", amazon_customer_id="A1DEMO000099")
    b_dev = pair_device(other, plugin, name="Mac-DEMO-B", account=b_acc)
    hb = {"Authorization": f"Bearer {b_dev['token']}"}
    err(plugin.post(f"/plugin/v1/runs/{run['run_id']}/heartbeat", headers=hb), 404, "NOT_FOUND")
    err(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=hb), 404, "NOT_FOUND")
    err(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish", json={}, headers=hb), 404, "NOT_FOUND")
    err(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/progress", json={"stage": "x"}, headers=hb),
        404, "NOT_FOUND")
    err(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                    json={"outcome": "failed", "error_code": "OUT_OF_STOCK"}, headers=hb), 404, "NOT_FOUND")


def test_workspace_b_cannot_run_any_order_action_or_filter_on_workspace_a(owner, other, api):
    """D3-10：其余人工动作、批量人审里夹带别人的 id、账号归档、按对象过滤事件、写回目标、清除提醒——全部 404 / 看不到。"""
    acc = create_account(owner)
    import_orders(owner, [import_row("PO-DEMO-W0201"), import_row("PO-DEMO-W0202", 人工审核=None)])
    order = find_order(owner, "PO-DEMO-W0201")
    pending = find_order(owner, "PO-DEMO-W0202")
    oid = order["id"]
    with tx() as conn:
        conn.execute("UPDATE procure.purchase_orders SET needs_attention = true, attention_note = 'DEMO' "
                     "WHERE id = %s", (oid,))
        src = conn.execute("SELECT id FROM procure.order_sources WHERE purchase_order_id = %s", (oid,)).fetchone()["id"]
    for method, path, body in [
        ("POST", f"/api/v1/orders/{oid}/resume", {}),
        ("POST", f"/api/v1/orders/{oid}/uncancel", {}),
        ("POST", f"/api/v1/orders/{oid}/resolve-unknown", {"outcome": "not_ordered"}),
        ("POST", f"/api/v1/orders/{oid}/mark-amazon-cancelled", {}),
        ("POST", f"/api/v1/orders/{oid}/repurchase", {"decision": "approve"}),
        ("POST", f"/api/v1/orders/{oid}/resolve-conflict", {"choice": "keep"}),
        ("POST", f"/api/v1/orders/{oid}/clear-attention", {}),
        ("POST", f"/api/v1/orders/{oid}/writeback-target", {"source_id": src}),
        ("POST", f"/api/v1/accounts/{acc['id']}/archive", {}),
    ]:
        err(other.request(method, path, json=body), 404, "NOT_FOUND")
    out = ok(other.post("/api/v1/orders/bulk-review", json={"ids": [pending["id"]], "decision": "approve"}))
    assert out["updated"] == [] and out["skipped"][0]["id"] == pending["id"]
    for params in ({"entity": "order", "entity_id": oid}, {"entity": "account", "entity_id": acc["id"]},
                   {"order_id": oid}):
        assert ok(other.get("/api/v1/events", params=params))["total"] == 0
    assert ok(other.get("/api/v1/orders", params={"needs_attention": "true"}))["total"] == 0
    # A 的数据原封不动
    a = ok(owner.get(f"/api/v1/orders/{oid}"))
    assert a["needs_attention"] is True and a["status"] == "assigned"
    assert ok(owner.get(f"/api/v1/orders/{pending['id']}"))["status"] == "pending_review"
    assert ok(owner.get(f"/api/v1/accounts/{acc['id']}"))["archived_at"] is None
