"""飞书连接：未配置、链接解析、字段、建连校验、样本预览只读、改绑、停用、写回验收、跨工作区隔离。全部数据虚构。"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from api.feishu import FeishuClient, FeishuError
from registry import settings
from services import clock, feishu_intake
from services import feishu_connections as fc
from services.errors import DomainError
from tests import factories as f
from tests.fake_feishu import (
    DEMO_APP,
    DEMO_LINK,
    DEMO_PURCHASER,
    DEMO_RESULT_TABLE,
    DEMO_TABLE,
    DEMO_WIKI_LINK,
    FakeFeishuClient,
    create_demo_connection,
    demo_connection_data,
    order_fields,
    text,
)

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


@pytest.fixture()
def fake(monkeypatch):
    client = FakeFeishuClient.with_order_table()
    monkeypatch.setattr(fc, "make_client", lambda: client)
    return client


def _settings_with(monkeypatch, app_id, secret):
    real = settings.get()
    patched = dataclasses.replace(real, feishu_app_id=app_id, feishu_app_secret=secret)
    monkeypatch.setattr(fc.settings, "get", lambda: patched)


# ── 应用凭证 ──────────────────────────────────────────────────────────────────


def test_not_configured_raises_feishu_not_configured(monkeypatch):
    _settings_with(monkeypatch, None, None)
    assert fc.status() == {"configured": False}
    with pytest.raises(DomainError) as e:
        fc.make_client()
    assert e.value.code == "FEISHU_NOT_CONFIGURED" and e.value.http_status == 409
    assert "FEISHU_APP_ID" in e.value.message
    with pytest.raises(DomainError) as e2:          # 不传 client 的函数走同一个钩子
        fc.resolve_link(DEMO_LINK)
    assert e2.value.code == "FEISHU_NOT_CONFIGURED"


def test_configured_settings_build_real_client_without_network(monkeypatch):
    _settings_with(monkeypatch, "cli_demo_app", "demo_secret_value")
    assert fc.status() == {"configured": True}
    client = fc.make_client()
    try:
        assert isinstance(client, FeishuClient)
    finally:
        client.close()


def test_feishu_error_translation_keeps_code_and_hint():
    err = fc.feishu_error(FeishuError(1254302, "Permission denied"), "读取字段")
    assert err.code == "FEISHU_ERROR" and err.http_status == 502
    assert err.message.startswith("读取字段失败：") and "添加文档应用" in err.message and "1254302" in err.message
    assert err.detail["feishu_code"] == 1254302


# ── 链接与字段 ────────────────────────────────────────────────────────────────


def test_resolve_base_and_wiki_links(fake):
    out = fc.resolve_link(DEMO_LINK)
    assert out["app_token"] == DEMO_APP and out["table_id"] == DEMO_TABLE and out["view_id"] == "vewDEMOVIEW01"
    assert out["tables"] == [{"table_id": DEMO_TABLE, "name": "采购订单DEMO"}]
    wiki = fc.resolve_link(DEMO_WIKI_LINK)
    assert wiki["app_token"] == DEMO_APP and wiki["table_id"] == DEMO_TABLE
    assert fake.calls_of("wiki_get_node") == [{"token": "wikcnDEMOWIKI0001"}]


def test_resolve_link_rejects_unknown_table_and_non_bitable_wiki(fake):
    with pytest.raises(DomainError) as e:
        fc.resolve_link(f"https://example-demo.feishu.cn/base/{DEMO_APP}?table=tblDEMONOPE99")
    assert e.value.code == "VALIDATION"
    fake.wiki["wikcnDEMODOC0002"] = {"obj_token": "doxcnDEMO00001", "obj_type": "docx", "title": "DEMO 文档"}
    with pytest.raises(DomainError) as e2:
        fc.resolve_link("https://example-demo.feishu.cn/wiki/wikcnDEMODOC0002")
    assert "不是多维表格" in e2.value.message
    fake.fail_next("list_tables")
    with pytest.raises(DomainError) as e3:
        fc.resolve_link(DEMO_LINK)
    assert e3.value.code == "FEISHU_ERROR" and "添加文档应用" in e3.value.message


def test_fields_marks_formula_unwritable_and_suggests_maps(fake):
    out = fc.fields(DEMO_APP, DEMO_TABLE)
    by_name = {x["field_name"]: x for x in out["fields"]}
    assert by_name["总成本USD"]["writable"] is False and by_name["总成本USD"]["type_zh"] == "公式"
    assert by_name["税前总额USD"]["writable"] is True
    assert by_name["采购状态"]["options"] == ["待拍单", "已拍单", "拿回"]
    assert out["suggested_field_map"]["upstream_order_no"] == "平台订单号"
    assert out["suggested_writeback_map"]["total_before_tax"] == "税前总额USD"
    assert "total" not in out["suggested_writeback_map"]           # 公式列「总成本USD」不建议写回


def test_system_fields_lists_source_and_result_fields():
    out = fc.system_fields()
    keys = {x["key"]: x for x in out["source_fields"]}
    assert keys["upstream_order_no"]["required"] is True and keys["asin"]["required"] is False
    assert [x["key"] for x in out["result_fields"]][:3] == ["buyer_account", "buyer_customer_id", "amazon_order_no"]


# ── 建连接 ────────────────────────────────────────────────────────────────────


def test_create_connection_merges_admission_and_stores_writeback_types(conn, workspace, fake):
    ws = workspace["id"]
    out = create_demo_connection(conn, ws, fake, workspace["user_id"])
    assert out["writeback_state"] == "unverified" and out["writeback_enabled"] is True
    assert out["writeback_verified_at"] is None and out["enabled"] is True and out["poll_minutes"] == 10
    assert out["admission"]["purchaser_values"] == [DEMO_PURCHASER]
    assert out["admission"]["approved_values"] == ["通过"]                # 缺省值合并进来
    assert out["admission"]["skip_purchase_status_values"] == ["已拍单", "拿回"]
    assert out["result_table_id"] == DEMO_TABLE
    row = fc.get_row(conn, ws, out["id"])
    assert row["writeback_field_types"]["税前总额USD"] == 2 and row["writeback_field_types"]["采购状态"] == 3
    assert [c["id"] for c in fc.list_connections(conn, ws)] == [out["id"]]
    ev = conn.execute("SELECT kind FROM procure.events WHERE entity = 'connection' AND entity_id = %s",
                      (out["id"],)).fetchall()
    assert [e["kind"] for e in ev] == ["connection.created"]


def test_create_connection_validation(conn, workspace, fake):
    ws = workspace["id"]
    with pytest.raises(DomainError) as e:
        fc.create(conn, ws, demo_connection_data(fake, field_map={"asin": "ASIN"}), None, client=fake)
    assert "平台订单号" in e.value.message
    with pytest.raises(DomainError) as e:
        fc.create(conn, ws, demo_connection_data(fake, field_map={"upstream_order_no": "不存在的列DEMO"}), None,
                  client=fake)
    assert "不存在的列DEMO" in e.value.message
    with pytest.raises(DomainError) as e:
        fc.create(conn, ws, demo_connection_data(fake, writeback_map={"total": "总成本USD"}), None, client=fake)
    assert "公式" in e.value.message and e.value.code == "VALIDATION"
    with pytest.raises(DomainError) as e:
        fc.create(conn, ws, demo_connection_data(fake, admission={"approved": ["通过"]}), None, client=fake)
    assert "未知" in e.value.message
    first = create_demo_connection(conn, ws, fake)
    with pytest.raises(DomainError) as e:
        create_demo_connection(conn, ws, fake)
    assert e.value.code == "CONFLICT" and e.value.detail["connection_id"] == first["id"]


def test_create_with_separate_result_table_requires_key_column(conn, workspace, fake):
    ws = workspace["id"]
    fake.add_table(DEMO_APP, DEMO_RESULT_TABLE, "结果DEMO", [("说明", 1), ("AMZ订单号", 1)])
    with pytest.raises(DomainError) as e:
        create_demo_connection(conn, ws, fake, result_table_id=DEMO_RESULT_TABLE,
                               writeback_map={"amazon_order_no": "AMZ订单号"})
    assert "平台订单号" in e.value.message
    fake.add_table(DEMO_APP, DEMO_RESULT_TABLE, "结果DEMO", [("平台订单号", 1), ("AMZ订单号", 1)])
    out = create_demo_connection(conn, ws, fake, result_table_id=DEMO_RESULT_TABLE,
                                 writeback_map={"amazon_order_no": "AMZ订单号"})
    assert out["result_table_id"] == DEMO_RESULT_TABLE and out["writeback_map"] == {"amazon_order_no": "AMZ订单号"}


# ── 样本预览 ──────────────────────────────────────────────────────────────────


def _seed_preview_records(fake):
    add = lambda rid, no, **kw: fake.add_record(DEMO_APP, DEMO_TABLE, rid, order_fields(no, **kw))
    add("recDEMO0001", "PO-DEMO-P01")                                          # 通过、本采购方 → new
    add("recDEMO0002", "PO-DEMO-P02", 采购方="外采购DEMO")                       # 别的采购方 → skipped
    add("recDEMO0003", "PO-DEMO-P03", 人工审核=None)                             # 未审 → skipped
    add("recDEMO0004", "PO-DEMO-P04", 人工审核="拒绝")                           # 拒绝 → skipped
    add("recDEMO0005", "PO-DEMO-P05", 平台订单号=None)                           # 缺键 → invalid
    add("recDEMO0006", "PO-DEMO-P06", AMZ订单号=text("111-0000000-0000006"), 采购状态="已拍单")  # 历史 → skipped
    add("recDEMO0007", "PO-DEMO-P01")                                          # 同键重复 → invalid
    add("recDEMO0008", "PO-DEMO-P08", ASIN=None)                               # 缺 ASIN → new（资料待补齐）


def test_preview_is_read_only_and_explains_each_record(conn, workspace, fake):
    ws = workspace["id"]
    _seed_preview_records(fake)
    data = demo_connection_data(fake)
    out = fc.preview(conn, ws, app_token=DEMO_APP, table_id=DEMO_TABLE, field_map=data["field_map"],
                     admission=data["admission"], limit=50)
    st = out["stats"]
    assert st["read"] == 8 and st["new"] == 2 and st["skipped"] == 4 and st["invalid"] == 2
    assert st["needs_info"] == 1
    by_id = {r["record_id"]: r for r in out["records"]}
    assert by_id["recDEMO0001"]["verdict"] == "new" and by_id["recDEMO0001"]["record"]["asin"] == "B0DEMO0001"
    assert "外采购DEMO" in by_id["recDEMO0002"]["reason"]
    assert "尚未人审" in by_id["recDEMO0003"]["reason"]
    assert "拒绝" in by_id["recDEMO0004"]["reason"]
    assert by_id["recDEMO0005"]["reason"] == "缺少平台订单号"
    assert "历史采购" in by_id["recDEMO0006"]["reason"]
    assert "重复" in by_id["recDEMO0007"]["reason"]
    assert any("资料待补齐" in i for i in by_id["recDEMO0008"]["issues"])
    # 只读：不建任务、不写读取记录、不写飞书
    assert f.count(conn, "purchase_orders") == 0 and f.count(conn, "feishu_sync_runs") == 0
    assert fake.updates == []
    # 开启导入历史采购后，带 AMZ 订单号的行是 historical
    out2 = fc.preview(conn, ws, app_token=DEMO_APP, table_id=DEMO_TABLE, field_map=data["field_map"],
                      admission={**data["admission"], "import_historical": True}, limit=50)
    assert {r["record_id"]: r["verdict"] for r in out2["records"]}["recDEMO0006"] == "historical"
    # limit 只读这么多条
    assert len(fc.preview(conn, ws, app_token=DEMO_APP, table_id=DEMO_TABLE, field_map=data["field_map"],
                          limit=3)["records"]) == 3


def test_preview_uses_existing_connection_to_recognise_known_records(conn, workspace, fake):
    ws = workspace["id"]
    fake.add_record(DEMO_APP, DEMO_TABLE, "recDEMO0001", order_fields("PO-DEMO-P11"))
    c = create_demo_connection(conn, ws, fake)
    feishu_intake.run_connection(conn, ws, c["id"], trigger="manual", client=fake)
    out = fc.preview(conn, ws, app_token=DEMO_APP, table_id=DEMO_TABLE, field_map=c["field_map"],
                     admission=c["admission"])
    assert [r["verdict"] for r in out["records"]] == ["unchanged"]
    fake.edit_record(DEMO_APP, DEMO_TABLE, "recDEMO0001", 采购单价=26.0)
    out = fc.preview(conn, ws, app_token=DEMO_APP, table_id=DEMO_TABLE, field_map=c["field_map"],
                     admission=c["admission"])
    assert [r["verdict"] for r in out["records"]] == ["update"]
    assert f.count(conn, "purchase_orders", "unit_price = 25.00") == 1      # 预览没改库


# ── 修改与停用 ────────────────────────────────────────────────────────────────


def test_update_validates_and_resets_verification_on_writeback_map_change(conn, workspace, fake):
    ws = workspace["id"]
    c = create_demo_connection(conn, ws, fake)
    conn.execute("UPDATE procure.feishu_connections SET writeback_verified_at = %s WHERE id = %s", (NOW, c["id"]))
    out = fc.update(conn, ws, c["id"], {"name": "DEMO 新名称", "poll_minutes": 30}, workspace["user_id"],
                    client=fake)
    assert out["name"] == "DEMO 新名称" and out["poll_minutes"] == 30 and out["writeback_state"] == "active"
    out = fc.update(conn, ws, c["id"], {"writeback_map": {"amazon_order_no": "AMZ订单号"}}, None, client=fake)
    assert out["writeback_map"] == {"amazon_order_no": "AMZ订单号"} and out["writeback_state"] == "unverified"
    with pytest.raises(DomainError):
        fc.update(conn, ws, c["id"], {"source_table_id": "tblDEMOOTHER9"}, None, client=fake)
    with pytest.raises(DomainError):
        fc.update(conn, ws, c["id"], {"writeback_map": {"total": "总成本USD"}}, None, client=fake)
    with pytest.raises(DomainError):
        fc.update(conn, ws, c["id"], {"poll_minutes": 0}, None, client=fake)
    out = fc.update(conn, ws, c["id"], {"admission": {"import_historical": True}}, None, client=fake)
    assert out["admission"]["import_historical"] is True and out["admission"]["purchaser_values"] == []


def test_disable_stops_schedule(conn, workspace, fake):
    ws = workspace["id"]
    c = create_demo_connection(conn, ws, fake)
    assert (ws, c["id"]) in feishu_intake.due_connections(conn)
    out = fc.disable(conn, ws, c["id"], workspace["user_id"])
    assert out["enabled"] is False and out["writeback_state"] == "disabled"
    assert feishu_intake.due_connections(conn) == []
    with pytest.raises(DomainError):
        feishu_intake.run_connection(conn, ws, c["id"], trigger="manual", client=fake)
    assert fc.update(conn, ws, c["id"], {"enabled": True}, None)["enabled"] is True


# ── 写回验收 ──────────────────────────────────────────────────────────────────


def test_verify_writeback_success_restores_test_record(conn, workspace, fake):
    ws = workspace["id"]
    original = order_fields("PO-DEMO-TEST", 买家号=text("旧买家DEMO"), 税前总额USD=12.5)
    fake.add_record(DEMO_APP, DEMO_TABLE, "recDEMOTEST1", original)
    c = create_demo_connection(conn, ws, fake)
    before = fake.record(DEMO_APP, DEMO_TABLE, "recDEMOTEST1")
    out = fc.verify_writeback(conn, ws, c["id"], "recDEMOTEST1", workspace["user_id"], client=fake)
    assert out["ok"] is True and out["mismatches"] == [] and out["restored"] is True and out["error"] is None
    written = out["written"]
    assert written["税前总额USD"] == 0.0 and written["采购状态"] == "已拍单" and written["付款卡号"] == "0000"
    assert written["AMZ订单号"].startswith("回填验收 2026-10-31 10:00")
    assert len(fake.updates) == 2                                        # 写测试值 + 恢复原值
    assert fake.record(DEMO_APP, DEMO_TABLE, "recDEMOTEST1") == before    # 原值恢复，原来为空的列仍为空
    assert out["connection"]["writeback_state"] == "active"
    assert out["connection"]["writeback_test_record"] == "recDEMOTEST1"
    assert out["connection"]["writeback_verified_at"] == NOW.isoformat()


def test_verify_writeback_mismatch_and_feishu_error(conn, workspace, fake):
    ws = workspace["id"]
    fake.add_record(DEMO_APP, DEMO_TABLE, "recDEMOTEST1", order_fields("PO-DEMO-TEST"))
    c = create_demo_connection(conn, ws, fake)
    conn.execute("UPDATE procure.feishu_connections SET writeback_verified_at = %s WHERE id = %s", (NOW, c["id"]))
    fake.ignore_writes_to = {"AMZ订单号"}           # 写入被接受但读回不一致
    out = fc.verify_writeback(conn, ws, c["id"], "recDEMOTEST1", None, client=fake)
    assert out["ok"] is False
    assert [m["column"] for m in out["mismatches"]] == ["AMZ订单号"]
    assert out["mismatches"][0]["result_field"] == "amazon_order_no" and out["mismatches"][0]["actual"] is None
    assert out["connection"]["writeback_state"] == "unverified"          # 原来的验收作废
    fake.ignore_writes_to = set()
    fake.fail_next("batch_update_records")
    out = fc.verify_writeback(conn, ws, c["id"], "recDEMOTEST1", None, client=fake)
    assert out["ok"] is False and "写回验收失败" in out["error"] and out["restored"] is False
    out = fc.verify_writeback(conn, ws, c["id"], "recDEMONOPE9", None, client=fake)
    assert out["ok"] is False and "记录不存在" in out["error"]
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM procure.events WHERE entity = 'connection' "
                                              "AND entity_id = %s ORDER BY id", (c["id"],)).fetchall()]
    assert kinds.count("connection.writeback_verify_failed") == 3


def test_verify_writeback_requires_map(conn, workspace, fake):
    ws = workspace["id"]
    c = create_demo_connection(conn, ws, fake, writeback_map={})
    assert c["writeback_state"] == "disabled" and c["writeback_enabled"] is False
    with pytest.raises(DomainError) as e:
        fc.verify_writeback(conn, ws, c["id"], "recDEMOTEST1", None, client=fake)
    assert "写回列" in e.value.message


# ── 读取记录与跨工作区隔离 ────────────────────────────────────────────────────────


def test_list_runs_after_intake(conn, workspace, fake):
    ws = workspace["id"]
    fake.add_record(DEMO_APP, DEMO_TABLE, "recDEMO0001", order_fields("PO-DEMO-R01"))
    c = create_demo_connection(conn, ws, fake)
    feishu_intake.run_connection(conn, ws, c["id"], trigger="manual", client=fake)
    runs = fc.list_runs(conn, ws, c["id"])
    assert len(runs) == 1 and runs[0]["ok"] is True and runs[0]["trigger"] == "manual"
    assert runs[0]["stats"]["new"] == 1
    got = fc.get(conn, ws, c["id"])
    assert got["last_stats"]["new"] == 1 and got["last_error"] is None and got["last_sync_ok_at"] == NOW.isoformat()


def test_connections_are_invisible_across_workspaces(conn, workspace, fake):
    ws_a = workspace["id"]
    ws_b = f.make_workspace(conn)["id"]
    c = create_demo_connection(conn, ws_a, fake)
    assert fc.list_connections(conn, ws_b) == []
    for call in (
        lambda: fc.get(conn, ws_b, c["id"]),
        lambda: fc.update(conn, ws_b, c["id"], {"name": "DEMO 越权"}, None, client=fake),
        lambda: fc.disable(conn, ws_b, c["id"], None),
        lambda: fc.verify_writeback(conn, ws_b, c["id"], "recDEMO0001", None, client=fake),
        lambda: fc.list_runs(conn, ws_b, c["id"]),
        lambda: feishu_intake.run_connection(conn, ws_b, c["id"], trigger="manual", client=fake),
    ):
        with pytest.raises(DomainError) as e:
            call()
        assert e.value.code == "NOT_FOUND" and e.value.http_status == 404
    # 乙工作区可以独立连接同一张表（各自的字段对应与结果互不影响）
    other = create_demo_connection(conn, ws_b, fake, name="DEMO 乙")
    assert other["id"] != c["id"] and fc.get(conn, ws_a, c["id"])["name"] == "DEMO 订单中心"
