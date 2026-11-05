"""HTTP：飞书接入（假客户端 tests/fake_feishu）——状态与系统字段、解析链接、字段、样本预览（只读）、建连接、立即读取、
写回验收、读取记录、停用；读飞书失败时失败记录仍保留；未配置应用 → 409 FEISHU_NOT_CONFIGURED（文件导入不受影响）；
写回队列：人工核实已下单 → 入队（回填待验证）→ 列表 → 重试。全部数据虚构（假表 token、假记录）。"""

from __future__ import annotations

import dataclasses

import pytest

from registry import settings
from services import feishu_connections as fc
from tests import factories as f
from tests import fake_feishu as ff
from tests.api_helpers import (api, create_account, err, find_order, import_orders, import_row, ok, owner,  # noqa: F401
                               tx, view_nos)


@pytest.fixture()
def fake(monkeypatch):
    client = ff.FakeFeishuClient.with_order_table()
    monkeypatch.setattr(fc, "make_client", lambda: client)
    return client


@pytest.fixture()
def not_configured(monkeypatch):
    patched = dataclasses.replace(settings.get(), feishu_app_id=None, feishu_app_secret=None)
    monkeypatch.setattr(settings, "_loaded", patched)
    return patched


def _connection_body(client, **over):
    data = ff.demo_connection_data(client, **over)
    return data


def test_status_and_system_fields(owner):
    st = ok(owner.get("/api/v1/feishu/status"))
    assert set(st) == {"configured"} and isinstance(st["configured"], bool)
    sf = ok(owner.get("/api/v1/feishu/system-fields"))
    keys = {x["key"] for x in sf["source_fields"]}
    assert "upstream_order_no" in keys and all({"key", "label", "required"} <= set(x) for x in sf["source_fields"])
    assert [x for x in sf["source_fields"] if x["required"]] == [
        {"key": "upstream_order_no", "label": "平台订单号", "required": True}]
    assert {"amazon_order_no", "purchase_status"} <= {x["key"] for x in sf["result_fields"]}


def test_resolve_link_fields_preview_create_sync_verify_and_runs(owner, fake):
    create_account(owner)
    fake.add_record(ff.DEMO_APP, ff.DEMO_TABLE, "recDEMO0001", ff.order_fields("PO-DEMO-FS001"))
    fake.add_record(ff.DEMO_APP, ff.DEMO_TABLE, "recDEMO0002", ff.order_fields("PO-DEMO-FS002", 人工审核=None))
    fake.add_record(ff.DEMO_APP, ff.DEMO_TABLE, "recDEMO0003", ff.order_fields("PO-DEMO-FS003", 采购方="外采购DEMO"))

    link = ok(owner.post("/api/v1/feishu/resolve-link", json={"link": ff.DEMO_LINK}))
    assert link["app_token"] == ff.DEMO_APP and link["table_id"] == ff.DEMO_TABLE and link["view_id"] == ff.DEMO_VIEW
    assert {"table_id": ff.DEMO_TABLE, "name": "采购订单DEMO"} in link["tables"]
    err(owner.post("/api/v1/feishu/resolve-link", json={"link": "https://example.invalid/not-feishu"}), 422,
        "VALIDATION")

    fields = ok(owner.post("/api/v1/feishu/fields", json={"app_token": ff.DEMO_APP, "table_id": ff.DEMO_TABLE}))
    by_name = {x["field_name"]: x for x in fields["fields"]}
    assert by_name["总成本USD"]["writable"] is False and by_name["AMZ订单号"]["writable"] is True
    assert fields["suggested_field_map"]["upstream_order_no"] == "平台订单号"
    assert "总成本USD" not in fields["suggested_writeback_map"].values()

    pv = ok(owner.post("/api/v1/feishu/preview", json={
        "app_token": ff.DEMO_APP, "table_id": ff.DEMO_TABLE, "view_id": None,
        "field_map": fields["suggested_field_map"], "admission": {"purchaser_values": [ff.DEMO_PURCHASER]},
        "limit": 20}))
    verdicts = {r["record_id"]: r["verdict"] for r in pv["records"]}
    assert verdicts == {"recDEMO0001": "new", "recDEMO0002": "skipped", "recDEMO0003": "skipped"}
    assert pv["stats"]["read"] == 3 and pv["stats"]["new"] == 1
    assert ok(owner.get("/api/v1/orders"))["total"] == 0 and fake.updates == []     # 预览只读

    created = ok(owner.post("/api/v1/feishu/connections", json=_connection_body(fake)))
    cid = created["id"]
    assert created["writeback_state"] == "unverified" and created["enabled"] is True
    assert created["app_token"] == ff.DEMO_APP and created["source_table_id"] == ff.DEMO_TABLE
    dup = err(owner.post("/api/v1/feishu/connections", json=_connection_body(fake)), 409, "CONFLICT")
    assert dup["detail"]["connection_id"] == cid

    sync = ok(owner.post(f"/api/v1/feishu/connections/{cid}/sync"))
    assert sync["read"] == 3 and sync["new"] == 1 and sync["skipped"] == 2 and sync["run_id"]
    assert view_nos(owner, "today") == ["PO-DEMO-FS001"]
    row = find_order(owner, "PO-DEMO-FS001")
    assert row["primary_source_kind"] == "feishu" and row["price_cap"] == "40.00"

    runs = ok(owner.get(f"/api/v1/feishu/connections/{cid}/runs"))
    assert runs["total"] == 1 and runs["rows"][0]["ok"] is True and runs["rows"][0]["trigger"] == "manual"

    fake.add_record(ff.DEMO_APP, ff.DEMO_TABLE, "recDEMOTEST", ff.order_fields("PO-DEMO-FSTEST", 采购方="外采购DEMO"))
    ver = ok(owner.post(f"/api/v1/feishu/connections/{cid}/verify-writeback", json={"record_id": "recDEMOTEST"}))
    assert ver["ok"] is True and ver["mismatches"] == [] and ver["restored"] is True
    assert ver["connection"]["writeback_state"] == "active"
    got = ok(owner.get(f"/api/v1/feishu/connections/{cid}"))
    assert got["writeback_verified_at"] and got["writeback_test_record"] == "recDEMOTEST"
    assert got["last_stats"]["new"] == 1

    listing = ok(owner.get("/api/v1/feishu/connections"))
    assert listing["total"] == 1 and listing["rows"][0]["id"] == cid
    patched = ok(owner.patch(f"/api/v1/feishu/connections/{cid}", json={"name": "DEMO 订单中心（改名）", "poll_minutes": 5}))
    assert patched["name"] == "DEMO 订单中心（改名）" and patched["poll_minutes"] == 5
    err(owner.patch(f"/api/v1/feishu/connections/{cid}", json={"poll_minutes": 0}), 422, "VALIDATION")
    disabled = ok(owner.delete(f"/api/v1/feishu/connections/{cid}"))
    assert disabled["enabled"] is False and disabled["writeback_state"] == "disabled"
    err(owner.post(f"/api/v1/feishu/connections/{cid}/sync"), 422, "VALIDATION")       # 停用的连接不读取


def test_sync_failure_is_recorded_and_reported(owner, fake):
    cid = ok(owner.post("/api/v1/feishu/connections", json=_connection_body(fake)))["id"]
    fake.fail_next("search_records")
    body = err(owner.post(f"/api/v1/feishu/connections/{cid}/sync"), 502, "FEISHU_ERROR")
    assert body["detail"]["run_id"] and body["detail"]["feishu_code"] == 1254302
    runs = ok(owner.get(f"/api/v1/feishu/connections/{cid}/runs"))["rows"]
    assert len(runs) == 1 and runs[0]["ok"] is False and runs[0]["error"]           # 失败历史没有随回滚丢失
    assert ok(owner.get(f"/api/v1/feishu/connections/{cid}"))["last_error"]


def test_sync_validation_and_internal_failures_are_committed_too(owner, fake, monkeypatch):
    """K14：平台订单号列不见了（VALIDATION）、接收时意外出错（INTERNAL）——都先提交失败的读取记录与 last_error 再返回。"""
    from services import feishu_intake

    cid = ok(owner.post("/api/v1/feishu/connections", json=_connection_body(fake)))["id"]
    table = fake.apps[ff.DEMO_APP][ff.DEMO_TABLE]
    table["fields"] = [x for x in table["fields"] if x["field_name"] != "平台订单号"]
    body = err(owner.post(f"/api/v1/feishu/connections/{cid}/sync"), 422, "VALIDATION")
    assert body["detail"]["run_id"] and "平台订单号" in body["message"]
    got = ok(owner.get(f"/api/v1/feishu/connections/{cid}"))
    assert got["last_error"] == body["message"] and got["last_sync_at"]

    def boom(*a, **kw):
        raise RuntimeError("DEMO")

    fake2 = ff.FakeFeishuClient.with_order_table()
    monkeypatch.setattr(fc, "make_client", lambda: fake2)
    monkeypatch.setattr(feishu_intake, "evaluate_records", boom)
    body = err(owner.post(f"/api/v1/feishu/connections/{cid}/sync"), 500, "INTERNAL")
    assert body["detail"]["run_id"]
    runs = ok(owner.get(f"/api/v1/feishu/connections/{cid}/runs"))["rows"]
    assert [r["ok"] for r in runs] == [False, False] and "RuntimeError" in runs[0]["error"]


def test_feishu_routes_without_app_credentials_are_409_and_file_import_still_works(owner, not_configured):
    assert ok(owner.get("/api/v1/feishu/status")) == {"configured": False}
    assert ok(owner.get("/api/v1/feishu/system-fields"))["source_fields"]       # 不需要飞书也能取
    err(owner.post("/api/v1/feishu/resolve-link", json={"link": ff.DEMO_LINK}), 409, "FEISHU_NOT_CONFIGURED")
    err(owner.post("/api/v1/feishu/fields", json={"app_token": ff.DEMO_APP, "table_id": ff.DEMO_TABLE}), 409,
        "FEISHU_NOT_CONFIGURED")
    err(owner.post("/api/v1/feishu/preview", json={"app_token": ff.DEMO_APP, "table_id": ff.DEMO_TABLE,
                                                   "field_map": {"upstream_order_no": "平台订单号"}}),
        409, "FEISHU_NOT_CONFIGURED")
    err(owner.post("/api/v1/feishu/connections", json={"name": "x", "link": ff.DEMO_LINK,
                                                       "field_map": {"upstream_order_no": "平台订单号"}}),
        409, "FEISHU_NOT_CONFIGURED")
    assert ok(owner.get("/api/v1/feishu/connections")) == {"total": 0, "rows": []}
    assert ok(owner.get("/api/v1/overview"))["feishu_configured"] is False
    create_account(owner)
    import_orders(owner, [import_row("PO-DEMO-NF001")])
    assert view_nos(owner, "today") == ["PO-DEMO-NF001"]


def test_writeback_queue_list_and_retry(owner, fake):
    acc = create_account(owner)
    fake.add_record(ff.DEMO_APP, ff.DEMO_TABLE, "recDEMOW001", ff.order_fields("PO-DEMO-WB001"))
    cid = ok(owner.post("/api/v1/feishu/connections", json=_connection_body(fake)))["id"]
    ok(owner.post(f"/api/v1/feishu/connections/{cid}/sync"))
    oid = find_order(owner, "PO-DEMO-WB001")["id"]
    with tx() as conn:   # 造一笔「已登记提交、结果不明」的真实尝试（接口里只能模拟执行，模拟结果永不写回）
        ws = conn.execute("SELECT workspace_id FROM procure.purchase_orders WHERE id = %s", (oid,)).fetchone()
        att = f.make_attempt(conn, ws["workspace_id"], oid, acc["id"], status="unknown", simulated=False,
                             submitted=True)
        f.set_status(conn, oid, "result_unknown", current_attempt_id=att["id"])
    d = ok(owner.post(f"/api/v1/orders/{oid}/resolve-unknown", json={
        "outcome": "ordered", "amazon_order_no": "111-0000000-0000123",
        "amounts": {"total": "44.49", "tax": "2.52"}, "note": "DEMO 人工核实"}))
    assert d["status"] == "purchased"

    wbs = ok(owner.get("/api/v1/writebacks"))
    assert wbs["total"] == 1
    wb = wbs["rows"][0]
    assert wb["status"] == "pending" and wb["waiting_verification"] is True
    assert wb["upstream_order_no"] == "PO-DEMO-WB001" and wb["fields"]["AMZ订单号"] == "111-0000000-0000123"
    assert ok(owner.get("/api/v1/writebacks", params={"status": "done"}))["total"] == 0
    assert ok(owner.get(f"/api/v1/writebacks/{wb['id']}"))["id"] == wb["id"]

    with tx() as conn:
        conn.execute("UPDATE procure.writebacks SET status = 'failed', tries = 10, last_error = 'DEMO' WHERE id = %s",
                     (wb["id"],))
    again = ok(owner.post(f"/api/v1/writebacks/{wb['id']}/retry"))
    assert again["status"] == "pending" and again["tries"] == 0 and again["last_error"] is None
