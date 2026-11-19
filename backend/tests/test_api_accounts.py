"""HTTP：买家账号增改查、暂停 / 恢复执行、当天额度、归档；浏览器列表、改名、移除与自动回到列表；待登记的买家号
（列表、登记、填到已有账号、忽略、恢复）；全局设置的校验；重新分配与最近一次摘要；概览；清除模拟结果（无数据时）。
全部数据虚构。"""

from __future__ import annotations

import json

import pytest

from registry import db

from tests.api_helpers import (IMPORT_HEADERS, api, bearer, create_account, err, find_order,  # noqa: F401
                               identity, import_orders, import_row, ok, owner, plugin_client, register_device,
                               run_body, xlsx_bytes)

DEVICE_KEYS = {"id", "name", "ext_version", "online", "last_seen_at", "created_at", "hidden_at", "login", "account",
               "active_run"}
DISCOVERED_KEYS = {"customer_id", "ignored", "browsers", "last_reported_at", "name_hint", "source_orders"}


def test_accounts_crud_pause_resume_and_quota_today(owner):
    acc = create_account(owner, name="环境-DEMO-A", min_amount="20", max_amount="50", daily_limit=3)
    assert acc["min_amount"] == "20.00" and acc["max_amount"] == "50.00" and acc["daily_limit"] == 3
    assert acc["quota_today"] == {"limit": 3, "used": 0, "planned": 0, "available": 3}
    assert acc["effective_max_delivery_days"] == 10 and acc["browsers"] == [] and acc["active_run"] is None
    assert "devices" not in acc and "default_device_id" not in acc

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


@pytest.mark.parametrize("bad", ["a\x00b", "a\ud800b", "a\nb"])
def test_account_name_with_control_or_unstorable_characters_is_422_not_500(owner, bad):
    """账号名称含 NUL、不成对的代理项或换行：新建与修改都答 422（field = name），什么也不写；备注可以换行，
    但同样不收存不了的字符。请求体按纯 ASCII 的 JSON 发（httpx 的 json= 编码不了代理项）。"""
    headers = {"Content-Type": "application/json"}
    body = json.dumps({"name": bad}).encode("ascii")
    message = "账号名称不能包含换行、制表符等控制字符" if bad == "a\nb" else "账号名称不能包含无法保存的字符"
    e = err(owner.post("/api/v1/accounts", content=body, headers=headers), 422, "VALIDATION")
    assert e["detail"]["field"] == "name" and e["message"] == message
    assert ok(owner.get("/api/v1/accounts"))["total"] == 0

    acc = create_account(owner, name="环境-DEMO-A")
    e = err(owner.patch(f"/api/v1/accounts/{acc['id']}", content=body, headers=headers), 422, "VALIDATION")
    assert e["detail"]["field"] == "name" and e["message"] == message
    assert ok(owner.get(f"/api/v1/accounts/{acc['id']}"))["name"] == "环境-DEMO-A"

    if bad != "a\nb":
        notes = json.dumps({"notes": bad}).encode("ascii")
        e = err(owner.patch(f"/api/v1/accounts/{acc['id']}", content=notes, headers=headers), 422, "VALIDATION")
        assert e["detail"]["field"] == "notes"
    else:
        assert ok(owner.patch(f"/api/v1/accounts/{acc['id']}", json={"notes": "第一行\n第二行"}))["notes"] == "第一行\n第二行"


def test_legacy_account_name_with_a_tab_does_not_block_other_edits(owner, api, clean_db):
    """规则之前存下的账号名称带制表符：控制台整份回传（名称原样）时照常保存别的字段；改成另一个带制表符的名称才 422。"""
    acc = create_account(owner, name="环境-DEMO-A")
    with db.pg_conn(clean_db) as c:
        c.execute("UPDATE procure.buyer_accounts SET name = %s WHERE id = %s", ("环境\tDEMO-A", acc["id"]))
    got = ok(owner.get(f"/api/v1/accounts/{acc['id']}"))
    patched = ok(owner.patch(f"/api/v1/accounts/{acc['id']}", json={**got, "priority": 5}))
    assert patched["priority"] == 5 and patched["name"] == "环境\tDEMO-A"
    e = err(owner.patch(f"/api/v1/accounts/{acc['id']}", json={"name": "环境\tDEMO-B"}), 422, "VALIDATION")
    assert e["detail"]["field"] == "name"


def test_browser_list_rename_remove_and_auto_restore(owner, api):
    """浏览器由插件自动登记：控制台只能看、改名、移除；移除不动令牌，它再次联系时自动回到列表。"""
    acc = create_account(owner)
    plugin = plugin_client(api)
    assert ok(owner.get("/api/v1/devices")) == {"total": 0, "rows": []}
    dev = register_device(plugin, acc)
    h = bearer(dev["token"])

    devs = ok(owner.get("/api/v1/devices"))
    assert devs["total"] == 1
    row = devs["rows"][0]
    assert set(row) == DEVICE_KEYS and row["id"] == dev["device_id"] and row["name"] == f"浏览器 {row['id']}"
    assert row["online"] is True and row["ext_version"] == "0.2.0" and row["hidden_at"] is None
    assert row["last_seen_at"] and row["created_at"]
    assert set(row["login"]) == {"state", "customer_id", "at", "stale"}
    assert row["login"]["state"] == "ready" and row["login"]["customer_id"] == acc["amazon_customer_id"]
    assert row["login"]["stale"] is False and row["login"]["at"]
    assert row["account"] == {"id": acc["id"], "name": acc["name"]} and row["active_run"] is None
    assert ok(owner.get(f"/api/v1/devices/{row['id']}")) == row
    assert not {"bound_account", "bound_account_id", "identity_status", "revoked_at", "browser"} & set(row)

    d = ok(owner.patch(f"/api/v1/devices/{row['id']}", json={"name": " AdsPower-DEMO-环境 1 "}))
    assert d["name"] == "AdsPower-DEMO-环境 1"
    for junk in ({"bound_account_id": acc["id"]}, {"name": "x", "bound_account_id": None}, {"hidden_at": None}):
        body = err(owner.patch(f"/api/v1/devices/{row['id']}", json=junk), 422, "VALIDATION")   # 只能改名
        assert body["detail"]["field"] in junk
    d = ok(owner.patch(f"/api/v1/devices/{row['id']}", json={}))                              # 什么都没传：原样返回
    assert d["name"] == "AdsPower-DEMO-环境 1" and d["account"] == {"id": acc["id"], "name": acc["name"]}
    for bad in ("", "   ", "长" * 65, None, 12):
        body = err(owner.patch(f"/api/v1/devices/{row['id']}", json={"name": bad}), 422, "VALIDATION")
        assert body["detail"]["field"] == "name"
    browsers = ok(owner.get(f"/api/v1/accounts/{acc['id']}"))["browsers"]
    assert [set(b) for b in browsers] == [{"id", "name", "online", "last_seen_at"}]
    assert browsers[0]["id"] == row["id"] and browsers[0]["name"] == "AdsPower-DEMO-环境 1" and browsers[0]["online"]

    # 正在执行的浏览器不能移除
    run = ok(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=h))
    d = ok(owner.get(f"/api/v1/devices/{row['id']}"))
    assert set(d["active_run"]) == {"id", "account_id", "started_at", "simulated"}
    assert d["active_run"]["id"] == run["run_id"] and d["active_run"]["account_id"] == acc["id"]
    assert d["active_run"]["simulated"] is True
    body = err(owner.post(f"/api/v1/devices/{row['id']}/remove"), 409, "CONFLICT")
    assert body["message"] == "这个浏览器正在执行，结束后才能移除" and body["detail"]["run_id"] == run["run_id"]
    ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish", json={"reason": "finished"}, headers=h))

    removed = ok(owner.post(f"/api/v1/devices/{row['id']}/remove"))
    assert removed["id"] == row["id"] and removed["hidden_at"] is not None
    assert ok(owner.post(f"/api/v1/devices/{row['id']}/remove"))["hidden_at"] == removed["hidden_at"]   # 幂等
    assert ok(owner.get("/api/v1/devices")) == {"total": 0, "rows": []}
    assert ok(owner.get("/api/v1/devices", params={"include_hidden": 0}))["total"] == 0
    hidden = ok(owner.get("/api/v1/devices", params={"include_hidden": 1}))
    assert hidden["total"] == 1 and hidden["rows"][0]["hidden_at"] == removed["hidden_at"]
    assert ok(owner.get(f"/api/v1/devices/{row['id']}"))["hidden_at"] is not None            # 详情照样读得到
    assert ok(owner.get(f"/api/v1/accounts/{acc['id']}"))["browsers"] == []
    assert ok(owner.get("/api/v1/overview"))["devices"] == {"total": 0, "online": 0}
    kinds = [e["kind"] for e in ok(owner.get("/api/v1/events", params={"entity": "device", "entity_id": row["id"]}))["rows"]]
    assert "device.registered" in kinds and "device.removed" in kinds and "device.updated" in kinds

    # 移除不动令牌：这个浏览器的插件再次联系（心跳 / 身份上报 / 登记）就回到列表
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=h))
    assert hb["device"] == {"id": row["id"], "name": "AdsPower-DEMO-环境 1"} and hb["login"]["state"] == "ready"
    back = ok(owner.get("/api/v1/devices"))
    assert back["total"] == 1 and back["rows"][0]["hidden_at"] is None
    assert ok(owner.get("/api/v1/overview"))["devices"] == {"total": 1, "online": 1}
    ok(owner.post(f"/api/v1/devices/{row['id']}/remove"))
    again = ok(plugin.post("/plugin/v1/register", json={"device_token": dev["token"], "ext_version": "0.2.0"}))
    assert again["device_id"] == row["id"] and again["created"] is False
    assert ok(owner.get("/api/v1/devices"))["total"] == 1

    err(owner.get("/api/v1/devices/999999"), 404, "NOT_FOUND")
    err(owner.patch("/api/v1/devices/999999", json={"name": "x"}), 404, "NOT_FOUND")
    err(owner.post("/api/v1/devices/999999/remove"), 404, "NOT_FOUND")
    err(owner.get("/api/v1/devices", params={"include_hidden": "maybe"}), 422, "VALIDATION")


def test_pairing_binding_and_adopt_routes_are_gone(owner, api):
    """配对码、撤销、绑定、采用 ID 的接口都已删除（不是换了名字）。"""
    acc = create_account(owner)
    dev = register_device(plugin_client(api), acc)
    assert owner.post("/api/v1/devices/pairing-codes").status_code in (404, 405)
    err(owner.post(f"/api/v1/devices/{dev['device_id']}/revoke"), 404, "NOT_FOUND")
    err(owner.post(f"/api/v1/accounts/{acc['id']}/adopt-customer-id", json={"device_id": dev["device_id"]}),
        404, "NOT_FOUND")
    body = err(owner.patch(f"/api/v1/accounts/{acc['id']}", json={"default_device_id": dev["device_id"]}),
               422, "VALIDATION")
    assert "default_device_id" in body["message"]
    paths = set(ok(owner.get("/api/v1/openapi.json"))["paths"])
    assert not any(word in p for p in paths for word in ("pair", "revoke", "adopt"))
    assert {"/plugin/v1/register", "/api/v1/devices/{device_id}/remove", "/api/v1/accounts/discovered",
            "/api/v1/accounts/discovered/ignore", "/api/v1/accounts/discovered/unignore"} <= paths


def test_discovered_buyers_register_fill_ignore_and_restore(owner, api):
    """插件读到、还没登记的买家号：列在待登记里；「登记」= 新建账号带 ID，「填到已有账号」= 改账号的 ID；可以忽略与恢复。"""
    plugin = plugin_client(api)
    assert ok(owner.get("/api/v1/accounts/discovered")) == {"items": []}         # 不会被当成账号 id
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 0
    d1 = register_device(plugin, customer_id="A1DEMO777777")
    d2 = register_device(plugin, customer_id="a1demo777777 ")                     # 同一个买家号登录在两个浏览器里
    d3 = register_device(plugin, customer_id="A1DEMO888888")
    register_device(plugin, customer_id="not-an-id")                             # 形状不对：当作没读到，不报错
    register_device(plugin)                                                      # 没上报
    assert d1["heartbeat"]["login"] == {**d1["heartbeat"]["login"], "state": "unregistered",
                                        "customer_id": "A1DEMO777777"}

    items = ok(owner.get("/api/v1/accounts/discovered"))["items"]
    assert [set(i) for i in items] == [DISCOVERED_KEYS, DISCOVERED_KEYS]
    by_id = {i["customer_id"]: i for i in items}
    assert set(by_id) == {"A1DEMO777777", "A1DEMO888888"}
    seven = by_id["A1DEMO777777"]
    assert seven["ignored"] is False and seven["name_hint"] is None and seven["last_reported_at"]
    assert seven["source_orders"] == 0 and by_id["A1DEMO888888"]["source_orders"] == 0     # 只来自浏览器
    assert sorted(b["id"] for b in seven["browsers"]) == sorted([d1["device_id"], d2["device_id"]])
    assert all(set(b) == {"id", "name", "online", "last_seen_at"} and b["online"] for b in seven["browsers"])
    assert [b["id"] for b in by_id["A1DEMO888888"]["browsers"]] == [d3["device_id"]]
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 2

    # 忽略 / 恢复（幂等）
    assert ok(owner.post("/api/v1/accounts/discovered/ignore", json={"customer_id": " a1demo888888"})) == {"ok": True}
    assert ok(owner.post("/api/v1/accounts/discovered/ignore", json={"customer_id": "A1DEMO888888"})) == {"ok": True}
    assert [i["customer_id"] for i in ok(owner.get("/api/v1/accounts/discovered"))["items"]] == ["A1DEMO777777"]
    assert ok(owner.get("/api/v1/accounts/discovered", params={"include_ignored": 0}))["items"] == \
        ok(owner.get("/api/v1/accounts/discovered"))["items"]
    with_ignored = {i["customer_id"]: i["ignored"]
                    for i in ok(owner.get("/api/v1/accounts/discovered", params={"include_ignored": 1}))["items"]}
    assert with_ignored == {"A1DEMO777777": False, "A1DEMO888888": True}
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 1
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=bearer(d3["token"])))
    assert hb["login"]["state"] == "ignored" and hb["account"] is None
    assert ok(owner.get(f"/api/v1/devices/{d3['device_id']}"))["login"]["state"] == "ignored"
    assert ok(owner.post("/api/v1/accounts/discovered/unignore", json={"customer_id": "A1DEMO888888"})) == {"ok": True}
    assert ok(owner.post("/api/v1/accounts/discovered/unignore", json={"customer_id": "A1DEMO888888"})) == {"ok": True}
    assert len(ok(owner.get("/api/v1/accounts/discovered"))["items"]) == 2
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=bearer(d3["token"])))
    assert hb["login"]["state"] == "unregistered"

    # 「登记」：新建账号时带上这个 ID —— 不需要对浏览器做任何操作
    acc = create_account(owner, name="环境-DEMO-七", amazon_customer_id="A1DEMO777777")
    assert sorted(b["id"] for b in acc["browsers"]) == sorted([d1["device_id"], d2["device_id"]])
    assert [i["customer_id"] for i in ok(owner.get("/api/v1/accounts/discovered"))["items"]] == ["A1DEMO888888"]
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=bearer(d1["token"])))
    assert hb["login"]["state"] == "ready" and hb["account"]["id"] == acc["id"]
    assert ok(owner.get(f"/api/v1/devices/{d2['device_id']}"))["account"] == {"id": acc["id"], "name": "环境-DEMO-七"}

    # 「填到已有账号」：给一个还没登记 ID 的账号填上（先忽略再填：忽略记录一并清掉）
    blank = create_account(owner, name="环境-DEMO-八", amazon_customer_id=None)
    assert blank["amazon_customer_id"] is None and blank["browsers"] == []
    ok(owner.post("/api/v1/accounts/discovered/ignore", json={"customer_id": "A1DEMO888888"}))
    filled = ok(owner.patch(f"/api/v1/accounts/{blank['id']}", json={"amazon_customer_id": "A1DEMO888888"}))
    assert filled["amazon_customer_id"] == "A1DEMO888888"
    assert [b["id"] for b in filled["browsers"]] == [d3["device_id"]]
    assert ok(owner.get("/api/v1/accounts/discovered", params={"include_ignored": 1})) == {"items": []}
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 0
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=bearer(d3["token"])))
    assert hb["login"]["state"] == "ready" and hb["account"]["name"] == "环境-DEMO-八"

    # 校验
    for bad in ({"customer_id": "bad"}, {"customer_id": None}, {}, {"customer_id": 12}):
        body = err(owner.post("/api/v1/accounts/discovered/ignore", json=bad), 422, "VALIDATION")
        assert body["detail"]["field"] == "customer_id"
        err(owner.post("/api/v1/accounts/discovered/unignore", json=bad), 422, "VALIDATION")
    body = err(owner.post("/api/v1/accounts/discovered/ignore", json={"customer_id": "A1DEMO777777"}),
               422, "VALIDATION")                                   # 已登记在账号上的 ID 不能忽略
    assert "环境-DEMO-七" in body["message"]
    err(owner.get("/api/v1/accounts/discovered", params={"include_ignored": "maybe"}), 422, "VALIDATION")
    kinds = [e["kind"] for e in ok(owner.get("/api/v1/events", params={"entity": "account", "page_size": 200}))["rows"]]
    assert kinds.count("account.buyer_id_ignored") == 2 and kinds.count("account.buyer_id_unignored") == 1


def test_ignore_and_unignore_refresh_the_unassigned_reason(owner):
    """忽略 / 恢复一个被来源指定的买家号 ID：待分配原因立即跟着变，不用再点「重新分配」；重复操作、没有任务指定的 ID
    不触发分配。"""
    cid = "A1DEMO999999"
    plain = f"来源指定的买家号 ID {cid} 还没有登记为账号；在「账号与浏览器」登记后会自动分配"
    tail = "（这个买家号 ID 已被忽略，恢复后才能登记）"
    create_account(owner, name="环境-DEMO-A")
    import_orders(owner, data=xlsx_bytes([import_row("PO-DEMO-IG01", 买家号ID=cid)],
                                         headers=[*IMPORT_HEADERS, "买家号ID"]))

    def reason() -> str:
        r = find_order(owner, "PO-DEMO-IG01")
        assert (r["status"], r["assigned_account"]) == ("queued", None)
        return r["status_reason"]

    def order_events() -> list[str]:
        rows = ok(owner.get("/api/v1/events", params={"entity": "order", "page_size": 200}))["rows"]
        return [e["kind"] for e in rows]

    assert reason() == plain
    before = order_events()
    first = ok(owner.get("/api/v1/allocation/last"))["id"]

    assert ok(owner.post("/api/v1/accounts/discovered/ignore", json={"customer_id": cid})) == {"ok": True}
    assert reason() == plain + tail
    last = ok(owner.get("/api/v1/allocation/last"))
    assert last["trigger"] == "account_change" and last["id"] != first
    assert [u["reason"] for u in last["unassigned"]] == [plain + tail]
    ok(owner.post("/api/v1/accounts/discovered/ignore", json={"customer_id": cid}))              # 重复：不分配
    ok(owner.post("/api/v1/accounts/discovered/ignore", json={"customer_id": "A1DEMO888888"}))   # 没有任务指定
    assert ok(owner.get("/api/v1/allocation/last"))["id"] == last["id"]

    assert ok(owner.post("/api/v1/accounts/discovered/unignore", json={"customer_id": cid})) == {"ok": True}
    assert reason() == plain
    again = ok(owner.get("/api/v1/allocation/last"))
    assert again["trigger"] == "account_change" and again["id"] != last["id"]
    ok(owner.post("/api/v1/accounts/discovered/unignore", json={"customer_id": cid}))            # 没忽略着：不分配
    assert ok(owner.get("/api/v1/allocation/last"))["id"] == again["id"]
    assert order_events() == before                                                            # 只改了原因


def test_new_console_routes_need_a_session_and_the_csrf_header(owner, api):
    """待登记买家号与浏览器的控制台接口和其他 /api 接口一样：要登录，写请求要防跨站请求头；插件令牌不能代替会话。"""
    from tests.api_helpers import console_client

    dev = register_device(plugin_client(api), customer_id="A1DEMO000321")
    anon = console_client(api)
    for method, path, body in [
        ("GET", "/api/v1/accounts/discovered", None),
        ("POST", "/api/v1/accounts/discovered/ignore", {"customer_id": "A1DEMO000321"}),
        ("POST", "/api/v1/accounts/discovered/unignore", {"customer_id": "A1DEMO000321"}),
        ("GET", "/api/v1/devices", None),
        ("GET", f"/api/v1/devices/{dev['device_id']}", None),
        ("PATCH", f"/api/v1/devices/{dev['device_id']}", {"name": "越权"}),
        ("POST", f"/api/v1/devices/{dev['device_id']}/remove", {}),
    ]:
        err(anon.request(method, path, json=body), 401, "UNAUTHORIZED")
        err(anon.request(method, path, json=body, headers=bearer(dev["token"])), 401, "UNAUTHORIZED")
    no_csrf = console_client(api, csrf=False)
    no_csrf.cookies.update(dict(owner.cookies))
    err(no_csrf.post("/api/v1/accounts/discovered/ignore", json={"customer_id": "A1DEMO000321"}), 403, "FORBIDDEN")
    err(no_csrf.post(f"/api/v1/devices/{dev['device_id']}/remove", json={}), 403, "FORBIDDEN")
    assert len(ok(no_csrf.get("/api/v1/accounts/discovered"))["items"]) == 1          # 读请求不需要防跨站头
    assert ok(owner.get(f"/api/v1/devices/{dev['device_id']}"))["hidden_at"] is None


def test_discovered_name_hint_comes_from_imported_source_data(owner, api):
    """来源数据里同一「买家号 ID」对应的「买家号」→ 待登记项的 name_hint（给「登记」对话框预填账号名）。
    两种来历都算：历史采购的行（带 AMZ 订单号，这两列只是「谁买的」，不是指定）与来源指定的行。"""
    headers = [*IMPORT_HEADERS, "买家号", "买家号ID", "AMZ订单号"]
    rows = [import_row("PO-DEMO-N0001", **{"买家号": "环境-DEMO-来源名", "买家号ID": "A1DEMO555555"}),
            import_row("PO-DEMO-N0002", **{"买家号": "环境-DEMO-历史名", "买家号ID": "A1DEMO444444",
                                           "AMZ订单号": "111-1234567-1234567"})]
    import_orders(owner, data=xlsx_bytes(rows, headers=headers))
    # 来源指定的那个不用等浏览器登录就在待登记里；历史采购的那个只是记录，不算指定
    items = ok(owner.get("/api/v1/accounts/discovered"))["items"]
    assert [(i["customer_id"], i["name_hint"], i["source_orders"], i["browsers"], i["last_reported_at"])
            for i in items] == [("A1DEMO555555", "环境-DEMO-来源名", 1, [], None)]
    plugin = plugin_client(api)
    register_device(plugin, customer_id="A1DEMO555555")
    register_device(plugin, customer_id="A1DEMO444444")
    by_id = {i["customer_id"]: i for i in ok(owner.get("/api/v1/accounts/discovered"))["items"]}
    assert {cid: (i["name_hint"], i["source_orders"], len(i["browsers"])) for cid, i in by_id.items()} == {
        "A1DEMO555555": ("环境-DEMO-来源名", 1, 1), "A1DEMO444444": ("环境-DEMO-历史名", 0, 1)}


def test_changing_customer_id_is_refused_while_the_account_is_executing(owner, api):
    acc = create_account(owner)
    import_orders(owner, [import_row("PO-DEMO-C0001")])
    plugin = plugin_client(api)
    dev = register_device(plugin, acc)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=h))
    for new_id in ("A1DEMO000002", None):
        body = err(owner.patch(f"/api/v1/accounts/{acc['id']}", json={"amazon_customer_id": new_id}), 409, "CONFLICT")
        assert body["message"] == "账号正在执行或有结果待核实的采购，处理完后才能修改买家号 ID"
    ok(owner.patch(f"/api/v1/accounts/{acc['id']}", json={"daily_limit": 3}))                 # 别的字段照常能改
    ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish", json={"reason": "finished"}, headers=h))
    changed = ok(owner.patch(f"/api/v1/accounts/{acc['id']}", json={"amazon_customer_id": "A1DEMO000002"}))
    assert changed["amazon_customer_id"] == "A1DEMO000002" and changed["browsers"] == []
    # 浏览器还登录着旧 ID：它成了待登记的买家号，用旧 ID 开不了批次
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=h))
    assert hb["login"]["state"] == "unregistered" and hb["account"] is None
    err(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=h), 403, "ACCOUNT_NOT_REGISTERED")
    assert [i["customer_id"] for i in ok(owner.get("/api/v1/accounts/discovered"))["items"]] == \
        [acc["amazon_customer_id"]]


def test_settings_get_and_patch_validation(owner):
    s = ok(owner.get("/api/v1/settings"))
    assert {k: v for k, v in s.items() if k != "file_admission"} == {
        "default_max_delivery_days": 10, "simulation_only": True, "logistics_sync_time": "08:30",
        "real_purchase_available": False}
    assert s["file_admission"]["cancel_after_sale_values"] == ["已取消"] and s["file_admission"]["import_historical"]
    s = ok(owner.patch("/api/v1/settings", json={"file_admission": {"purchaser_values": ["自采购DEMO"]}}))
    assert s["file_admission"]["purchaser_values"] == ["自采购DEMO"]
    err(owner.patch("/api/v1/settings", json={"file_admission": {"bogus": 1}}), 422, "VALIDATION")
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
    assert set(run["stats"]) == {"considered", "assigned", "coverage", "fill", "designated", "unassigned"}
    assert run["stats"]["designated"] == 0 and run["stats"]["assigned"] == 2
    assert ok(owner.get("/api/v1/allocation/last"))["id"] == run["id"]

    rows = {r["upstream_order_no"]: r for r in ok(owner.get("/api/v1/orders", params={"view": "today"}))["rows"]}
    assert rows["PO-DEMO-L0025"]["assigned_account"]["name"] == "环境-DEMO-A"     # A 只接 20–30
    assert rows["PO-DEMO-L0045"]["assigned_account"]["name"] == "环境-DEMO-B"

    ov = ok(owner.get("/api/v1/overview"))
    assert ov["bj_date"] == "2026-10-31" and ov["today_executable"] == 2 and ov["counts"]["assigned"] == 2
    assert ov["accounts"] == {"total": 2, "enabled": 2, "paused": 0} and ov["devices"] == {"total": 0, "online": 0}
    assert ov["discovered_buyers"] == 0
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
