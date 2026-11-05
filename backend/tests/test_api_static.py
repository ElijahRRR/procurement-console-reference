"""HTTP：前端托管——有构建目录时 / 与客户端路由回 index.html（单页应用回退）、静态文件按原样返回、/api 与 /plugin 不回退、
不能越出构建目录；没有构建目录时 / 返回 JSON 提示。另测下载文件名的 RFC 5987 编码。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.serialize import content_disposition
from tests.api_helpers import err, ok

INDEX = "<!doctype html><html><head><title>DEMO 控制台</title></head><body><div id=app></div></body></html>"


@pytest.fixture()
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "app-DEMO.js").write_text("console.log('DEMO')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("DEMO secret outside dist", encoding="utf-8")
    return root


def test_spa_fallback_serves_index_and_files(clean_db, dist):
    c = TestClient(create_app(web_dist=dist))
    for path in ("/", "/orders", "/orders/123", "/settings/feishu?tab=1", "/accounts/7/edit"):
        resp = c.get(path)
        assert resp.status_code == 200 and resp.text == INDEX, path
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["cache-control"] == "no-cache"
    js = c.get("/assets/app-DEMO.js")
    assert js.status_code == 200 and js.text == "console.log('DEMO')"
    assert "immutable" in js.headers["cache-control"]
    assert c.get("/favicon.svg").text.startswith("<svg")
    assert c.get("/assets/missing.js").text == INDEX            # 找不到的文件同样回退
    assert c.get("/..%2Fsecret.txt").text == INDEX               # 越出构建目录 → 回退，不泄露
    assert "secret" not in c.get("/assets/..%2F..%2Fsecret.txt").text
    assert ok(c.get("/healthz")) == {"ok": True}


def test_api_and_plugin_paths_never_fall_back_to_index(clean_db, dist):
    c = TestClient(create_app(web_dist=dist))
    err(c.get("/api/v1/does-not-exist"), 404, "NOT_FOUND")
    err(c.get("/api"), 404, "NOT_FOUND")
    err(c.get("/plugin/v1/does-not-exist"), 404, "NOT_FOUND")
    err(c.get("/api/v1/orders"), 401, "UNAUTHORIZED")            # 真正的接口照常工作
    assert c.get("/api/v1/openapi.json").json()["info"]["title"] == "采购控制台"


def test_without_dist_root_returns_json_hint(clean_db, tmp_path):
    c = TestClient(create_app(web_dist=tmp_path / "missing"))
    body = ok(c.get("/"))
    assert body["ok"] is True and "npm run build" in body["message"] and body["api"] == "/api/v1"
    err(c.get("/orders"), 404, "NOT_FOUND")


def test_dist_without_index_is_treated_as_missing(clean_db, tmp_path):
    (tmp_path / "half").mkdir()
    c = TestClient(create_app(web_dist=tmp_path / "half"))
    assert ok(c.get("/"))["ok"] is True


def test_content_disposition_has_ascii_fallback_and_utf8_name():
    v = content_disposition("采购结果-批次3.xlsx")
    assert v.startswith('attachment; filename="')
    fallback = v.split('filename="', 1)[1].split('"', 1)[0]
    assert fallback.isascii() and fallback.endswith(".xlsx")
    assert v.endswith("filename*=UTF-8''%E9%87%87%E8%B4%AD%E7%BB%93%E6%9E%9C-%E6%89%B9%E6%AC%A13.xlsx")
    assert content_disposition("orders.xlsx").startswith('attachment; filename="orders.xlsx"')
