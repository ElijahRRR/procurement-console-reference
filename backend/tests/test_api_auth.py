"""HTTP：首次设置 → 登录 → 当前用户 → 退出；会话 Cookie 属性；防跨站请求头；未登录 401；统一错误体
（参数校验 422、未知接口 404、未预期异常 500 不带堆栈）；健康检查与 OPTIONS。"""

from __future__ import annotations

from tests.api_helpers import OWNER, api, console_client, err, ok, owner  # noqa: F401 - 夹具


def test_first_run_setup_then_login_me_and_logout(api):
    c = console_client(api)
    assert ok(c.get("/api/v1/auth/state")) == {"needs_setup": True}
    err(c.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")

    me = ok(c.post("/api/v1/auth/setup", json=OWNER))
    assert me["user"]["username"] == OWNER["username"] and me["role"] == "owner"
    assert me["workspace"]["name"] == OWNER["workspace_name"] and isinstance(me["workspace"]["id"], int)
    assert ok(c.get("/api/v1/auth/state")) == {"needs_setup": False}
    assert ok(c.get("/api/v1/auth/me")) == me          # 设置成功即登录

    ok(c.post("/api/v1/auth/logout"))
    err(c.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")

    again = console_client(api)
    logged = ok(again.post("/api/v1/auth/login", json={"username": OWNER["username"], "password": OWNER["password"]}))
    assert logged == me
    assert ok(again.get("/api/v1/auth/me"))["user"]["id"] == me["user"]["id"]
    err(c.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")   # 退出的旧会话不会因别人登录而复活


def test_session_cookie_is_httponly_strict_with_max_age(api):
    c = console_client(api)
    resp = c.post("/api/v1/auth/setup", json=OWNER)
    ok(resp)
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("pc_session=")
    low = cookie.lower()
    assert "httponly" in low and "samesite=strict" in low and "max-age=1209600" in low and "path=/" in low
    assert "secure" not in low.replace("samesite", "")          # PC_COOKIE_SECURE 缺省关闭（本机 http）

    out = c.post("/api/v1/auth/logout")
    ok(out)
    assert out.headers["set-cookie"].startswith('pc_session=""') or "max-age=0" in out.headers["set-cookie"].lower()


def test_setup_is_refused_once_an_owner_exists(owner, api):
    late = console_client(api)
    err(late.post("/api/v1/auth/setup", json={**OWNER, "username": "demo_late"}), 403, "FORBIDDEN")


def test_setup_validates_username_and_password(api):
    c = console_client(api)
    body = err(c.post("/api/v1/auth/setup", json={**OWNER, "password": "short"}), 422, "VALIDATION")
    assert body["detail"].get("field") == "password"
    assert ok(c.get("/api/v1/auth/state")) == {"needs_setup": True}   # 失败的设置整体回滚


def test_wrong_password_gives_uniform_401(owner, api):
    c = console_client(api)
    a = err(c.post("/api/v1/auth/login", json={"username": OWNER["username"], "password": "wrong-pass-0"}),
            401, "UNAUTHORIZED")
    b = err(c.post("/api/v1/auth/login", json={"username": "nobody_demo", "password": "wrong-pass-0"}),
            401, "UNAUTHORIZED")
    assert a["message"] == b["message"]          # 不泄露用户名是否存在
    assert "set-cookie" not in c.post("/api/v1/auth/login", json={"username": "x", "password": "y"}).headers


def test_write_request_without_csrf_header_is_forbidden(owner, api):
    bare = console_client(api, csrf=False)
    err(bare.post("/api/v1/auth/login", json={"username": OWNER["username"], "password": OWNER["password"]}),
        403, "FORBIDDEN")
    err(bare.post("/api/v1/auth/setup", json=OWNER), 403, "FORBIDDEN")
    # 已登录的会话也一样：带 Cookie 但不带请求头的写请求被拒绝（防跨站）
    owner.headers.pop("X-Requested-With")
    err(owner.post("/api/v1/allocation/run"), 403, "FORBIDDEN")
    err(owner.patch("/api/v1/settings", json={"default_max_delivery_days": 12}), 403, "FORBIDDEN")
    wrong = owner.post("/api/v1/allocation/run", headers={"X-Requested-With": "XMLHttpRequest"})
    err(wrong, 403, "FORBIDDEN")
    assert ok(owner.get("/api/v1/settings"))["default_max_delivery_days"] == 10   # 读请求不需要请求头


def test_unauthenticated_requests_get_401(api):
    c = console_client(api)
    for path in ("/api/v1/overview", "/api/v1/orders", "/api/v1/orders/1", "/api/v1/accounts", "/api/v1/devices",
                 "/api/v1/settings", "/api/v1/feishu/status", "/api/v1/imports/template.xlsx",
                 "/api/v1/exports/orders.xlsx", "/api/v1/events", "/api/v1/writebacks"):
        err(c.get(path), 401, "UNAUTHORIZED")
    err(c.post("/api/v1/orders/1/cancel", json={"note": "x"}), 401, "UNAUTHORIZED")
    err(c.post("/api/v1/allocation/run"), 401, "UNAUTHORIZED")
    c.cookies.set("pc_session", "not-a-real-session-token")
    err(c.get("/api/v1/auth/me"), 401, "UNAUTHORIZED")


def test_request_validation_errors_use_the_uniform_body(owner):
    body = err(owner.get("/api/v1/orders", params={"page": 0}), 422, "VALIDATION")
    assert body["message"] == "请求参数不正确" and body["detail"]["errors"]
    err(owner.get("/api/v1/orders", params={"page_size": 201}), 422, "VALIDATION")
    err(owner.get("/api/v1/orders/not-a-number"), 422, "VALIDATION")
    err(owner.post("/api/v1/orders/1/review", content=b"{not json", headers={"Content-Type": "application/json"}),
        422, "VALIDATION")
    err(owner.get("/api/v1/orders", params={"view": "nope"}), 422, "VALIDATION")   # 服务层的校验同样是 422


def test_unknown_api_path_is_json_404(owner):
    err(owner.get("/api/v1/no-such-thing"), 404, "NOT_FOUND")
    err(owner.get("/plugin/v1/no-such-thing"), 404, "NOT_FOUND")


def test_unexpected_error_is_500_without_stack_trace(owner, monkeypatch):
    from services import overview

    def boom(conn, workspace_id):
        raise RuntimeError("secret internal detail DEMO")

    monkeypatch.setattr(overview, "overview", boom)
    resp = owner.get("/api/v1/overview")
    body = err(resp, 500, "INTERNAL")
    assert body["message"] == "服务器内部错误"
    assert "secret" not in resp.text and "Traceback" not in resp.text


def test_healthz_and_options(api):
    c = console_client(api, csrf=False)
    assert ok(c.get("/healthz")) == {"ok": True}
    resp = c.options("/api/v1/orders")
    assert resp.status_code == 204 and "POST" in resp.headers["allow"]
    assert c.options("/plugin/v1/heartbeat").status_code == 204


def test_slow_request_does_not_block_other_requests_of_the_same_session(owner, api, monkeypatch):
    import threading
    import time

    from fastapi.testclient import TestClient

    from services import overview

    release = threading.Event()
    entered = threading.Event()
    real = overview.overview

    def slow(conn, workspace_id):
        entered.set()
        release.wait(10)
        return real(conn, workspace_id)

    monkeypatch.setattr(overview, "overview", slow)
    second = TestClient(api, headers=dict(owner.headers), cookies=dict(owner.cookies))
    results: dict[str, int] = {}
    t = threading.Thread(target=lambda: results.setdefault("slow", owner.get("/api/v1/overview").status_code))
    t.start()
    try:
        assert entered.wait(10)
        started = time.monotonic()
        assert ok(second.get("/api/v1/auth/me"))["user"]["username"] == OWNER["username"]
        assert time.monotonic() - started < 5          # 没有排在慢请求后面（会话行锁已在请求开头释放）
    finally:
        release.set()
        t.join(15)
    assert results["slow"] == 200
