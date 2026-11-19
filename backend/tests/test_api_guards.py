"""HTTP 入口的闸：/plugin 入口闸（本机对端、不带代理转发头、本机 Host、不接受网页 Origin、扩展 ID 名单、POST 必须是
JSON）、旧版配对接口的占位、登录失败限速、浏览器登记数量上限（只数还在用的）、请求体大小（含分块上传）、Host 与
Origin 检查（防 DNS 重绑定）、解析不了的 Origin 与存不了的字符不变成 500。全部数据虚构。"""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from registry import settings
from server import limits
from services import clock
from tests import factories as f
from tests.api_helpers import (JSON, NOW, OWNER, api, bearer, console_client, create_account, err,  # noqa: F401
                               find_order, good_facts, identity, import_orders, import_row, local_client, ok, owner,
                               plugin_client, register_device, remote_client, run_body, tx)
from tests.factories_exec import device_token
from workflows import sweep as sweep_wf

#: 虚构的扩展 ID（Chrome 的扩展 ID 是 32 个 a–p 的字母）
EXT_ID = "abcdefghijklmnopabcdefghijklmnop"
OTHER_EXT_ID = "ponmlkjihgfedcbaponmlkjihgfedcba"


def _login(c, username="demo_owner", password="wrong-pass"):
    return c.post("/api/v1/auth/login", json={"username": username, "password": password})


def test_login_failures_are_rate_limited_per_ip_and_username(owner, api):
    c = console_client(api)
    for _ in range(10):
        err(_login(c), 401, "UNAUTHORIZED")
    r = _login(c)
    body = err(r, 429, "RATE_LIMITED")
    assert body["message"] == "尝试次数过多，请稍后再试" and body["detail"]["retry_after_s"] > 0
    assert int(r.headers["retry-after"]) == body["detail"]["retry_after_s"]
    err(_login(c, password=OWNER["password"]), 429, "RATE_LIMITED")      # 被限速时连正确密码也不验证
    err(_login(c, username="someone_else"), 401, "UNAUTHORIZED")          # 别的用户名不受影响
    clock.freeze(NOW + timedelta(minutes=11))                             # 窗口过去
    ok(_login(c, password=OWNER["password"]))


def test_successful_login_clears_failures(owner, api):
    c = console_client(api)
    for _ in range(9):
        _login(c)
    ok(_login(c, password=OWNER["password"]))
    for _ in range(9):
        err(_login(c), 401, "UNAUTHORIZED")


def test_register_is_not_failure_rate_limited_but_capped_by_max_devices(owner, api, monkeypatch):
    """本机所有浏览器共用 127.0.0.1 一个来源地址：登记不按失败次数限速（否则几十个环境同时启动会把自己限住）；
    数量由 settings.max_devices 封顶，满了 → 429 RATE_LIMITED（说明里写的是所有者能做的事，不是「请稍后再试」）；
    已登记的令牌照常幂等。所有者在控制台移除一个浏览器，名额立刻腾出来；被移除的再联系时回到列表，不查上限。"""
    c = plugin_client(api)
    for _ in range(25):
        body = err(c.post("/plugin/v1/register", json={"device_token": "too-short", "ext_version": "0.2.0"}),
                   422, "VALIDATION")
        assert body["detail"]["field"] == "device_token"
    monkeypatch.setattr(settings, "_loaded", dataclasses.replace(settings.get(), max_devices=2))
    first = register_device(c)
    register_device(c)
    r = c.post("/plugin/v1/register", json={"device_token": device_token(), "ext_version": "0.2.0"})
    body = err(r, 429, "RATE_LIMITED")
    assert body["message"] == "登记的浏览器太多：请在控制台「账号与浏览器」里移除不用的浏览器，或调大 PC_MAX_DEVICES"
    assert body["detail"] == {} and "retry-after" not in r.headers       # 等是等不来的，所以不给重试时间
    again = ok(c.post("/plugin/v1/register", json={"device_token": first["token"], "ext_version": "0.2.0"}))
    assert again["device_id"] == first["device_id"] and again["created"] is False
    assert ok(owner.get("/api/v1/devices"))["total"] == 2
    # 控制台「移除」立刻腾出一个名额
    assert ok(owner.post(f"/api/v1/devices/{first['device_id']}/remove"))["hidden_at"] is not None
    assert ok(c.post("/plugin/v1/register", json=_register_body()))["created"] is True
    err(c.post("/plugin/v1/register", json=_register_body()), 429, "RATE_LIMITED")
    # 被移除的浏览器再联系：回到列表，不查上限（显示出来的可以比上限多出回来的这一个）
    hb = ok(c.post("/plugin/v1/heartbeat", json={"ext_version": "0.2.0"}, headers=bearer(first["token"])))
    assert hb["device"]["id"] == first["device_id"]
    assert ok(owner.get("/api/v1/devices"))["total"] == 3
    err(c.post("/plugin/v1/register", json=_register_body()), 429, "RATE_LIMITED")


def test_register_cap_ignores_browsers_not_seen_within_retention(owner, api, monkeypatch):
    """换过令牌的旧环境过了保留期（30 天没有联系）不再占名额。执行过批次的旧行清扫不会删（留给历史记录显示名称），
    以前它们永远占着名额：攒到上限后谁也登记不进来，等多久都没用。"""
    acc = create_account(owner)
    plugin = plugin_client(api)
    ran = register_device(plugin, acc)                                    # 执行过一个批次的环境
    h = bearer(ran["token"])
    run = ok(plugin.post("/plugin/v1/runs", json=run_body(acc), headers=h))
    ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish", json={"reason": "finished"}, headers=h))
    idle = register_device(plugin, acc)                                   # 从没执行过的环境
    monkeypatch.setattr(settings, "_loaded", dataclasses.replace(settings.get(), max_devices=2))
    err(plugin.post("/plugin/v1/register", json=_register_body()), 429, "RATE_LIMITED")
    clock.freeze(NOW + timedelta(days=31))
    assert sweep_wf.run()["removed_devices"] == [idle["device_id"]]       # 只删得掉没有历史的那一行
    with tx() as conn:
        assert [r["id"] for r in conn.execute("SELECT id FROM procure.devices").fetchall()] == [ran["device_id"]]
    for _ in range(2):                                                    # 留下的旧行不占名额
        assert ok(plugin.post("/plugin/v1/register", json=_register_body()))["created"] is True
    r = plugin.post("/plugin/v1/register", json=_register_body())
    assert err(r, 429, "RATE_LIMITED")["message"].endswith("或调大 PC_MAX_DEVICES")
    # 旧环境回来了：用原来的令牌照常工作（不查上限）
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={"ext_version": "0.2.0"}, headers=h))
    assert hb["device"]["id"] == ran["device_id"]


def test_rate_limit_tunables_come_from_settings(api, monkeypatch):
    from server.app import TEST_CLIENT_HOST, create_app

    monkeypatch.setattr(settings, "_loaded", dataclasses.replace(settings.get(), login_fail_limit=2))
    c = TestClient(create_app(web_dist=None, allowed_hosts=(TEST_CLIENT_HOST,)),
                   headers={"X-Requested-With": "pc-console"})
    for _ in range(2):
        _login(c)
    err(_login(c), 429, "RATE_LIMITED")


def test_chunked_oversized_bodies_are_refused_before_auth(api):
    """没有 Content-Length 的分块请求体同样受限：上传 > 10 MB、其他接口 > 1 MB → 413，不等认证。"""
    c = console_client(api)

    def chunks(total, size=256 * 1024):
        sent = 0
        while sent < total:
            n = min(size, total - sent)
            sent += n
            yield b"x" * n

    r = c.post("/plugin/v1/register", content=chunks(2 * 1024 * 1024), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "VALIDATION"
    r = c.post("/api/v1/imports", content=chunks(11 * 1024 * 1024),
               headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413 and "10 MB" in r.json()["message"]
    r = c.post("/api/v1/auth/login", content=b"x" * (1024 * 1024 + 1), headers={"content-type": "application/json"})
    assert r.status_code == 413
    # 正常大小照常处理
    err(c.post("/api/v1/auth/login", json={"username": "x", "password": "y"}), 401, "UNAUTHORIZED")


def test_foreign_host_and_origin_are_refused(api):
    """DNS 重绑定：Host 不是本机地址 → 403（包括首次设置、登录与插件登记）；/api 写请求的 Origin 不符 → 403。"""
    c = console_client(api)
    for path in ("/api/v1/auth/setup", "/api/v1/auth/login", "/plugin/v1/register"):
        r = c.post(path, json={}, headers={"host": "rebind.attacker.example:27431"})
        assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", (path, r.text)
    r = c.get("/api/v1/openapi.json", headers={"host": "rebind.attacker.example"})
    assert r.status_code == 403
    r = c.post("/api/v1/auth/setup", json=OWNER, headers={"origin": "http://rebind.attacker.example:27431"})
    assert r.status_code == 403 and "Origin" in r.json()["message"]
    ok(c.get("/healthz", headers={"host": "127.0.0.1:8790"}))
    ok(c.get("/healthz", headers={"host": "localhost:8790"}))
    ok(c.get("/healthz", headers={"host": "[::1]:8790"}))
    r = c.post("/api/v1/auth/setup", json=OWNER, headers={"host": "127.0.0.1:8790", "origin": "http://127.0.0.1:8790"})
    assert r.status_code == 200
    # 插件从扩展 service worker 调用：Origin 是 chrome-extension://，没有配置扩展 ID 名单时放行
    r = c.post("/plugin/v1/register", json={"device_token": device_token(), "ext_version": "0.2.0"},
               headers={"host": "127.0.0.1:8790", "origin": f"chrome-extension://{EXT_ID}"})
    assert r.status_code == 200 and r.json()["created"] is True


# ── /plugin 入口闸（契约 §5「信任边界」）──────────────────────────────────────


def _register_body() -> dict:
    return {"device_token": device_token(), "ext_version": "0.2.0"}


def _devices() -> int:
    with tx() as conn:
        return f.count(conn, "devices")


def _forbidden(resp, message: str) -> None:
    body = err(resp, 403, "FORBIDDEN")
    assert body["message"] == message, body


def _plugin_posts(app) -> list[str]:
    """应用里登记的全部 /plugin 路由（路径参数填 1）。"""
    paths = sorted({r.path for r in app.routes if getattr(r, "path", "").startswith("/plugin/")
                    and "POST" in (getattr(r, "methods", None) or ())})
    assert "/plugin/v1/register" in paths and "/plugin/v1/heartbeat" in paths and len(paths) >= 12
    return [p.replace("{run_id}", "1").replace("{attempt_id}", "1") for p in paths]


@pytest.mark.parametrize("peer", ["192.0.2.10", "10.0.0.8", "::ffff:192.0.2.10", "2001:db8::1", "testclient",
                                  "127.0.0.2", "0.0.0.0", ""])
def test_plugin_gate_refuses_non_loopback_peers(owner, api, peer):
    """① 对端不是回环地址 → 403，与令牌、Host 名单无关；每一个 /plugin 接口都一样，而且什么都没写进库。"""
    dev = register_device(plugin_client(api), create_account(owner))
    before = _devices()
    far = remote_client(api, peer, headers=JSON)
    for path in _plugin_posts(api):
        _forbidden(far.post(path, json=_register_body(), headers=bearer(dev["token"])), limits.PLUGIN_REMOTE_MESSAGE)
    _forbidden(far.get("/plugin/v1/no-such-thing"), limits.PLUGIN_REMOTE_MESSAGE)
    _forbidden(far.options("/plugin/v1/heartbeat"), limits.PLUGIN_REMOTE_MESSAGE)
    assert limits.PLUGIN_REMOTE_MESSAGE == "插件接口只接受本机请求：跨机器部署需要接入密钥，当前版本不支持"
    assert _devices() == before
    # 控制台接口不受这道闸影响（它有自己的登录、Host 与防跨站检查）
    assert ok(far.get("/api/v1/auth/state")) == {"needs_setup": False}
    assert ok(far.get("/healthz")) == {"ok": True}


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_plugin_gate_accepts_loopback_peers(owner, api, peer):
    c = local_client(api, client=(peer, 40000), headers=JSON)
    reg = ok(c.post("/plugin/v1/register", json=_register_body()))
    assert reg["created"] is True


@pytest.mark.parametrize("header, value", [
    ("X-Forwarded-For", "203.0.113.7"), ("x-forwarded-for", "127.0.0.1"), ("X-FORWARDED-FOR", "203.0.113.7, 10.0.0.1"),
    ("Forwarded", "for=203.0.113.7;proto=https;host=console.demo.example"), ("X-Real-IP", "203.0.113.7"),
    ("x-real-ip", "::1"), ("Via", "1.1 proxy.demo.example"), ("via", "HTTP/1.1 demo-proxy"),
])
def test_plugin_gate_refuses_requests_forwarded_by_a_proxy(owner, api, header, value):
    """② 带代理转发头（X-Forwarded-For / Forwarded / X-Real-IP / Via 任一个，不看值、不分大小写）→ 403：本机的反向
    代理把远端请求转过来时对端与 Host 都是本机，靠这些头认出来。每一个 /plugin 接口都一样，有没有令牌都一样，
    什么都没写进库；控制台接口不受这道闸影响。"""
    dev = register_device(plugin_client(api), create_account(owner))
    before = _devices()
    c = plugin_client(api)
    for path in _plugin_posts(api):
        _forbidden(c.post(path, json=_register_body(), headers={header: value, **bearer(dev["token"])}),
                   limits.PLUGIN_PROXIED_MESSAGE)
    _forbidden(c.post("/plugin/v1/register", json=_register_body(), headers={header: value}),
               limits.PLUGIN_PROXIED_MESSAGE)
    _forbidden(c.get("/plugin/v1/no-such-thing", headers={header: value}), limits.PLUGIN_PROXIED_MESSAGE)
    _forbidden(c.options("/plugin/v1/heartbeat", headers={header: value}), limits.PLUGIN_PROXIED_MESSAGE)
    assert limits.PLUGIN_PROXIED_MESSAGE == "插件接口不接受经过代理转发的请求"
    assert _devices() == before
    assert ok(owner.get("/api/v1/auth/me", headers={header: value}))["workspace"]["name"] == OWNER["workspace_name"]
    assert ok(c.get("/healthz", headers={header: value})) == {"ok": True}
    # 不带这些头照常（别的 X- 头不算）
    assert ok(c.post("/plugin/v1/register", json=_register_body(),
                     headers={"X-Request-Id": "demo-1", "X-Forwarded-Host-Demo": "x"}))["created"] is True


def test_plugin_gate_treats_an_empty_proxy_header_as_present():
    """纯函数：头在就拒绝，值是空的也一样（不解析值，没有「可信代理」名单）。"""
    def scope(**headers):
        hs = {"host": "127.0.0.1:8790", "content-type": "application/json", **headers}
        return {"type": "http", "method": "POST", "path": "/plugin/v1/heartbeat", "client": ("127.0.0.1", 1),
                "headers": [(k.encode(), v.encode()) for k, v in hs.items()]}

    gate = limits.PluginGateMiddleware(app=None)
    assert gate.refusal(scope()) is None
    assert limits.PROXY_HEADERS == (b"x-forwarded-for", b"forwarded", b"x-real-ip", b"via")
    for name in ("x-forwarded-for", "Forwarded", "X-Real-Ip", "VIA"):
        assert gate.refusal(scope(**{name: ""})) == limits.PLUGIN_PROXIED_MESSAGE, name
        assert gate.refusal(scope(**{name: "unknown"})) == limits.PLUGIN_PROXIED_MESSAGE, name
    for name in ("x-forwarded-proto", "x-forwarded-host", "forwarded-for", "x-via"):
        assert gate.refusal(scope(**{name: "demo"})) is None, name


def test_pair_placeholder_tells_old_plugins_to_update_and_does_nothing_else(owner, api):
    """旧版插件（0.1.0）还在调的 POST /plugin/v1/pair：只剩一个占位，恒回 409 PLUGIN_OUTDATED「插件版本过旧，请更新
    插件」——不要令牌、不解析请求体、不碰数据库，没有恢复任何配对逻辑；照样先过入口闸；不出现在接口文档里。"""
    c = plugin_client(api)
    before = _devices()
    for kw in ({"json": {"code": "ZZZZZZZZ", "device_name": "Mac-DEMO", "browser": "chrome", "ext_version": "0.1.0"}},
               {"json": {}}, {"json": [1, 2]}, {"content": b"{not json"}, {}):
        out = err(c.post("/plugin/v1/pair", **kw), 409, "PLUGIN_OUTDATED")
        assert out["message"] == "插件版本过旧，请更新插件" and out["detail"] == {"min_ext_version": "0.2.0"}
    out = err(c.post("/plugin/v1/pair", json={}, headers=bearer(device_token())), 409, "PLUGIN_OUTDATED")
    assert out["message"] == "插件版本过旧，请更新插件"
    with tx() as conn:
        assert _devices() == before and f.count(conn, "events") == 0
    # 仍过入口闸：非本机对端、经代理转发、网页来源、不是 JSON → 403，轮不到占位
    _forbidden(remote_client(api, headers=JSON).post("/plugin/v1/pair", json={}), limits.PLUGIN_REMOTE_MESSAGE)
    _forbidden(c.post("/plugin/v1/pair", json={}, headers={"X-Forwarded-For": "203.0.113.7"}),
               limits.PLUGIN_PROXIED_MESSAGE)
    _forbidden(c.post("/plugin/v1/pair", json={}, headers={"origin": "http://evil.example"}),
               limits.PLUGIN_WEB_ORIGIN_MESSAGE)
    _forbidden(local_client(api).post("/plugin/v1/pair", content=b"code=ZZZZZZZZ",
                                      headers={"content-type": "application/x-www-form-urlencoded"}),
               limits.PLUGIN_JSON_MESSAGE)
    # 只有 POST；不在接口文档里，也没有别的配对 / 绑定接口回来
    assert c.get("/plugin/v1/pair").status_code in (404, 405)
    paths = set(ok(owner.get("/api/v1/openapi.json"))["paths"])
    assert not any("pair" in p for p in paths)
    assert "/plugin/v1/pair" in {getattr(r, "path", "") for r in api.routes}
    assert owner.post("/api/v1/devices/pairing-codes").status_code in (404, 405)


def test_plugin_gate_requires_a_local_host_header_regardless_of_allowed_hosts(clean_db, tmp_path, monkeypatch):
    """② Host 必须是本机名：PC_ALLOWED_HOSTS 与测试放行的主机名只对控制台有效，对 /plugin 无效。"""
    from server.app import TEST_CLIENT_HOST, create_app

    with tx() as conn:
        f.make_workspace(conn)
    monkeypatch.setattr(settings, "_loaded", dataclasses.replace(settings.get(), allowed_hosts=("console.demo.lan",)))
    app = create_app(web_dist=tmp_path / "no-dist", allowed_hosts=(TEST_CLIENT_HOST,))
    c = local_client(app, headers=JSON)
    for host in ("console.demo.lan:8790", "console.demo.lan", TEST_CLIENT_HOST, "127.0.0.1.evil.example",
                 "192.0.2.10:8790"):
        r = c.post("/plugin/v1/register", json=_register_body(), headers={"host": host})
        assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", (host, r.text)
    for host in ("console.demo.lan:8790", TEST_CLIENT_HOST):
        _forbidden(c.post("/plugin/v1/register", json=_register_body(), headers={"host": host}),
                   limits.PLUGIN_HOST_MESSAGE)
        assert "needs_setup" in ok(c.get("/api/v1/auth/state", headers={"host": host}))           # 控制台照常
    assert _devices() == 0
    for host in ("127.0.0.1:8790", "localhost:8790", "LOCALHOST", "[::1]:8790", "127.0.0.1"):
        assert ok(c.post("/plugin/v1/register", json=_register_body(), headers={"host": host}))["created"] is True


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8790", "http://localhost:8790", "https://www.amazon.com",
                                    "http://evil.example", "HTTPS://Evil.Example", "null"])
def test_plugin_gate_refuses_web_origins(owner, api, origin):
    """③ 带网页来源（http / https，或不透明来源 null）的请求一律 403：网页不能替所有者登记浏览器或调用协议，
    哪怕它拿到了令牌、哪怕来源就是控制台自己。预检同样被拒绝。"""
    dev = register_device(plugin_client(api), create_account(owner))
    before = _devices()
    c = plugin_client(api)
    for path in _plugin_posts(api):
        _forbidden(c.post(path, json=_register_body(), headers={"origin": origin, **bearer(dev["token"])}),
                   limits.PLUGIN_WEB_ORIGIN_MESSAGE)
    _forbidden(c.options("/plugin/v1/register", headers={"origin": origin,
                                                         "access-control-request-method": "POST"}),
               limits.PLUGIN_WEB_ORIGIN_MESSAGE)
    assert limits.PLUGIN_WEB_ORIGIN_MESSAGE == "插件接口不接受网页来源的请求"
    assert _devices() == before


def test_plugin_gate_accepts_extension_origin_or_none_when_no_id_list(owner, api):
    c = plugin_client(api)
    for headers in ({}, {"origin": f"chrome-extension://{EXT_ID}"}, {"origin": f"chrome-extension://{OTHER_EXT_ID}"}):
        assert ok(c.post("/plugin/v1/register", json=_register_body(), headers=headers))["created"] is True
    assert c.options("/plugin/v1/heartbeat").status_code == 204            # 不带来源的 OPTIONS 照旧 204，没有 CORS 头
    assert "access-control-allow-origin" not in c.options("/plugin/v1/heartbeat").headers


@pytest.mark.parametrize("content_type", [None, "text/plain", "text/plain;charset=UTF-8",
                                          "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
                                          "application/jsonx", "text/json"])
def test_plugin_gate_requires_json_content_type_on_post(owner, api, content_type):
    """④ POST 必须带 Content-Type: application/json：网页的表单与 no-cors 请求只能用简单类型，进不来；
    没有这道闸时，不带类型的请求体会被当成 JSON 解析。"""
    dev = register_device(plugin_client(api), create_account(owner))
    before = _devices()
    c = local_client(api)
    raw = b'{"device_token": "' + device_token().encode() + b'", "ext_version": "0.2.0"}'
    headers = {"content-type": content_type} if content_type else {}
    _forbidden(c.post("/plugin/v1/register", content=raw, headers=headers), limits.PLUGIN_JSON_MESSAGE)
    for path in _plugin_posts(api):                                       # 没有请求体的接口也一样
        _forbidden(c.post(path, content=raw, headers={**headers, **bearer(dev["token"])}), limits.PLUGIN_JSON_MESSAGE)
    assert limits.PLUGIN_JSON_MESSAGE == "插件接口只接受 JSON 请求"
    assert _devices() == before


def test_plugin_gate_accepts_json_content_type_with_charset(owner, api):
    c = local_client(api)
    for ct in ("application/json", "application/json; charset=utf-8", "Application/JSON;charset=UTF-8"):
        raw = b'{"device_token": "' + device_token().encode() + b'", "ext_version": "0.2.0"}'
        assert ok(c.post("/plugin/v1/register", content=raw, headers={"content-type": ct}))["created"] is True
    # GET 没有请求体，不看 Content-Type（/plugin 下没有 GET 接口：照常 404）
    err(c.get("/plugin/v1/no-such-thing"), 404, "NOT_FOUND")
    # 闸在认证之前：过了闸、没有令牌 → 401；没有请求体的 POST 带上类型即可
    err(c.post("/plugin/v1/heartbeat", headers=JSON), 401, "UNAUTHORIZED")


def test_plugin_gate_extension_id_allow_list(owner, clean_db, tmp_path, monkeypatch):
    """⑤ 配置了 PC_PLUGIN_EXTENSION_IDS：Origin 必须是 chrome-extension://<名单里的 ID>，没带 Origin 也拒绝；
    别的扩展（同一个浏览器里的第三方扩展）进不来。控制台接口不受影响。"""
    from server.app import create_app

    monkeypatch.setattr(settings, "_loaded",
                        dataclasses.replace(settings.get(), plugin_extension_ids=(EXT_ID, "bbbbccccddddeeeeffffgggghhhhiiii")))
    app = create_app(web_dist=tmp_path / "no-dist")
    c = local_client(app, headers=JSON)
    mine = {"origin": f"chrome-extension://{EXT_ID}"}
    reg = ok(c.post("/plugin/v1/register", json=_register_body(), headers=mine))
    assert reg["created"] is True
    assert ok(c.post("/plugin/v1/register", json=_register_body(),
                     headers={"origin": f"chrome-extension://{EXT_ID.upper()}"}))["created"] is True
    assert ok(c.post("/plugin/v1/register", json=_register_body(),
                     headers={"origin": "chrome-extension://bbbbccccddddeeeeffffgggghhhhiiii"}))["created"] is True
    before = _devices()
    for headers in ({}, {"origin": f"chrome-extension://{OTHER_EXT_ID}"}, {"origin": "moz-extension://" + EXT_ID},
                    {"origin": f"chrome-extension://{EXT_ID}.evil.example"}, {"origin": EXT_ID},
                    {"origin": "chrome-extension://"}):
        _forbidden(c.post("/plugin/v1/register", json=_register_body(), headers=headers),
                   limits.PLUGIN_EXTENSION_MESSAGE)
    _forbidden(c.post("/plugin/v1/register", json=_register_body(), headers={"origin": "http://127.0.0.1:8790"}),
               limits.PLUGIN_WEB_ORIGIN_MESSAGE)
    _forbidden(c.post("/plugin/v1/heartbeat", json={}), limits.PLUGIN_EXTENSION_MESSAGE)       # 先于令牌检查
    assert "PC_PLUGIN_EXTENSION_IDS" in limits.PLUGIN_EXTENSION_MESSAGE
    assert _devices() == before
    assert ok(c.get("/api/v1/auth/state"))["needs_setup"] is False            # 控制台不看扩展 ID


def test_plugin_gate_runs_before_auth_body_parsing_and_rate_limits(api):
    """闸在最外层：被拒绝的请求不解析请求体、不查令牌、不进限速统计。"""
    far = remote_client(api, headers=JSON)
    _forbidden(far.post("/plugin/v1/register", content=b"{not json"), limits.PLUGIN_REMOTE_MESSAGE)
    _forbidden(far.post("/plugin/v1/heartbeat", content=b"x" * (2 * 1024 * 1024)), limits.PLUGIN_REMOTE_MESSAGE)
    c = plugin_client(api)
    err(c.post("/plugin/v1/register", content=b"{not json"), 422, "VALIDATION")       # 过了闸才轮到解析
    assert _devices() == 0


def test_plugin_gate_refusal_reasons_in_order():
    """纯函数：按「对端 → 代理转发头 → Host → Origin → 扩展 ID → Content-Type」的顺序给出第一个不满足的原因。"""
    def scope(method="POST", client=("127.0.0.1", 1), **headers):
        hs = {"host": "127.0.0.1:8790", "content-type": "application/json", **headers}
        return {"type": "http", "method": method, "path": "/plugin/v1/register", "client": client,
                "headers": [(k.encode(), v.encode()) for k, v in hs.items() if v is not None]}

    gate = limits.PluginGateMiddleware(app=None)
    listed = limits.PluginGateMiddleware(app=None, extension_ids=(EXT_ID.upper(), " ", ""))
    assert gate.refusal(scope()) is None and listed.extension_ids == {EXT_ID}
    assert gate.refusal(scope(client=None)) == limits.PLUGIN_REMOTE_MESSAGE
    assert gate.refusal(scope(client=("192.0.2.10", 1), host="evil.example", origin="http://evil.example",
                              **{"content-type": None})) == limits.PLUGIN_REMOTE_MESSAGE
    assert gate.refusal(scope(client=("192.0.2.10", 1), **{"x-forwarded-for": "127.0.0.1"})) == \
        limits.PLUGIN_REMOTE_MESSAGE                                   # 转发头说它来自本机也不信：只看套接字对端
    assert gate.refusal(scope(host="evil.example", origin="http://evil.example", via="1.1 demo-proxy",
                              **{"content-type": None})) == limits.PLUGIN_PROXIED_MESSAGE
    assert gate.refusal(scope(host="evil.example", origin="http://evil.example",
                              **{"content-type": None})) == limits.PLUGIN_HOST_MESSAGE
    assert gate.refusal(scope(host=None)) == limits.PLUGIN_HOST_MESSAGE
    assert gate.refusal(scope(origin="http://evil.example", **{"content-type": None})) == limits.PLUGIN_WEB_ORIGIN_MESSAGE
    assert listed.refusal(scope(**{"content-type": None})) == limits.PLUGIN_EXTENSION_MESSAGE
    assert listed.refusal(scope(origin=f"chrome-extension://{EXT_ID}", **{"content-type": None})) == \
        limits.PLUGIN_JSON_MESSAGE
    assert listed.refusal(scope(origin=f"chrome-extension://{EXT_ID}")) is None
    assert gate.refusal(scope(method="GET", **{"content-type": None})) is None
    assert limits.is_plugin_path("/plugin/v1/register") and not limits.is_plugin_path("/api/v1/plugin/x")
    assert not limits.is_plugin_path("/pluginx/v1") and not limits.is_plugin_path("/plugin")


#: urlsplit 解析不了的 Origin（方括号不配对、方括号里不是 IP 地址）
MALFORMED_ORIGINS = ["http://[::1", "chrome-extension://[", "https://[zz]", "HTTP://[", "chrome-extension://[::1",
                     "//[x"]


@pytest.mark.parametrize("origin", MALFORMED_ORIGINS)
def test_plugin_gate_refuses_an_unparsable_origin_with_403_not_500(owner, api, origin):
    """Origin 是对端随便填的：解析不了的和网页来源一样 403（以前 urlsplit 的 ValueError 直接变成 500）。每一个
    /plugin 接口都一样，包括不要令牌的登记；什么都没写进库。"""
    dev = register_device(plugin_client(api), create_account(owner))
    before = _devices()
    c = local_client(api, headers=JSON, raise_server_exceptions=False)
    for path in _plugin_posts(api):
        _forbidden(c.post(path, json=_register_body(), headers={"origin": origin, **bearer(dev["token"])}),
                   limits.PLUGIN_WEB_ORIGIN_MESSAGE)
    _forbidden(c.post("/plugin/v1/register", json=_register_body(), headers={"origin": origin}),
               limits.PLUGIN_WEB_ORIGIN_MESSAGE)
    _forbidden(c.options("/plugin/v1/register", headers={"origin": origin}), limits.PLUGIN_WEB_ORIGIN_MESSAGE)
    assert _devices() == before


@pytest.mark.parametrize("origin", MALFORMED_ORIGINS)
def test_api_writes_refuse_an_unparsable_origin_with_403_not_500(owner, api, origin):
    """/api 的写请求（含不要登录的首次设置与登录）：解析不了的 Origin 按「来源不符」403；读请求不看 Origin。"""
    c = local_client(api, headers={"X-Requested-With": "pc-console"}, raise_server_exceptions=False)
    for path, body in [("/api/v1/auth/login", {"username": OWNER["username"], "password": OWNER["password"]}),
                       ("/api/v1/auth/setup", OWNER),
                       ("/api/v1/accounts/discovered/ignore", {"customer_id": "A1DEMO000404"})]:
        r = c.post(path, json=body, headers={"origin": origin})
        assert err(r, 403, "FORBIDDEN")["message"] == limits.FORBIDDEN_ORIGIN_MESSAGE, (path, r.text)
    r = owner.post("/api/v1/accounts/discovered/ignore", json={"customer_id": "A1DEMO000404"},
                   headers={"origin": origin})
    assert err(r, 403, "FORBIDDEN")["message"] == limits.FORBIDDEN_ORIGIN_MESSAGE     # 登录着也一样
    assert ok(owner.get("/api/v1/accounts/discovered", params={"include_ignored": 1}))["items"] == []   # 没有写进去
    assert ok(c.get("/api/v1/auth/state", headers={"origin": origin})) == {"needs_setup": False}


def test_unparsable_origin_in_the_gate_functions():
    """纯函数：解析不了的 Origin 在入口闸里等同网页来源（先于扩展 ID 名单），取扩展 ID 时等同「不是扩展来源」。"""
    def scope(origin):
        hs = {"host": "127.0.0.1:8790", "content-type": "application/json", "origin": origin}
        return {"type": "http", "method": "POST", "path": "/plugin/v1/register", "client": ("127.0.0.1", 1),
                "headers": [(k.encode(), v.encode()) for k, v in hs.items()]}

    gate = limits.PluginGateMiddleware(app=None)
    listed = limits.PluginGateMiddleware(app=None, extension_ids=(EXT_ID,))
    for origin in MALFORMED_ORIGINS:
        assert limits._split_origin(origin) is None, origin
        assert limits._extension_id(origin) is None, origin
        assert gate.refusal(scope(origin)) == limits.PLUGIN_WEB_ORIGIN_MESSAGE, origin
        assert listed.refusal(scope(origin)) == limits.PLUGIN_WEB_ORIGIN_MESSAGE, origin
    # 解析得了的照旧
    assert limits._extension_id(f"chrome-extension://{EXT_ID.upper()}") == EXT_ID
    assert gate.refusal(scope(f"chrome-extension://{EXT_ID}")) is None
    assert listed.refusal(scope(f"chrome-extension://{OTHER_EXT_ID}")) == limits.PLUGIN_EXTENSION_MESSAGE
    assert gate.refusal(scope("http://[::1]:8790")) == limits.PLUGIN_WEB_ORIGIN_MESSAGE


def _raw(body: dict) -> bytes:
    """输入：请求体 → 输出：纯 ASCII 的 JSON 字节（NUL 写成 \\u0000、不成对的代理项写成 \\ud800 这样的转义；
    httpx 的 json= 会直接按 UTF-8 编码，遇到代理项在客户端就失败了）。"""
    return json.dumps(body).encode("ascii")


#: PostgreSQL 存不了的字符：NUL，以及不成对的代理项（高位、低位各一个）
UNSTORABLE = ["\x00", "\ud800", "\udfff"]


@pytest.mark.parametrize("bad", UNSTORABLE)
def test_unstorable_characters_from_the_plugin_are_dropped_not_500(owner, api, bad):
    """插件送来的文本（版本、UA 之外还有身份里的 site、page_url）带 NUL 或不成对的代理项：去掉坏字符照常处理。
    以前它们原样进了 SQL 参数，PostgreSQL 拒收，请求变成 500——包括不要令牌的 /plugin/v1/register。"""
    acc = create_account(owner)
    c = local_client(api, headers=JSON, raise_server_exceptions=False)
    token = device_token()
    reg = ok(c.post("/plugin/v1/register", content=_raw({"device_token": token, "ext_version": f"0.2.0{bad}"})))
    assert reg["created"] is True
    assert ok(c.post("/plugin/v1/register", content=_raw({"device_token": device_token(),
                                                          "ext_version": bad})))["created"] is True
    h = bearer(token)
    ok(c.post("/plugin/v1/heartbeat", content=_raw({"ext_version": f"a{bad}b"}), headers=h))
    ident = {"signed_in": True, "customer_id": acc["amazon_customer_id"], "site": "US", "conflict": False,
             "page_url": "https://www.amazon.com/gp/css/order-history"}
    for over in ({"site": bad}, {"site": f"U{bad}S"}, {"page_url": f"https://www.amazon.com/a{bad}b?x=1"},
                 {"page_url": bad}):
        out = ok(c.post("/plugin/v1/identity", content=_raw({**ident, **over, "ext_version": "0.2.0"}), headers=h))
        assert out["login"]["state"] == "ready", over                    # 契约 §5：只有身份不是对象才是 422
        out = ok(c.post("/plugin/v1/heartbeat", content=_raw({"identity": {**ident, **over}}), headers=h))
        assert out["login"]["customer_id"] == acc["amazon_customer_id"], over
    with tx() as conn:
        row = conn.execute("SELECT ext_version, identity FROM procure.devices WHERE id = %s",
                           (reg["device_id"],)).fetchone()
    assert row["ext_version"] == "0.2.0" and row["identity"]["page_url"] is None and row["identity"]["site"] == "US"
    # 买家号 ID 里带坏字符：照旧当作没读到（不是把坏字符去掉后凑成一个合格的 ID）
    out = ok(c.post("/plugin/v1/identity", content=_raw({**ident, "customer_id": f"A1DEMO0000{bad}01"}), headers=h))
    assert out["login"]["state"] == "no_id"
    assert ok(owner.get(f"/api/v1/devices/{reg['device_id']}"))["ext_version"] == "0.2.0"


@pytest.mark.parametrize("bad", UNSTORABLE)
def test_outdated_version_with_unstorable_characters_is_refused_not_stored(owner, api, bad):
    """过旧的版本号后面跟着坏字符：闸门按会存下来的版本（去掉坏字符后）判定 → 409 PLUGIN_OUTDATED，detail 写得进
    JSON；以前原文解析不了就放行，浏览器以「0.1.0」登记进来。"""
    c = local_client(api, headers=JSON, raise_server_exceptions=False)
    token = device_token()
    body = err(c.post("/plugin/v1/register", content=_raw({"device_token": token, "ext_version": f"0.1.0{bad}"})),
               409, "PLUGIN_OUTDATED")
    assert body["detail"] == {"ext_version": "0.1.0", "min_ext_version": "0.2.0"}
    reg = ok(c.post("/plugin/v1/register", content=_raw({"device_token": token, "ext_version": "0.2.0"})))
    assert reg["created"] is True                                         # 刚才那次什么也没登记
    h = bearer(token)
    for path, extra in [("/plugin/v1/heartbeat", {}), ("/plugin/v1/identity", identity(None))]:
        body = err(c.post(path, content=_raw({**extra, "ext_version": f"{bad}0.1.0"}), headers=h), 409, "PLUGIN_OUTDATED")
        assert body["detail"]["ext_version"] == "0.1.0", path
    assert ok(owner.get(f"/api/v1/devices/{reg['device_id']}"))["ext_version"] == "0.2.0"


@pytest.mark.parametrize("bad", UNSTORABLE)
def test_unstorable_characters_in_the_execution_protocol_are_not_500(owner, api, bad):
    """执行中的每一步（开始批次、进度、护栏、等待验证、结果、结束批次）：请求里带 NUL 或不成对的代理项，要么按
    参数错误 / 护栏拦截回答，要么去掉坏字符照常处理——没有一步是 500，回执收得下来。"""
    acc = create_account(owner)
    import_orders(owner, [import_row("PO-DEMO-NUL-1"), import_row("PO-DEMO-NUL-2")])
    plugin = plugin_client(api)
    dev = register_device(plugin, acc)
    h = bearer(dev["token"])
    c = local_client(api, headers=JSON, raise_server_exceptions=False)

    def post(path, body):
        return c.post(path, content=_raw(body), headers=h)

    # 开始批次的参数：形状不对就是 422
    for over, field in [({"customer_id": f"A1DEMO{bad}000001"}, "customer_id"), ({"trigger": f"manual{bad}"}, "trigger"),
                        ({"trigger": "scheduled", "slot": f"09:30{bad}"}, "slot")]:
        assert err(post("/plugin/v1/runs", {**run_body(acc), **over}), 422, "VALIDATION")["detail"]["field"] == field
    run = ok(post("/plugin/v1/runs", run_body(acc)))
    a1 = ok(post(f"/plugin/v1/runs/{run['run_id']}/next", {}))["attempt"]
    base = f"/plugin/v1/attempts/{a1['attempt_id']}"
    assert ok(post(f"{base}/progress", {"stage": f"check{bad}out", "detail": {f"k{bad}": f"v{bad}"}}))["continue"] is True
    err(post(f"{base}/progress", {"stage": bad}), 422, "VALIDATION")
    facts = good_facts(a1)
    passed = ok(post(f"{base}/guard", {**facts, "delivery": {**facts["delivery"], "text": f"Arriving 😀{bad}"}}))
    assert passed["verdict"] == "pass"
    ok(post(f"{base}/submitting", {}))
    assert ok(post(f"{base}/verifying", {"note": f"3DS{bad}"}))["attempt_status"] == "verifying"
    done = ok(post(f"{base}/result", {"outcome": "succeeded", "amazon_order_no": f"SIM-20261031-{a1['attempt_id']}{bad}",
                                      "error_detail": f"备注{bad}", "delivery": {"text": f"Arriving 😀{bad}"}}))
    assert done == {"attempt_status": "succeeded", "order_status": "purchased"}
    # 第二笔：要比对的值带坏字符 → 护栏拦截（不是把字符去掉再放行），拦截原因写得进响应
    a2 = ok(post(f"/plugin/v1/runs/{run['run_id']}/next", {}))["attempt"]
    facts2 = good_facts(a2)
    blocked = ok(post(f"/plugin/v1/attempts/{a2['attempt_id']}/guard", {**facts2, "currency": f"US{bad}D"}))
    assert blocked["verdict"] == "block" and blocked["code"]
    ended = ok(post(f"/plugin/v1/runs/{run['run_id']}/finish", {"reason": f"fin{bad}ished", "detail": {"a": f"b{bad}"}}))
    assert ended["end_reason"] == "finished"
    orders = {no: find_order(owner, no)["status"] for no in ("PO-DEMO-NUL-1", "PO-DEMO-NUL-2")}
    assert sorted(orders.values()) == ["exception", "purchased"]
    with tx() as conn:
        row = conn.execute("SELECT amazon_order_no, delivery_text, stage FROM procure.purchase_attempts WHERE id = %s",
                           (a1["attempt_id"],)).fetchone()
    assert row["amazon_order_no"] == f"SIM-20261031-{a1['attempt_id']}" and row["delivery_text"] == "Arriving 😀"


@pytest.mark.parametrize("page_url", ["http://[::1", "https://[amazon]/gp/css/order-history", "https://[zz]/x",
                                      "http://[", "//[x", "https://www.amazon.com]/x"])
def test_unparsable_page_url_is_treated_as_missing_not_500(owner, api, page_url):
    """身份里的 page_url 解析不了：当作没有，身份其余部分照常存下，最近联系时间照常更新（以前整个请求 500、什么都没记）。"""
    acc = create_account(owner)
    c = local_client(api, headers=JSON, raise_server_exceptions=False)
    dev = register_device(plugin_client(api))
    h = bearer(dev["token"])
    clock.freeze(NOW + timedelta(seconds=60))
    ident = {"signed_in": True, "customer_id": acc["amazon_customer_id"], "site": "US", "page_url": page_url}
    hb = ok(c.post("/plugin/v1/heartbeat", json={"ext_version": "0.2.0", "identity": ident}, headers=h))
    assert hb["login"]["state"] == "ready" and hb["account"]["id"] == acc["id"]
    assert ok(c.post("/plugin/v1/identity", json={**ident, "ext_version": "0.2.0"}, headers=h))["login"]["state"] == \
        "ready"
    console = ok(owner.get(f"/api/v1/devices/{dev['device_id']}"))
    assert console["last_seen_at"] == (NOW + timedelta(seconds=60)).isoformat()
    with tx() as conn:
        stored = conn.execute("SELECT identity FROM procure.devices WHERE id = %s", (dev["device_id"],)).fetchone()
    assert stored["identity"]["page_url"] is None and stored["identity"]["customer_id"] == acc["amazon_customer_id"]


def test_page_url_is_stored_without_userinfo_and_only_for_http(owner, api):
    """page_url 只留 http / https 的 origin + path：用户名密码、查询串、片段不存；别的协议当作没有。"""
    acc = create_account(owner)
    plugin = plugin_client(api)
    dev = register_device(plugin)
    h = bearer(dev["token"])
    for sent, kept in [("https://demo-user:demo-pass@www.amazon.com/p?q=1#f", "https://www.amazon.com/p"),
                       ("javascript:alert(1)", None), ("chrome-extension://abcdefghijklmnop/popup.html?x=1", None),
                       ("/gp/css?x=1", None), ("HTTPS://WWW.AMAZON.COM:8443/gp/buy", "https://www.amazon.com:8443/gp/buy")]:
        ok(plugin.post("/plugin/v1/identity", json={**identity(acc["amazon_customer_id"]), "page_url": sent},
                       headers=h))
        with tx() as conn:
            got = conn.execute("SELECT identity->>'page_url' AS u FROM procure.devices WHERE id = %s",
                               (dev["device_id"],)).fetchone()["u"]
        assert got == kept, sent


@pytest.mark.parametrize("name", ["a\x00b", "\x00", "两行\n名称", "制表\t符", "代理\ud800项"])
def test_device_name_with_control_characters_is_422_not_500(owner, api, name):
    """控制台改名：名称含控制字符（NUL、换行、制表符 …）或存不了的代理项 → 422 VALIDATION（field = name），名称不变。"""
    dev = register_device(plugin_client(api))
    c = local_client(api, headers={"X-Requested-With": "pc-console", **JSON}, cookies=dict(owner.cookies),
                     raise_server_exceptions=False)
    body = err(c.patch(f"/api/v1/devices/{dev['device_id']}", content=_raw({"name": name})), 422, "VALIDATION")
    assert body["message"] == "浏览器名称不能包含换行、制表符等控制字符" and body["detail"] == {"field": "name"}
    assert ok(owner.get(f"/api/v1/devices/{dev['device_id']}"))["name"] == dev["register"]["device_name"]
    renamed = ok(owner.patch(f"/api/v1/devices/{dev['device_id']}", json={"name": " Mac-DEMO 一号 "}))
    assert renamed["name"] == "Mac-DEMO 一号"


def test_production_app_does_not_accept_testserver_host(monkeypatch, clean_db):
    from server.app import create_app

    c = TestClient(create_app(web_dist=None))
    assert c.get("/healthz").status_code == 403
    monkeypatch.setattr(settings, "_loaded", dataclasses.replace(settings.get(), allowed_hosts=("console.demo.lan",)))
    c2 = TestClient(create_app(web_dist=None))
    assert c2.get("/healthz", headers={"host": "console.demo.lan:8790"}).status_code == 200


@pytest.mark.parametrize("path", ["//etc/passwd"])
def test_absolute_path_probe_returns_spa_not_file(api, path):
    c = console_client(api)
    r = c.get(path)
    assert r.status_code in (200, 404) and "root:" not in r.text
