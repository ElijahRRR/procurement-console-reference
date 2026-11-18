"""HTTP 入口的闸：/plugin 入口闸（本机对端、本机 Host、不接受网页 Origin、扩展 ID 名单、POST 必须是 JSON）、
登录失败限速、浏览器登记数量上限、请求体大小（含分块上传）、Host 与 Origin 检查（防 DNS 重绑定）。全部数据虚构。"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from registry import settings
from server import limits
from services import clock
from tests import factories as f
from tests.api_helpers import (JSON, NOW, OWNER, api, bearer, console_client, create_account, err,  # noqa: F401
                               local_client, ok, owner, plugin_client, register_device, remote_client, run_body, tx)
from tests.factories_exec import device_token

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
    数量由 settings.max_devices 封顶，满了 → 429 RATE_LIMITED；已登记的令牌照常幂等。"""
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
    assert body["message"] == "登记的浏览器太多：请稍后再试"
    again = ok(c.post("/plugin/v1/register", json={"device_token": first["token"], "ext_version": "0.2.0"}))
    assert again["device_id"] == first["device_id"] and again["created"] is False
    assert ok(owner.get("/api/v1/devices"))["total"] == 2


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
    """纯函数：按「对端 → Host → Origin → 扩展 ID → Content-Type」的顺序给出第一个不满足的原因。"""
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
