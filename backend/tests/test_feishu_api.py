"""api/feishu：令牌缓存与刷新、分页、重试、错误、分批写入（httpx.MockTransport，不连真实飞书）。"""

from __future__ import annotations

import json

import httpx
import pytest

from api.feishu import FeishuClient, FeishuError

APP = "basDEMO0000000000000000001"
TABLE = "tblDEMO000000001"


class FakeFeishu:
    """按路径分派的假飞书：记录每个请求；handlers[path] 是返回 httpx.Response 的函数或响应队列。"""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.token_seq = 0
        self.handlers: dict[str, object] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            self.token_calls += 1
            self.token_seq += 1
            handler = self.handlers.get(path)
            if handler is not None:
                return handler(request)
            return httpx.Response(200, json={"code": 0, "msg": "ok",
                                             "tenant_access_token": f"t-demo-{self.token_seq}", "expire": 7200})
        handler = self.handlers.get(path)
        if handler is None:
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        if isinstance(handler, list):
            item = handler.pop(0)
            return item(request) if callable(item) else item
        return handler(request)

    def data_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if "tenant_access_token" not in r.url.path]


def ok(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "success", "data": data})


@pytest.fixture()
def fake():
    return FakeFeishu()


@pytest.fixture()
def sleeps():
    return []


@pytest.fixture()
def client(fake, sleeps):
    c = FeishuClient("cli_demo", "secret_demo", transport=httpx.MockTransport(fake), min_interval_s=0,
                     sleep=sleeps.append)
    yield c
    c.close()


def tables_path() -> str:
    return f"/open-apis/bitable/v1/apps/{APP}/tables"


def search_path() -> str:
    return f"/open-apis/bitable/v1/apps/{APP}/tables/{TABLE}/records/search"


# ── 认证 ─────────────────────────────────────────────────────────────────────


def test_token_request_shape_and_cache(client, fake):
    fake.handlers[tables_path()] = lambda r: ok({"items": [{"table_id": TABLE, "name": "采购订单", "revision": 3}],
                                                 "has_more": False})
    assert client.list_tables(APP) == [{"table_id": TABLE, "name": "采购订单"}]
    assert client.list_tables(APP) == [{"table_id": TABLE, "name": "采购订单"}]
    assert fake.token_calls == 1
    token_req = fake.requests[0]
    assert token_req.method == "POST"
    assert json.loads(token_req.content) == {"app_id": "cli_demo", "app_secret": "secret_demo"}
    for r in fake.data_requests():
        assert r.headers["Authorization"] == "Bearer t-demo-1"


def test_token_cached_until_expire_minus_300(client, fake):
    now = [1000.0]
    client._clock = lambda: now[0]
    fake.handlers[tables_path()] = lambda r: ok({"items": [], "has_more": False})
    client.list_tables(APP)
    now[0] += 7200 - 301          # 还没到提前 300 秒的刷新点
    client.list_tables(APP)
    assert fake.token_calls == 1
    now[0] += 2                   # 过了 expire − 300
    client.list_tables(APP)
    assert fake.token_calls == 2
    assert fake.data_requests()[-1].headers["Authorization"] == "Bearer t-demo-2"


def test_token_invalid_code_refreshes_once_and_resends(client, fake):
    fake.handlers[tables_path()] = [
        httpx.Response(400, json={"code": 99991663, "msg": "Invalid access token for authorization."}),
        ok({"items": [{"table_id": TABLE, "name": "表一"}], "has_more": False}),
    ]
    assert client.list_tables(APP)[0]["table_id"] == TABLE
    assert fake.token_calls == 2
    auths = [r.headers["Authorization"] for r in fake.data_requests()]
    assert auths == ["Bearer t-demo-1", "Bearer t-demo-2"]


def test_token_invalid_twice_raises(client, fake):
    fake.handlers[tables_path()] = lambda r: httpx.Response(400, json={"code": 99991663, "msg": "invalid token"})
    with pytest.raises(FeishuError) as ei:
        client.list_tables(APP)
    assert ei.value.code == 99991663 and ei.value.http_status == 400
    assert fake.token_calls == 2


def test_token_endpoint_error(fake, sleeps):
    fake.handlers["/open-apis/auth/v3/tenant_access_token/internal"] = (
        lambda r: httpx.Response(200, json={"code": 10014, "msg": "app secret invalid"}))
    c = FeishuClient("cli_demo", "wrong", transport=httpx.MockTransport(fake), min_interval_s=0, sleep=sleeps.append)
    with pytest.raises(FeishuError) as ei:
        c.tenant_token()
    assert ei.value.code == 10014
    assert "wrong" not in str(ei.value)          # 密钥不进错误信息


def test_not_configured():
    with pytest.raises(FeishuError) as ei:
        FeishuClient("", "")
    assert ei.value.code == "NOT_CONFIGURED"


# ── 分页 ─────────────────────────────────────────────────────────────────────


def test_search_records_paginates(client, fake):
    pages = {
        None: {"items": [{"record_id": "rec1", "fields": {"平台订单号": [{"type": "text", "text": "PO-DEMO-1"}]}},
                         {"record_id": "rec2", "fields": {}}], "has_more": True, "page_token": "pt-2", "total": 5},
        "pt-2": {"items": [{"record_id": "rec3", "fields": {"数量": 2}}, {"record_id": "rec4"}],
                 "has_more": True, "page_token": "pt-3", "total": 5},
        "pt-3": {"items": [{"record_id": "rec5", "fields": {}}], "has_more": False, "total": 5},
    }
    fake.handlers[search_path()] = lambda r: ok(pages[r.url.params.get("page_token")])
    got = list(client.search_records(APP, TABLE, field_names=["平台订单号", "数量"], view_id="vewDEMO0001"))
    assert [g["record_id"] for g in got] == ["rec1", "rec2", "rec3", "rec4", "rec5"]
    assert got[3]["fields"] == {}
    reqs = fake.data_requests()
    assert len(reqs) == 3
    assert all(r.method == "POST" and r.url.params["page_size"] == "500" for r in reqs)
    assert [r.url.params.get("page_token") for r in reqs] == [None, "pt-2", "pt-3"]
    body = json.loads(reqs[0].content)
    assert body["field_names"] == ["平台订单号", "数量"] and body["view_id"] == "vewDEMO0001"


def test_search_page_size_capped(client, fake):
    fake.handlers[search_path()] = lambda r: ok({"items": [], "has_more": False})
    list(client.search_records(APP, TABLE, page_size=2000))
    assert fake.data_requests()[0].url.params["page_size"] == "500"


def test_pagination_without_token_raises(client, fake):
    fake.handlers[search_path()] = lambda r: ok({"items": [], "has_more": True, "page_token": ""})
    with pytest.raises(FeishuError) as ei:
        list(client.search_records(APP, TABLE))
    assert ei.value.code == "BAD_PAGINATION"


def test_list_fields_paginates_and_shapes(client, fake):
    path = f"/open-apis/bitable/v1/apps/{APP}/tables/{TABLE}/fields"
    pages = {
        None: {"items": [{"field_id": "fld1", "field_name": "平台订单号", "type": 20, "ui_type": "Formula",
                          "is_primary": True, "property": {"formatter": ""}, "description": "x"}],
               "has_more": True, "page_token": "f2"},
        "f2": {"items": [{"field_id": "fld2", "field_name": "限价", "type": 2, "ui_type": "Currency",
                          "property": {"currency_code": "USD"}}], "has_more": False},
    }
    fake.handlers[path] = lambda r: ok(pages[r.url.params.get("page_token")])
    fields = client.list_fields(APP, TABLE)
    assert fields == [
        {"field_id": "fld1", "field_name": "平台订单号", "type": 20, "ui_type": "Formula", "is_primary": True,
         "property": {"formatter": ""}},
        {"field_id": "fld2", "field_name": "限价", "type": 2, "ui_type": "Currency", "is_primary": False,
         "property": {"currency_code": "USD"}},
    ]


def test_get_record_and_wiki_node(client, fake):
    fake.handlers[f"/open-apis/bitable/v1/apps/{APP}/tables/{TABLE}/records/recDEMO1"] = (
        lambda r: ok({"record": {"record_id": "recDEMO1", "fields": {"AMZ订单号": "TEST-DEMO"}}}))
    fake.handlers["/open-apis/wiki/v2/spaces/get_node"] = (
        lambda r: ok({"node": {"space_id": "7000", "node_token": r.url.params["token"], "obj_token": APP,
                               "obj_type": "bitable", "title": "订单中心 DEMO"}}))
    assert client.get_record(APP, TABLE, "recDEMO1") == {"record_id": "recDEMO1", "fields": {"AMZ订单号": "TEST-DEMO"}}
    node = client.wiki_get_node("wikDEMO000000000000000001")
    assert node["obj_token"] == APP and node["obj_type"] == "bitable"
    assert fake.data_requests()[-1].url.params["token"] == "wikDEMO000000000000000001"


# ── 重试与错误 ────────────────────────────────────────────────────────────────


def test_retries_on_429_with_backoff(client, fake, sleeps):
    fake.handlers[tables_path()] = [
        httpx.Response(429, json={"code": 99991400, "msg": "request trigger frequency limit"}),
        httpx.Response(429, text="Too Many Requests"),
        ok({"items": [], "has_more": False}),
    ]
    assert client.list_tables(APP) == []
    assert len(fake.data_requests()) == 3
    assert sleeps == [0.5, 1.0]


def test_429_respects_ratelimit_reset_header(client, fake, sleeps):
    fake.handlers[tables_path()] = [
        httpx.Response(429, headers={"x-ogw-ratelimit-reset": "3"}, json={"code": 99991400, "msg": "limit"}),
        ok({"items": [], "has_more": False}),
    ]
    client.list_tables(APP)
    assert sleeps == [3.0]


def test_retries_on_transient_codes_and_5xx(client, fake, sleeps):
    fake.handlers[tables_path()] = [
        httpx.Response(200, json={"code": 1254290, "msg": "TooManyRequest"}),
        httpx.Response(200, json={"code": 1254291, "msg": "Write conflict"}),
        httpx.Response(502, text="<html>bad gateway</html>"),
        ok({"items": [{"table_id": TABLE, "name": "表"}], "has_more": False}),
    ]
    assert client.list_tables(APP) == [{"table_id": TABLE, "name": "表"}]
    assert sleeps == [0.5, 1.0, 2.0]


def test_retries_exhausted_raises(client, fake, sleeps):
    fake.handlers[tables_path()] = lambda r: httpx.Response(429, json={"code": 99991400, "msg": "limit"})
    with pytest.raises(FeishuError) as ei:
        client.list_tables(APP)
    assert ei.value.code == 99991400 and ei.value.http_status == 429
    assert len(fake.data_requests()) == 4          # 1 次 + 3 次重试
    assert len(sleeps) == 3


def test_network_error_retried_then_raised(fake, sleeps):
    calls = {"n": 0}

    def handler(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-x", "expire": 7200})
        calls["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    c = FeishuClient("cli_demo", "secret_demo", transport=httpx.MockTransport(handler), min_interval_s=0,
                     max_retries=2, sleep=sleeps.append)
    with pytest.raises(FeishuError) as ei:
        c.list_tables(APP)
    assert ei.value.code == "NETWORK" and ei.value.http_status is None
    assert calls["n"] == 3


def test_business_error_not_retried(client, fake, sleeps):
    fake.handlers[tables_path()] = lambda r: httpx.Response(
        403, json={"code": 1254302, "msg": "The role has no permissions."})
    with pytest.raises(FeishuError) as ei:
        client.list_tables(APP)
    assert (ei.value.code, ei.value.msg, ei.value.http_status) == (1254302, "The role has no permissions.", 403)
    assert sleeps == [] and len(fake.data_requests()) == 1


def test_non_json_error(client, fake):
    fake.handlers[tables_path()] = lambda r: httpx.Response(400, text="bad request")
    with pytest.raises(FeishuError) as ei:
        client.list_tables(APP)
    assert ei.value.code == "HTTP_400" and ei.value.http_status == 400


def test_min_interval_throttles(fake, sleeps):
    now = [0.0]
    c = FeishuClient("cli_demo", "secret_demo", transport=httpx.MockTransport(fake), min_interval_s=0.2,
                     sleep=lambda s: (sleeps.append(round(s, 3)), now.__setitem__(0, now[0] + s)))
    c._clock = lambda: now[0]
    fake.handlers[tables_path()] = lambda r: ok({"items": [], "has_more": False})
    c.list_tables(APP)
    c.list_tables(APP)
    assert sleeps and all(s == 0.2 for s in sleeps)


# ── 分批写入 ─────────────────────────────────────────────────────────────────


def test_batch_update_chunks_of_500(client, fake):
    path = f"/open-apis/bitable/v1/apps/{APP}/tables/{TABLE}/records/batch_update"

    def handler(request):
        recs = json.loads(request.content)["records"]
        return ok({"records": [{"record_id": r["record_id"], "fields": r["fields"]} for r in recs]})

    fake.handlers[path] = handler
    records = [{"record_id": f"rec{i}", "fields": {"AMZ订单号": f"TEST-{i}"}} for i in range(1201)]
    out = client.batch_update_records(APP, TABLE, records)
    sizes = [len(json.loads(r.content)["records"]) for r in fake.data_requests()]
    assert sizes == [500, 500, 201]
    assert [r["record_id"] for r in out] == [f"rec{i}" for i in range(1201)]
    assert client.batch_update_records(APP, TABLE, []) == []
    assert len(fake.data_requests()) == 3
