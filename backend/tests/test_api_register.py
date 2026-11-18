"""HTTP：浏览器自动登记与新的开始批次规则（契约 §5）——POST /plugin/v1/register 的每一种返回码、幂等与并发、
旧版插件（0.1.0、不带 customer_id）得到看得懂的报错、登录状态全集与身份清洗、行被清扫删掉后 401 → 重新登记、
开始批次的拒绝码（DEVICE_BUSY / IDENTITY_MISMATCH / SLOT_DONE …）、执行中途换买家号、护栏 buyer 项、
/auth/me 给出工作区 id。全部数据虚构（DEMO 名称、A1DEMO… 买家号 ID）。"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from services import clock
from tests import factories as f
from tests.api_helpers import (DEMO_CUSTOMER_ID, NOW, OWNER, api, bearer, console_client, create_account,  # noqa: F401
                               err, find_order, good_facts, identity, import_orders, import_row, ok, owner,
                               plugin_client, register_device, run_body, tx)
from tests.factories_exec import device_token
from workflows import sweep as sweep_wf

REGISTER_KEYS = {"device_id", "device_name", "workspace_id", "workspace_name", "created"}
HEARTBEAT_KEYS = {"device", "login", "account", "device_run", "server_time", "bj_date", "simulation_only"}
OUTDATED = "插件版本过旧：请重新构建并加载新版插件（需要 0.2.0 及以上）"


def _event_kinds(owner, device_id: int) -> list[str]:
    rows = ok(owner.get("/api/v1/events", params={"entity": "device", "entity_id": device_id, "page_size": 200}))["rows"]
    return [e["kind"] for e in rows]


# ── 登记 ──────────────────────────────────────────────────────────────────────


def test_register_needs_no_credentials_is_idempotent_and_stores_only_the_hash(owner, api):
    plugin = plugin_client(api)
    token = device_token()
    ws = ok(owner.get("/api/v1/auth/me"))["workspace"]
    reg = ok(plugin.post("/plugin/v1/register", json={"device_token": token, "ext_version": "0.2.0"},
                         headers={"user-agent": "DEMO-UA/1.0"}))
    assert set(reg) == REGISTER_KEYS
    assert reg == {"device_id": reg["device_id"], "device_name": f"浏览器 {reg['device_id']}", "workspace_id": ws["id"],
                   "workspace_name": ws["name"], "created": True}
    # 同一个令牌再登记：还是那一行，created=false；带不带 Authorization 都一样
    again = ok(plugin.post("/plugin/v1/register", json={"device_token": token, "ext_version": "0.2.1"}))
    assert again == {**reg, "created": False}
    assert ok(plugin.post("/plugin/v1/register", json={"device_token": token}, headers=bearer(token))) == again
    devs = ok(owner.get("/api/v1/devices"))
    assert devs["total"] == 1 and devs["rows"][0]["ext_version"] == "0.2.1"
    assert devs["rows"][0]["login"] == {"state": "unknown", "customer_id": None, "at": None, "stale": False}
    with tx() as conn:
        row = conn.execute("SELECT * FROM procure.devices").fetchone()
        assert row["token_hash"] != token and len(row["token_hash"]) == 64 and token not in str(row)
        ev = conn.execute("SELECT * FROM procure.events WHERE kind = 'device.registered'").fetchall()
        assert len(ev) == 1 and ev[0]["detail"] == {"ext_version": "0.2.0", "user_agent": "DEMO-UA/1.0"}
        assert ev[0]["actor_kind"] == "device" and token not in str(ev[0])
    # 登记完就能用这个令牌调其余接口
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={"ext_version": "0.2.1"}, headers=bearer(token)))
    assert set(hb) == HEARTBEAT_KEYS and hb["device"] == {"id": reg["device_id"], "name": reg["device_name"]}


@pytest.mark.parametrize("body", [
    {}, {"device_token": None}, {"device_token": ""}, {"device_token": "short"}, {"device_token": "x" * 31},
    {"device_token": "x" * 129}, {"device_token": "has space " + "x" * 40}, {"device_token": "bad!chars" + "x" * 40},
    {"device_token": 12345678901234567890123456789012}, {"device_token": ["x" * 43]},
    {"device_token": "x" * 43 + "\n"},
])
def test_register_refuses_malformed_tokens_with_422(owner, api, body):
    plugin = plugin_client(api)
    out = err(plugin.post("/plugin/v1/register", json={"ext_version": "0.2.0", **body}), 422, "VALIDATION")
    assert out["detail"]["field"] == "device_token" and "令牌" in out["message"]
    assert ok(owner.get("/api/v1/devices", params={"include_hidden": 1}))["total"] == 0


def test_register_without_a_body_is_422_not_a_crash(owner, api):
    plugin = plugin_client(api)
    assert err(plugin.post("/plugin/v1/register"), 422, "VALIDATION")["detail"]["field"] == "device_token"
    err(plugin.post("/plugin/v1/register", json=[1, 2]), 422, "VALIDATION")
    err(plugin.post("/plugin/v1/register", content=b"{not json"), 422, "VALIDATION")


def test_register_before_first_setup_is_409_conflict(api):
    """服务端还没有任何工作区（没做过首次设置）：登记不了，提示先去控制台建用户；做完首次设置后同一个令牌登记成功。"""
    plugin = plugin_client(api)
    token = device_token()
    body = err(plugin.post("/plugin/v1/register", json={"device_token": token, "ext_version": "0.2.0"}),
               409, "CONFLICT")
    assert body["message"] == "服务端还没有初始化：请先打开控制台创建用户"
    ok(console_client(api).post("/api/v1/auth/setup", json=OWNER))
    assert ok(plugin.post("/plugin/v1/register", json={"device_token": token, "ext_version": "0.2.0"}))["created"]


def test_register_with_one_workspace_ignores_workspace_id(owner, api):
    plugin = plugin_client(api)
    ws_id = ok(owner.get("/api/v1/auth/me"))["workspace"]["id"]
    for extra in ({"workspace_id": 999999}, {"workspace_id": "nope"}, {"workspace_id": ws_id}, {}):
        reg = ok(plugin.post("/plugin/v1/register", json={"device_token": device_token(), "ext_version": "0.2.0",
                                                          **extra}))
        assert reg["workspace_id"] == ws_id and reg["created"] is True


def test_concurrent_registration_of_one_token_creates_one_browser(owner, api):
    token = device_token()
    results: list = []
    barrier = threading.Barrier(6)

    def go():
        c = plugin_client(api)
        barrier.wait(10)
        results.append(c.post("/plugin/v1/register", json={"device_token": token, "ext_version": "0.2.0"}))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    bodies = [ok(r) for r in results]
    assert len(bodies) == 6 and len({b["device_id"] for b in bodies}) == 1
    assert sum(1 for b in bodies if b["created"]) == 1
    assert ok(owner.get("/api/v1/devices"))["total"] == 1
    with tx() as conn:
        assert f.count(conn, "events", "kind = 'device.registered'") == 1


# ── 旧版插件得到看得懂的报错 ──────────────────────────────────────────────────


def test_old_plugin_gets_readable_errors(owner, api):
    """旧版插件（0.1.0）：登记、心跳、身份上报都回 409 PLUGIN_OUTDATED 并说明要更新；不带版本、也不带 customer_id 的
    开始批次请求回 422「插件版本过旧，请更新插件」；配对接口不存在。服务端状态不被旧插件改动。"""
    acc = create_account(owner)
    plugin = plugin_client(api)
    dev = register_device(plugin, acc)
    h = bearer(dev["token"])
    seen_before = ok(owner.get(f"/api/v1/devices/{dev['device_id']}"))
    clock.freeze(NOW + timedelta(seconds=60))

    for path, body in [
        ("/plugin/v1/register", {"device_token": device_token(), "ext_version": "0.1.0"}),
        ("/plugin/v1/register", {"device_token": dev["token"], "ext_version": "0.1.9"}),
        ("/plugin/v1/heartbeat", {"ext_version": "0.1.0"}),
        ("/plugin/v1/heartbeat", {"ext_version": "0.1.0", "identity": identity("A1DEMO999999")}),
        ("/plugin/v1/identity", {**identity("A1DEMO999999"), "ext_version": "0.1.0"}),
    ]:
        out = err(plugin.post(path, json=body, headers=h), 409, "PLUGIN_OUTDATED")
        assert out["message"] == OUTDATED
        assert out["detail"] == {"ext_version": body["ext_version"], "min_ext_version": "0.2.0"}
    # 旧插件的开始批次请求：{trigger, simulated}，没有 customer_id
    out = err(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h),
              422, "VALIDATION")
    assert out["message"] == "插件版本过旧，请更新插件" and out["detail"]["field"] == "customer_id"
    for empty in (None, "", "   "):
        out = err(plugin.post("/plugin/v1/runs", json=run_body(customer_id=empty), headers=h), 422, "VALIDATION")
        assert out["message"] == "插件版本过旧，请更新插件"
    out = err(plugin.post("/plugin/v1/runs", headers=h), 422, "VALIDATION")          # 连请求体都没有
    assert out["detail"]["field"] == "simulated"
    err(plugin.post("/plugin/v1/pair", json={"code": "ZZZZZZZZ", "device_name": "Mac-DEMO", "browser": "chrome",
                                             "ext_version": "0.1.0"}), 404, "NOT_FOUND")
    # 什么都没被改：登录状态、最近联系时间、浏览器个数
    assert ok(owner.get(f"/api/v1/devices/{dev['device_id']}")) == {**seen_before, "online": True}
    assert ok(owner.get("/api/v1/devices", params={"include_hidden": 1}))["total"] == 1
    # 没带版本、版本写法认不出来的不拦
    for ver in ({}, {"ext_version": None}, {"ext_version": "dev-build"}, {"ext_version": "0.2"},
                {"ext_version": "0.2.0"}, {"ext_version": "1.0.0.1"}):
        ok(plugin.post("/plugin/v1/heartbeat", json=ver, headers=h))


# ── 登录状态全集、身份清洗、心跳与身份上报同形 ────────────────────────────────


def test_login_states_and_identity_cleaning_over_http(owner, api):
    acc = create_account(owner)
    plugin = plugin_client(api)
    dev = register_device(plugin)
    h = bearer(dev["token"])

    def report(body, via="heartbeat"):
        if via == "heartbeat":
            out = ok(plugin.post("/plugin/v1/heartbeat", json={"ext_version": "0.2.0", "identity": body}, headers=h))
        else:
            out = ok(plugin.post("/plugin/v1/identity", json={**body, "ext_version": "0.2.0"}, headers=h))
        assert set(out) == HEARTBEAT_KEYS and set(out["login"]) == {"state", "customer_id", "at", "stale"}
        assert (out["account"] is not None) == (out["login"]["state"] == "ready")
        console = ok(owner.get(f"/api/v1/devices/{dev['device_id']}"))["login"]
        assert console == out["login"]                                 # 控制台看到的与插件拿到的是同一个结果
        return out["login"]

    assert dev["heartbeat"]["login"]["state"] == "unknown"
    for via in ("heartbeat", "identity"):
        assert report(identity(None, signed_in=False), via)["state"] == "signed_out"
        assert report(identity(None), via)["state"] == "no_id"
        # 形状不对的买家号 ID：当作没读到，绝不报错
        for junk in ("not-an-id", "B1DEMO000001", "A12", 12345, {"x": 1}, "A1DEMO" + "0" * 80, ""):
            got = report({**identity(None), "customer_id": junk}, via)
            assert got == {**got, "state": "no_id", "customer_id": None}
        got = report(identity(DEMO_CUSTOMER_ID, conflict=True), via)
        assert got["state"] == "conflict" and got["customer_id"] is None
        got = report(identity("A1DEMO000404"), via)
        assert got["state"] == "unregistered" and got["customer_id"] == "A1DEMO000404"
        ok(owner.post("/api/v1/accounts/discovered/ignore", json={"customer_id": "A1DEMO000404"}))
        assert report(identity("A1DEMO000404"), via)["state"] == "ignored"
        ok(owner.post("/api/v1/accounts/discovered/unignore", json={"customer_id": "A1DEMO000404"}))
        got = report(identity(" a1demo000001 "), via)                    # 大写、去空白
        assert got["state"] == "ready" and got["customer_id"] == DEMO_CUSTOMER_ID and got["stale"] is False
        # 已登录但 signed_in 不是布尔 true：不算登录
        assert report({**identity(DEMO_CUSTOMER_ID), "signed_in": "yes"}, via)["state"] == "signed_out"
    # 身份不是对象才是参数错误
    err(plugin.post("/plugin/v1/heartbeat", json={"identity": "A1DEMO000001"}, headers=h), 422, "VALIDATION")
    err(plugin.post("/plugin/v1/identity", headers=h), 422, "VALIDATION")
    # page_url 只留 origin + path；买家号 ID 之外不存别的页面内容
    ok(plugin.post("/plugin/v1/identity", json={**identity(DEMO_CUSTOMER_ID),
                                                "page_url": "https://www.amazon.com/gp/x?ref=DEMO-SECRET#frag",
                                                "cookies": "session-id=DEMO", "html": "<html>DEMO</html>"}, headers=h))
    with tx() as conn:
        ident = conn.execute("SELECT identity FROM procure.devices WHERE id = %s", (dev["device_id"],)).fetchone()
    assert set(ident["identity"]) == {"signed_in", "customer_id", "site", "page_url", "conflict", "at"}
    assert ident["identity"]["page_url"] == "https://www.amazon.com/gp/x"
    assert "device.identity_changed" in _event_kinds(owner, dev["device_id"])
    assert acc["id"] == ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=h))["account"]["id"]


def test_stale_login_report_is_flagged_and_refused_at_run_start(owner, api):
    acc = create_account(owner)
    plugin = plugin_client(api)
    dev = register_device(plugin, acc)
    h = bearer(dev["token"])
    clock.freeze(NOW + timedelta(seconds=601))                       # identity_fresh_s = 600
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=h))    # 没带身份的心跳不刷新登录状态
    assert hb["login"]["state"] == "ready" and hb["login"]["stale"] is True
    assert ok(owner.get(f"/api/v1/devices/{dev['device_id']}"))["login"]["stale"] is True
    body = err(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=h), 403, "IDENTITY_MISMATCH")
    assert body["message"] == "插件超过 10 分钟没有上报登录状态，请打开或刷新亚马逊页面后重试"
    assert body["detail"]["login_state"] == "signed_in" and body["detail"]["stale"] is True
    fresh = ok(plugin.post("/plugin/v1/identity", json=identity(acc["amazon_customer_id"]), headers=h))
    assert fresh["login"]["stale"] is False
    ok(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=h))


# ── 行被清扫删掉：401 → 用同一个令牌重新登记 ──────────────────────────────────


def test_purged_browser_gets_401_then_reregisters_with_the_same_token(owner, api):
    """长期没有联系、也没有批次引用的浏览器行会被清扫删掉；它再回来时 401，插件用同一个令牌重新登记后照常工作。"""
    acc = create_account(owner)
    plugin = plugin_client(api)
    dev = register_device(plugin, acc)
    h = bearer(dev["token"])
    clock.freeze(NOW + timedelta(days=31))
    assert sweep_wf.run()["removed_devices"] == [dev["device_id"]]
    ok(owner.post("/api/v1/auth/login", json={"username": OWNER["username"], "password": OWNER["password"]}))
    assert ok(owner.get("/api/v1/devices", params={"include_hidden": 1}))["total"] == 0      # 会话 14 天已过期，重新登录
    for path, body in [("/plugin/v1/heartbeat", {}), ("/plugin/v1/identity", identity(DEMO_CUSTOMER_ID)),
                       ("/plugin/v1/runs", run_body(acc)), ("/plugin/v1/runs/1/next", None),
                       ("/plugin/v1/attempts/1/result", {"outcome": "unknown"})]:
        out = err(plugin.post(path, json=body, headers=h), 401, "UNAUTHORIZED")
        assert out["message"] == "这个浏览器还没有登记，插件会自动重新登记"
    reg = ok(plugin.post("/plugin/v1/register", json={"device_token": dev["token"], "ext_version": "0.2.0"}))
    assert reg["created"] is True and reg["device_id"] != dev["device_id"]
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={"identity": identity(DEMO_CUSTOMER_ID)}, headers=h))
    assert hb["device"]["id"] == reg["device_id"] and hb["login"]["state"] == "ready"
    ok(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=h))


# ── 开始批次：定时时间点 ──────────────────────────────────────────────────────


def test_scheduled_runs_need_a_due_slot_and_run_once_per_slot(owner, api):
    """定时批次（北京 10:00）：必须带账号执行时间里已到点的 slot，否则 422（说明原因）；同一个时间点今天执行过 → 409
    SLOT_DONE；两个浏览器登录同一个买家号时，第二个得到的是 SLOT_DONE 而不是又跑一遍。"""
    acc = create_account(owner, purchase_mode="scheduled", schedule_times=["09:30", "11:00"])
    manual = create_account(owner, name="环境-DEMO-手动", amazon_customer_id="A1DEMO000002")
    plugin = plugin_client(api)
    dev = register_device(plugin, acc)
    dev2 = register_device(plugin, acc)
    h, h2 = bearer(dev["token"]), bearer(dev2["token"])
    assert dev["heartbeat"]["account"]["purchase_mode"] == "scheduled"
    assert dev["heartbeat"]["account"]["schedule_times"] == ["09:30", "11:00"]

    for slot, why in [(None, "时间点"), ("", "时间点"), ("25:00", "格式"), ("0930", "格式"), ("08:00", "不在"),
                      ("11:00", "还没有到")]:
        body = err(plugin.post("/plugin/v1/runs", json=run_body(acc, trigger="scheduled", slot=slot), headers=h),
                   422, "VALIDATION")
        assert body["detail"]["field"] == "slot" and why in body["message"], body
    mdev = register_device(plugin, manual)
    body = err(plugin.post("/plugin/v1/runs", json=run_body(manual, trigger="scheduled", slot="09:30"),
                           headers=bearer(mdev["token"])), 422, "VALIDATION")
    assert "不是定时执行模式" in body["message"]

    run = ok(plugin.post("/plugin/v1/runs", json=run_body(acc, trigger="scheduled", slot="9:30"), headers=h))
    with tx() as conn:
        row = conn.execute("SELECT * FROM procure.execution_runs WHERE id = %s", (run["run_id"],)).fetchone()
    assert row["trigger"] == "scheduled" and row["scheduled_slot"] == "09:30" and str(row["bj_date"]) == "2026-10-31"
    # 进行中：另一个浏览器同一个时间点 → SLOT_DONE（先于 ACCOUNT_BUSY）；手动开 → ACCOUNT_BUSY
    body = err(plugin.post("/plugin/v1/runs", json=run_body(acc, trigger="scheduled", slot="09:30"), headers=h2),
               409, "SLOT_DONE")
    assert body["message"] == "这个时间点今天已经执行过"
    err(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=h2), 409, "ACCOUNT_BUSY")
    ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish", json={"reason": "finished"}, headers=h))
    # 结束后：同一个时间点今天不再执行；手动随时可以
    for hh in (h, h2):
        err(plugin.post("/plugin/v1/runs", json=run_body(acc, trigger="scheduled", slot="09:30"), headers=hh),
            409, "SLOT_DONE")
    again = ok(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=h2))
    with tx() as conn:
        assert conn.execute("SELECT scheduled_slot FROM procure.execution_runs WHERE id = %s",
                            (again["run_id"],)).fetchone()["scheduled_slot"] is None
    ok(plugin.post(f"/plugin/v1/runs/{again['run_id']}/finish", json={}, headers=h2))
    # 到了第二天，同一个时间点又可以执行
    clock.freeze(NOW + timedelta(days=1))
    ok(plugin.post("/plugin/v1/heartbeat", json={"identity": identity(DEMO_CUSTOMER_ID)}, headers=h))
    ok(plugin.post("/plugin/v1/runs", json=run_body(acc, trigger="scheduled", slot="09:30"), headers=h))


# ── 执行中途换了买家号、护栏 buyer 项 ─────────────────────────────────────────


def _claimed(owner, api, no="PO-DEMO-R0001"):
    acc = create_account(owner)
    import_orders(owner, [import_row(no)])
    plugin = plugin_client(api)
    dev = register_device(plugin, acc)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    return acc, plugin, h, run, att


def test_buyer_switch_before_submit_aborts_without_pausing_the_account(owner, api):
    """执行中途浏览器换登了别的买家号（还没登记提交）：进度让插件停下、提交登记 409、批次心跳要求停止；
    插件中止后任务回已分配，账号不暂停、不占额度。"""
    acc, plugin, h, run, att = _claimed(owner, api)
    aid = att["attempt_id"]
    ok(plugin.post(f"/plugin/v1/attempts/{aid}/guard", json=good_facts(att), headers=h))
    ok(plugin.post("/plugin/v1/identity", json=identity("A1DEMO000002"), headers=h))
    pr = ok(plugin.post(f"/plugin/v1/attempts/{aid}/progress", json={"stage": "before_submit"}, headers=h))
    assert pr["continue"] is False and pr["cancel_code"] == "IDENTITY_MISMATCH" and "买家号变了" in pr["cancel_reason"]
    body = err(plugin.post(f"/plugin/v1/attempts/{aid}/submitting", headers=h), 409, "IDENTITY_MISMATCH")
    assert body["detail"]["login_state"] == "signed_in" and body["detail"]["stale"] is False
    assert set(body["detail"]) == {"login_state", "stale", "identity_at"}
    rhb = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/heartbeat", headers=h))
    assert rhb["stop"] is True and rhb["stop_reason"] == "identity_mismatch"
    nxt = ok(plugin.post(f"/plugin/v1/attempts/{aid}/result",
                         json={"outcome": "failed", "error_code": "ABORTED_BY_SERVER",
                               "error_detail": pr["cancel_reason"]}, headers=h))
    assert nxt == {"attempt_status": "aborted", "order_status": "assigned"}
    a = ok(owner.get(f"/api/v1/accounts/{acc['id']}"))
    assert a["execution_paused"] is False and a["quota_today"]["used"] == 0
    assert find_order(owner, "PO-DEMO-R0001")["status"] == "assigned"


def test_buyer_switch_after_submit_still_accepts_the_result(owner, api):
    """已经登记提交之后才换的买家号：结果照常收（回执不丢），不做身份检查。"""
    acc, plugin, h, run, att = _claimed(owner, api)
    aid = att["attempt_id"]
    ok(plugin.post(f"/plugin/v1/attempts/{aid}/guard", json=good_facts(att), headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{aid}/submitting", headers=h))
    ok(plugin.post("/plugin/v1/identity", json=identity(None, signed_in=False), headers=h))
    res = ok(plugin.post(f"/plugin/v1/attempts/{aid}/result",
                         json={"outcome": "succeeded", "amazon_order_no": f"SIM-20261031-{aid}",
                               "amounts": {"total": "25.00"}}, headers=h))
    assert res == {"attempt_status": "succeeded", "order_status": "purchased"}
    fin = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish", json={"reason": "finished"}, headers=h))
    assert fin["end_reason"] == "finished"


def test_guard_checks_the_buyer_read_on_the_checkout_page(owner, api):
    """护栏 buyer 项：结算页事实里的 customer_id 必须是批次账号登记的买家号 ID；缺失按数据不可靠拦截。"""
    acc, plugin, h, run, att = _claimed(owner, api)
    aid = att["attempt_id"]
    wrong = ok(plugin.post(f"/plugin/v1/attempts/{aid}/guard", json=good_facts(att, customer_id="A1DEMO000002"),
                           headers=h))
    assert wrong["verdict"] == "block" and wrong["code"] == "IDENTITY_MISMATCH"
    assert "A1DEMO000002" in wrong["reasons"][0] and DEMO_CUSTOMER_ID in wrong["reasons"][0]
    assert wrong["checks"]["buyer"]["ok"] is False
    row = find_order(owner, "PO-DEMO-R0001")
    assert row["status"] == "exception" and row["latest_attempt"]["error_code"] == "IDENTITY_MISMATCH"
    assert ok(owner.get(f"/api/v1/accounts/{acc['id']}"))["execution_paused"] is False

    import_orders(owner, [import_row("PO-DEMO-R0002")])
    att2 = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    facts = good_facts(att2)
    del facts["customer_id"]
    missing = ok(plugin.post(f"/plugin/v1/attempts/{att2['attempt_id']}/guard", json=facts, headers=h))
    assert missing["verdict"] == "block" and missing["code"] == "DATA_UNRELIABLE"

    import_orders(owner, [import_row("PO-DEMO-R0003")])
    att3 = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    passed = ok(plugin.post(f"/plugin/v1/attempts/{att3['attempt_id']}/guard",
                            json=good_facts(att3, customer_id=" a1demo000001"), headers=h))
    assert passed["verdict"] == "pass" and passed["checks"]["buyer"]["ok"] is True
    stored = ok(owner.get(f"/api/v1/orders/{find_order(owner, 'PO-DEMO-R0003')['id']}"))["attempts"][0]["guard_input"]
    assert stored["customer_id"] == DEMO_CUSTOMER_ID


# ── 工作区 id（「设置」页显示；系统里有多个工作区时插件设置里要填它）──────────────


def test_auth_responses_carry_the_workspace_id(api):
    c = console_client(api)
    setup = ok(c.post("/api/v1/auth/setup", json=OWNER))
    assert set(setup["workspace"]) == {"id", "name"} and isinstance(setup["workspace"]["id"], int)
    me = ok(c.get("/api/v1/auth/me"))
    assert me["workspace"] == setup["workspace"] == {"id": setup["workspace"]["id"], "name": OWNER["workspace_name"]}
    login = ok(console_client(api).post("/api/v1/auth/login",
                                        json={"username": OWNER["username"], "password": OWNER["password"]}))
    assert login["workspace"] == me["workspace"]
    reg = ok(plugin_client(api).post("/plugin/v1/register", json={"device_token": device_token()}))
    assert reg["workspace_id"] == me["workspace"]["id"]
