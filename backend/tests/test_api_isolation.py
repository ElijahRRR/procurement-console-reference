"""HTTP：跨工作区一律 404（v1 §10「甲尝试访问乙的任务」、方案 v2 §4「跨工作区拒绝访问」）——工作区 B 的用户读不到、
也动不了工作区 A 的任务、账号、浏览器、待登记的买家号、飞书连接、导入批次、模板与事件；列表里也看不到。
浏览器登记时归属一个工作区，买家号 ID 只在它所属的工作区里找账号。全部数据虚构。"""

from __future__ import annotations

from tests import fake_feishu as ff
from tests.api_helpers import (api, bearer, create_account, err, find_order, good_facts, identity,  # noqa: F401
                               import_orders, import_row, ok, other, owner, plugin_client, register_device, run_body,
                               tx, upload, xlsx_bytes)


def _ws_id(client) -> int:
    return ok(client.get("/api/v1/auth/me"))["workspace"]["id"]


def test_workspace_b_cannot_read_or_act_on_workspace_a(owner, other, api):
    acc = create_account(owner)
    imp = import_orders(owner, [import_row("PO-DEMO-W0001")])
    order = find_order(owner, "PO-DEMO-W0001")
    a_ws, b_ws = _ws_id(owner), _ws_id(other)
    assert a_ws != b_ws
    plugin = plugin_client(api)
    # 系统里有两个工作区：登记必须带工作区 id
    dev = register_device(plugin, acc, workspace_id=a_ws)
    stray = register_device(plugin, customer_id="A1DEMO000555", workspace_id=a_ws)      # A 里一个待登记的买家号
    pending = upload(owner, xlsx_bytes([import_row("PO-DEMO-W0002")]))
    tpl = ok(owner.post("/api/v1/import-templates", json={"name": "A 的模板", "mapping": {"单号": "upstream_order_no"}}))
    client = ff.FakeFeishuClient.with_order_table()
    with tx() as conn:
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
        ("POST", f"/api/v1/devices/{did}/remove", {}),
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
    b_acc = create_account(other, name="环境-DEMO-B", amazon_customer_id="A1DEMO000002")
    err(other.post(f"/api/v1/orders/{oid}/assign", json={"account_id": b_acc["id"]}), 404, "NOT_FOUND")
    # B 的浏览器登录着 A 已登记的买家号：买家号 ID 只在浏览器所属的工作区里找账号 → 在 B 里它是「未登记」，
    # 拿不到 A 的账号配置，也开不了 A 的批次
    b_dev = register_device(plugin, customer_id=acc["amazon_customer_id"], workspace_id=b_ws)
    assert b_dev["register"]["workspace_id"] == b_ws and b_dev["register"]["workspace_name"] == "DEMO 工作区 B"
    assert b_dev["heartbeat"]["login"]["state"] == "unregistered" and b_dev["heartbeat"]["account"] is None
    body = err(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=bearer(b_dev["token"])),
               403, "ACCOUNT_NOT_REGISTERED")
    assert acc["name"] not in str(body)
    # 待登记的买家号、忽略名单都按工作区分开
    assert [i["customer_id"] for i in ok(other.get("/api/v1/accounts/discovered"))["items"]] == \
        [acc["amazon_customer_id"]]
    assert [i["customer_id"] for i in ok(owner.get("/api/v1/accounts/discovered"))["items"]] == ["A1DEMO000555"]
    ok(other.post("/api/v1/accounts/discovered/ignore", json={"customer_id": "A1DEMO000555"}))
    assert [i["customer_id"] for i in ok(owner.get("/api/v1/accounts/discovered"))["items"]] == ["A1DEMO000555"]
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=bearer(stray["token"])))
    assert hb["login"]["state"] == "unregistered"                    # B 的忽略不影响 A 的浏览器
    # 同一个买家号 ID 可以分别登记在两个工作区；B 登记后只解析到 B 自己的账号
    b_same = create_account(other, name="环境-DEMO-B-同号", amazon_customer_id=acc["amazon_customer_id"])
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=bearer(b_dev["token"])))
    assert hb["login"]["state"] == "ready" and hb["account"]["id"] == b_same["id"]
    assert [b["id"] for b in ok(owner.get(f"/api/v1/accounts/{aid}"))["browsers"]] == [did]
    assert [b["id"] for b in ok(other.get(f"/api/v1/accounts/{b_same['id']}"))["browsers"]] == [b_dev["device_id"]]

    # 列表只有自己的
    assert ok(other.get("/api/v1/orders"))["total"] == 0
    assert sorted(a["id"] for a in ok(other.get("/api/v1/accounts"))["rows"]) == sorted([b_acc["id"], b_same["id"]])
    assert [d["id"] for d in ok(other.get("/api/v1/devices", params={"include_hidden": 1}))["rows"]] == \
        [b_dev["device_id"]]
    assert sorted(d["id"] for d in ok(owner.get("/api/v1/devices"))["rows"]) == sorted([did, stray["device_id"]])
    assert ok(other.get("/api/v1/overview"))["devices"]["total"] == 1
    assert ok(other.get("/api/v1/events", params={"entity": "device", "entity_id": did}))["total"] == 0
    assert ok(other.get("/api/v1/feishu/connections"))["total"] == 0
    assert ok(other.get("/api/v1/imports"))["total"] == 0
    assert ok(other.get("/api/v1/import-templates"))["total"] == 0
    assert ok(other.get("/api/v1/events", params={"order_id": oid}))["total"] == 0
    assert ok(other.get("/api/v1/overview"))["counts"]["assigned"] == 0

    # A 的数据原封不动
    assert ok(owner.get(f"/api/v1/orders/{oid}"))["status"] == "assigned"
    assert ok(owner.get(f"/api/v1/accounts/{aid}"))["execution_paused"] is False
    a_dev = ok(owner.get(f"/api/v1/devices/{did}"))
    assert a_dev["hidden_at"] is None and a_dev["name"] == f"浏览器 {did}"
    assert a_dev["login"]["state"] == "ready" and a_dev["account"] == {"id": aid, "name": acc["name"]}
    assert ok(owner.get(f"/api/v1/feishu/connections/{cid}"))["enabled"] is True


def test_plugin_of_workspace_b_cannot_touch_runs_or_attempts_of_workspace_a(owner, other, api):
    acc = create_account(owner)
    import_orders(owner, [import_row("PO-DEMO-W0101")])
    plugin = plugin_client(api)
    a_dev = register_device(plugin, acc, workspace_id=_ws_id(owner))
    ha = bearer(a_dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=ha))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=ha))["attempt"]
    b_acc = create_account(other, name="环境-DEMO-B", amazon_customer_id="A1DEMO000099")
    b_dev = register_device(plugin, b_acc, workspace_id=_ws_id(other))
    hb = bearer(b_dev["token"])
    aid = att["attempt_id"]
    for path, body in [
        (f"/plugin/v1/runs/{run['run_id']}/heartbeat", None),
        (f"/plugin/v1/runs/{run['run_id']}/next", None),
        (f"/plugin/v1/runs/{run['run_id']}/finish", {}),
        (f"/plugin/v1/attempts/{aid}/progress", {"stage": "x"}),
        (f"/plugin/v1/attempts/{aid}/guard", good_facts(att)),
        (f"/plugin/v1/attempts/{aid}/submitting", None),
        (f"/plugin/v1/attempts/{aid}/verifying", {"note": "越权"}),
        (f"/plugin/v1/attempts/{aid}/result", {"outcome": "failed", "error_code": "OUT_OF_STOCK"}),
    ]:
        err(plugin.post(path, json=body, headers=hb), 404, "NOT_FOUND")
    # A 的批次与尝试原封不动，照常继续
    pr = ok(plugin.post(f"/plugin/v1/attempts/{aid}/progress", json={"stage": "checkout"}, headers=ha))
    assert pr["continue"] is True
    assert find_order(owner, "PO-DEMO-W0101")["status"] == "executing"
    # 同一个工作区里的另一个浏览器同样碰不到别的浏览器的批次
    a_dev2 = register_device(plugin, acc, workspace_id=_ws_id(owner))
    err(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=bearer(a_dev2["token"])), 404, "NOT_FOUND")
    err(plugin.post(f"/plugin/v1/attempts/{aid}/result", json={"outcome": "unknown"},
                    headers=bearer(a_dev2["token"])), 404, "NOT_FOUND")


def test_registration_picks_the_workspace_once_and_never_moves(owner, other, api):
    """系统里有多个工作区：登记必须带存在的工作区 id（否则 409 WORKSPACE_REQUIRED）；登记后工作区不再变。"""
    a_ws, b_ws = _ws_id(owner), _ws_id(other)
    plugin = plugin_client(api)
    token = "DEMO-device-token-isolation".ljust(43, "x")
    for bad in ({}, {"workspace_id": 999999}, {"workspace_id": "abc"}, {"workspace_id": None}, {"workspace_id": True}):
        body = err(plugin.post("/plugin/v1/register", json={"device_token": token, "ext_version": "0.2.0", **bad}),
                   409, "WORKSPACE_REQUIRED")
        assert body["message"] == "系统里有多个工作区：请在插件设置的「工作区 ID」里填写（控制台「设置」页可以查到）"
    assert ok(owner.get("/api/v1/devices"))["total"] == 0 and ok(other.get("/api/v1/devices"))["total"] == 0
    reg = ok(plugin.post("/plugin/v1/register", json={"device_token": token, "ext_version": "0.2.0",
                                                      "workspace_id": b_ws}))
    assert reg["workspace_id"] == b_ws and reg["workspace_name"] == "DEMO 工作区 B" and reg["created"] is True
    # 同一个令牌再登记：幂等，不看 workspace_id（不会被搬到 A）
    again = ok(plugin.post("/plugin/v1/register", json={"device_token": token, "ext_version": "0.2.0",
                                                        "workspace_id": a_ws}))
    assert again == {**reg, "created": False}
    assert ok(plugin.post("/plugin/v1/register", json={"device_token": token})) == {**reg, "created": False}
    assert ok(owner.get("/api/v1/devices"))["total"] == 0
    assert [d["id"] for d in ok(other.get("/api/v1/devices"))["rows"]] == [reg["device_id"]]
    err(owner.get(f"/api/v1/devices/{reg['device_id']}"), 404, "NOT_FOUND")


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
