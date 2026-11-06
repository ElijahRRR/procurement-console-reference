"""HTTP 入口的闸：失败限速（登录 / 配对）、请求体大小（含分块上传）、Host 与 Origin 检查（防 DNS 重绑定）。"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from registry import settings
from services import clock
from tests.api_helpers import NOW, OWNER, api, console_client, err, ok, owner  # noqa: F401


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


def test_pair_failures_are_rate_limited_per_ip(api):
    c = TestClient(api)
    for _ in range(20):
        r = c.post("/plugin/v1/pair", json={"code": "ZZZZZZZZ", "device_name": "Mac-DEMO", "browser": "chrome",
                                             "ext_version": "0.1.0"})
        assert r.status_code in (401, 404, 409, 422), r.text
    r = c.post("/plugin/v1/pair", json={"code": "ZZZZZZZZ", "device_name": "Mac-DEMO", "browser": "chrome",
                                         "ext_version": "0.1.0"})
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED" and r.headers["retry-after"]


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

    r = c.post("/plugin/v1/pair", content=chunks(2 * 1024 * 1024), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "VALIDATION"
    r = c.post("/api/v1/imports", content=chunks(11 * 1024 * 1024),
               headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413 and "10 MB" in r.json()["message"]
    r = c.post("/api/v1/auth/login", content=b"x" * (1024 * 1024 + 1), headers={"content-type": "application/json"})
    assert r.status_code == 413
    # 正常大小照常处理
    err(c.post("/api/v1/auth/login", json={"username": "x", "password": "y"}), 401, "UNAUTHORIZED")


def test_foreign_host_and_origin_are_refused(api):
    """DNS 重绑定：Host 不是本机地址 → 403（包括首次设置、登录与配对）；/api 写请求的 Origin 不符 → 403。"""
    c = console_client(api)
    for path in ("/api/v1/auth/setup", "/api/v1/auth/login", "/plugin/v1/pair"):
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
    # 插件从扩展 service worker 调用：Origin 是 chrome-extension://，/plugin 不检查 Origin
    r = c.post("/plugin/v1/pair", json={"code": "ZZZZZZZZ", "device_name": "Mac-DEMO", "browser": "chrome",
                                         "ext_version": "0.1.0"},
               headers={"host": "127.0.0.1:8790", "origin": "chrome-extension://demoextensionid"})
    assert r.status_code != 403


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
