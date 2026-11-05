"""HTTP：买家账号增改查、暂停 / 恢复执行、当天额度、归档；设备配对码、改名与绑定、撤销；全局设置的校验；
重新分配与最近一次摘要；概览；清除模拟结果（无数据时）。全部数据虚构。"""

from __future__ import annotations

from tests.api_helpers import (api, create_account, err, import_orders, import_row, ok, owner,  # noqa: F401
                               pair_device, plugin_client)


def test_accounts_crud_pause_resume_and_quota_today(owner):
    acc = create_account(owner, name="环境-DEMO-A", min_amount="20", max_amount="50", daily_limit=3)
    assert acc["min_amount"] == "20.00" and acc["max_amount"] == "50.00" and acc["daily_limit"] == 3
    assert acc["quota_today"] == {"limit": 3, "used": 0, "planned": 0, "available": 3}
    assert acc["effective_max_delivery_days"] == 10 and acc["devices"] == [] and acc["active_run"] is None

    import_orders(owner, [import_row("PO-DEMO-A0001"), import_row("PO-DEMO-A0002")])
    got = ok(owner.get(f"/api/v1/accounts/{acc['id']}"))
    assert got["quota_today"] == {"limit": 3, "used": 0, "planned": 2, "available": 1}

    # 前端整份回传（含只读键）也能改：只读键被忽略
    patched = ok(owner.patch(f"/api/v1/accounts/{acc['id']}", json={**got, "daily_limit": 5, "max_delivery_days": 7}))
    assert patched["daily_limit"] == 5 and patched["effective_max_delivery_days"] == 7
    assert patched["quota_today"]["limit"] == 5

    paused = ok(owner.post(f"/api/v1/accounts/{acc['id']}/pause", json={"reason": "DEMO 暂停"}))
    assert paused["execution_paused"] is True and paused["pause_reason"] == "DEMO 暂停" and paused["paused_at"]
    resumed = ok(owner.post(f"/api/v1/accounts/{acc['id']}/resume"))
    assert resumed["execution_paused"] is False and resumed["pause_reason"] is None

    listing = ok(owner.get("/api/v1/accounts"))
    assert listing["total"] == 1 and listing["rows"][0]["id"] == acc["id"]

    archived = ok(owner.post(f"/api/v1/accounts/{acc['id']}/archive"))
    assert archived["archived_at"] is not None
    assert ok(owner.get("/api/v1/accounts"))["total"] == 0
    assert ok(owner.get("/api/v1/accounts", params={"include_archived": True}))["total"] == 1


def test_account_validation_errors(owner):
    err(owner.post("/api/v1/accounts", json={"name": "环境-DEMO-X", "min_amount": "60", "max_amount": "50"}),
        422, "VALIDATION")
    err(owner.post("/api/v1/accounts", json={"min_amount": "10"}), 422, "VALIDATION")          # 缺名称
    err(owner.post("/api/v1/accounts", json={"name": "环境-DEMO-X", "unknown_key": 1}), 422, "VALIDATION")
    body = err(owner.post("/api/v1/accounts", json={"name": "环境-DEMO-X", "amazon_customer_id": "bad"}),
               422, "VALIDATION")
    assert body["detail"]["field"] == "amazon_customer_id"
    acc = create_account(owner)
    err(owner.post("/api/v1/accounts", json={"name": acc["name"]}), 422, "VALIDATION")          # 重名
    err(owner.patch(f"/api/v1/accounts/{acc['id']}", json={"daily_limit": -1}), 422, "VALIDATION")
    err(owner.patch("/api/v1/accounts/999999", json={"daily_limit": 1}), 404, "NOT_FOUND")
    err(owner.get("/api/v1/accounts/999999"), 404, "NOT_FOUND")


def test_device_pairing_rename_bind_and_revoke(owner, api):
    acc = create_account(owner)
    plugin = plugin_client(api)
    code = ok(owner.post("/api/v1/devices/pairing-codes"))
    assert len(code["code"]) == 8 and code["expires_at"]
    paired = ok(plugin.post("/plugin/v1/pair", json={"code": code["code"], "device_name": "Mac-AdsPower-DEMO-1",
                                                     "browser": "adspower", "ext_version": "0.1.0"}))
    assert paired["workspace_name"] == "DEMO 工作区 A" and paired["device_token"]
    err(plugin.post("/plugin/v1/pair", json={"code": code["code"], "device_name": "again", "browser": "chrome",
                                             "ext_version": "0.1.0"}), 422, "VALIDATION")    # 配对码一次性

    devs = ok(owner.get("/api/v1/devices"))
    assert devs["total"] == 1
    dev = devs["rows"][0]
    assert dev["id"] == paired["device_id"] and dev["online"] is True and dev["bound_account"] is None

    d = ok(owner.patch(f"/api/v1/devices/{dev['id']}", json={"name": "Mac-AdsPower-DEMO-改名"}))
    assert d["name"] == "Mac-AdsPower-DEMO-改名" and d["bound_account"] is None
    d = ok(owner.patch(f"/api/v1/devices/{dev['id']}", json={"bound_account_id": acc["id"]}))
    assert d["bound_account"] == {"id": acc["id"], "name": acc["name"]}
    assert ok(owner.get(f"/api/v1/accounts/{acc['id']}"))["devices"][0]["id"] == dev["id"]
    d = ok(owner.patch(f"/api/v1/devices/{dev['id']}", json={"bound_account_id": None}))
    assert d["bound_account"] is None
    err(owner.patch(f"/api/v1/devices/{dev['id']}", json={"bound_account_id": 999999}), 422, "VALIDATION")

    revoked = ok(owner.post(f"/api/v1/devices/{dev['id']}/revoke"))
    assert revoked["revoked_at"] is not None and revoked["online"] is False
    err(plugin.post("/plugin/v1/heartbeat", json={}, headers={"Authorization": f"Bearer {paired['device_token']}"}),
        401, "UNAUTHORIZED")


def test_adopt_customer_id_from_device(owner, api):
    acc = create_account(owner, amazon_customer_id=None)
    plugin = plugin_client(api)
    dev = pair_device(owner, plugin, account=acc, report_identity=False)
    from tests.api_helpers import bearer, identity

    hb = ok(plugin.post("/plugin/v1/heartbeat", json={"identity": identity("A1DEMO777777")},
                        headers=bearer(dev["token"])))
    assert hb["identity_status"] == "unknown"                         # 账号还没登记买家号 ID
    adopted = ok(owner.post(f"/api/v1/accounts/{acc['id']}/adopt-customer-id", json={"device_id": dev["device_id"]}))
    assert adopted["amazon_customer_id"] == "A1DEMO777777"
    assert ok(owner.get(f"/api/v1/devices/{dev['device_id']}"))["identity_status"] == "match"
    err(owner.post(f"/api/v1/accounts/{acc['id']}/adopt-customer-id", json={"device_id": 999999}), 404, "NOT_FOUND")


def test_settings_get_and_patch_validation(owner):
    s = ok(owner.get("/api/v1/settings"))
    assert s == {"default_max_delivery_days": 10, "simulation_only": True, "logistics_sync_time": "08:30",
                 "real_purchase_available": False}
    s = ok(owner.patch("/api/v1/settings", json={"default_max_delivery_days": 12, "logistics_sync_time": "09:15",
                                                 "real_purchase_available": True}))
    assert s["default_max_delivery_days"] == 12 and s["logistics_sync_time"] == "09:15"
    assert s["real_purchase_available"] is False                        # 只读键被忽略
    for bad in ({"default_max_delivery_days": 0}, {"default_max_delivery_days": 61},
                {"default_max_delivery_days": "12"}, {"default_max_delivery_days": True},
                {"simulation_only": False}, {"logistics_sync_time": "25:00"}, {"unknown_setting": 1}):
        err(owner.patch("/api/v1/settings", json=bad), 422, "VALIDATION")
    err(owner.patch("/api/v1/settings", json=[1, 2]), 422, "VALIDATION")
    assert ok(owner.get("/api/v1/settings"))["default_max_delivery_days"] == 12   # 失败的修改不生效


def test_allocation_run_last_and_overview(owner):
    assert ok(owner.get("/api/v1/allocation/last")) is None
    create_account(owner, name="环境-DEMO-A", min_amount="20", max_amount="30")
    create_account(owner, name="环境-DEMO-B", amazon_customer_id="A1DEMO000002", min_amount="20", max_amount="50")
    import_orders(owner, [import_row("PO-DEMO-L0025", 采购单价=25), import_row("PO-DEMO-L0045", 采购单价=45, 限价=60)])
    run = ok(owner.post("/api/v1/allocation/run"))
    assert run["trigger"] == "manual" and run["bj_date"] == "2026-10-31"
    assert set(run["stats"]) >= {"considered", "assigned", "coverage", "fill", "unassigned"}
    assert ok(owner.get("/api/v1/allocation/last"))["id"] == run["id"]

    rows = {r["upstream_order_no"]: r for r in ok(owner.get("/api/v1/orders", params={"view": "today"}))["rows"]}
    assert rows["PO-DEMO-L0025"]["assigned_account"]["name"] == "环境-DEMO-A"     # A 只接 20–30
    assert rows["PO-DEMO-L0045"]["assigned_account"]["name"] == "环境-DEMO-B"

    ov = ok(owner.get("/api/v1/overview"))
    assert ov["bj_date"] == "2026-10-31" and ov["today_executable"] == 2 and ov["counts"]["assigned"] == 2
    assert ov["accounts"] == {"total": 2, "enabled": 2, "paused": 0} and ov["devices"] == {"total": 0, "online": 0}
    assert ov["simulation_only"] is True and ov["last_allocation"]["id"] == run["id"]
    assert ov["feishu_configured"] in (True, False)


def test_simulation_reset_with_nothing_to_reset(owner):
    out = ok(owner.post("/api/v1/simulation/reset"))
    assert out == {"runs_ended": [], "attempts_reset": 0, "orders_restored": [], "orders_skipped": [],
                   "accounts_resumed": []}


def test_writeback_queue_is_empty_without_feishu(owner):
    assert ok(owner.get("/api/v1/writebacks")) == {"total": 0, "rows": []}
    err(owner.get("/api/v1/writebacks", params={"status": "nope"}), 422, "VALIDATION")
    err(owner.post("/api/v1/writebacks/999999/retry"), 404, "NOT_FOUND")
