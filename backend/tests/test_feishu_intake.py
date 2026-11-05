"""飞书读取：准入（只为本采购方、人审通过的行建任务）、更新、取消、历史单号、重复读取、跨来源冲突、
读飞书失败的记录、到期判断、后台任务。全部数据虚构。"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from api.feishu import FeishuError
from registry import db
from services import allocation, clock, feishu_intake, imports
from services import feishu_connections as fc
from services.errors import DomainError
from tests import factories as f
from tests.fake_feishu import (
    DEMO_APP,
    DEMO_TABLE,
    FakeFeishuClient,
    create_demo_connection,
    ms,
    order_fields,
    text,
)
from workflows import feishu_intake as intake_workflow

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


def _add(fake, rid, no, **kw):
    fake.add_record(DEMO_APP, DEMO_TABLE, rid, order_fields(no, **kw))


def _order(conn, ws, no):
    return conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = %s",
                        (ws, no)).fetchone()


def _run(conn, ws, cid, fake, trigger="manual"):
    return feishu_intake.run_connection(conn, ws, cid, trigger=trigger, client=fake)


# ── 准入 ─────────────────────────────────────────────────────────────────────


def test_intake_creates_tasks_only_for_approved_rows_of_our_purchaser(conn, workspace, fake):
    ws = workspace["id"]
    acc = f.make_account(conn, ws, name="环境-DEMO-A", min_amount=10, max_amount=100)
    _add(fake, "recDEMO0001", "PO-DEMO-A01")
    _add(fake, "recDEMO0002", "PO-DEMO-A02", 采购方="外采购DEMO")
    _add(fake, "recDEMO0003", "PO-DEMO-A03", 人工审核=None)
    _add(fake, "recDEMO0004", "PO-DEMO-A04", 人工审核="钓鱼")
    _add(fake, "recDEMO0005", "PO-DEMO-A05", 采购状态="已拍单")              # 已拍单但没有 AMZ 订单号
    _add(fake, "recDEMO0006", "PO-DEMO-A06", 平台订单号=None)
    c = create_demo_connection(conn, ws, fake)
    stats = _run(conn, ws, c["id"], fake)
    assert {k: stats[k] for k in feishu_intake.STAT_KEYS} == {
        "read": 6, "new": 1, "update": 0, "unchanged": 0, "conflict": 0, "historical": 0, "skipped": 4, "invalid": 1}
    reasons = {p["record_id"]: p["reason"] for p in stats["problems"]}
    assert "外采购DEMO" in reasons["recDEMO0002"] and "尚未人审" in reasons["recDEMO0003"]
    assert "钓鱼" in reasons["recDEMO0004"] and "已拍单" in reasons["recDEMO0005"]
    assert reasons["recDEMO0006"] == "缺少平台订单号"
    assert f.count(conn, "purchase_orders", "workspace_id = %s", (ws,)) == 1
    o = _order(conn, ws, "PO-DEMO-A01")
    # 读取后立即分配
    assert o["status"] == "assigned" and o["assigned_account_id"] == acc["id"]
    assert stats["allocation"]["trigger"] == "intake" and stats["allocation"]["stats"]["assigned"] == 1
    assert o["primary_source_kind"] == "feishu" and o["review_status"] == "approved"
    src = conn.execute("SELECT * FROM procure.order_sources WHERE purchase_order_id = %s", (o["id"],)).fetchone()
    assert src["is_writeback_target"] and src["feishu_record_id"] == "recDEMO0001"
    assert src["snapshot"]["平台订单号"] == "PO-DEMO-A01" and src["snapshot"]["采购单价"] == 25.0
    assert "备注" not in src["snapshot"]                  # 只读已对应的列
    assert fake.calls_of("search_records")[0]["field_names"] == fc.read_fields_for(
        fake, DEMO_APP, DEMO_TABLE, c["field_map"])[1]
    run = conn.execute("SELECT * FROM procure.feishu_sync_runs WHERE id = %s", (stats["run_id"],)).fetchone()
    assert run["ok"] is True and run["trigger"] == "manual" and run["stats"]["new"] == 1
    assert len(run["stats"]["problems"]) == 5


def test_intake_twice_is_unchanged_without_duplicates(conn, workspace, fake, monkeypatch):
    ws = workspace["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-B01")
    _add(fake, "recDEMO0002", "PO-DEMO-B02", ASIN=None)            # 缺 ASIN → 资料待补齐
    c = create_demo_connection(conn, ws, fake)
    first = _run(conn, ws, c["id"], fake)
    calls = []
    real_ingest = feishu_intake.orders.ingest
    monkeypatch.setattr(feishu_intake.orders, "ingest", lambda *a, **k: calls.append(a) or real_ingest(*a, **k))
    clock.freeze(NOW + timedelta(minutes=10))
    second = _run(conn, ws, c["id"], fake, trigger="schedule")
    assert calls == []                                                # 记录没变：不逐条 ingest、不锁任务行
    assert f.count(conn, "order_sources", "last_seen_at = %s", (NOW + timedelta(minutes=10),)) == 2
    assert first["new"] == 2 and second["unchanged"] == 2 and second["new"] == 0
    assert second["allocation"] is None                               # 没有变化就不重新分配
    assert f.count(conn, "purchase_orders") == 2 and f.count(conn, "order_sources") == 2
    assert _order(conn, ws, "PO-DEMO-B02")["status"] == "needs_info"
    assert f.count(conn, "feishu_sync_runs", "ok") == 2


def test_duplicate_keys_in_one_read_create_one_task(conn, workspace, fake):
    ws = workspace["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-C01")
    _add(fake, "recDEMO0002", "PO-DEMO-C01", 采购单价=30.0)
    c = create_demo_connection(conn, ws, fake)
    stats = _run(conn, ws, c["id"], fake)
    assert stats["new"] == 1 and stats["invalid"] == 1
    assert "recDEMO0001" in stats["problems"][0]["reason"]
    # 已关联的那条记录优先：即使它排在后面
    fake.apps[DEMO_APP][DEMO_TABLE]["records"] = dict(reversed(list(fake.apps[DEMO_APP][DEMO_TABLE]["records"].items())))
    again = _run(conn, ws, c["id"], fake)
    assert again["unchanged"] == 1 and again["invalid"] == 1
    assert "recDEMO0001" in again["problems"][0]["reason"]
    assert f.count(conn, "purchase_orders") == 1


# ── 更新、取消、人审撤回 ───────────────────────────────────────────────────────


def test_intake_updates_existing_task_on_change(conn, workspace, fake):
    ws = workspace["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-D01")
    c = create_demo_connection(conn, ws, fake)
    _run(conn, ws, c["id"], fake)
    fake.edit_record(DEMO_APP, DEMO_TABLE, "recDEMO0001", 限价=45.5, 收货人城市=text("New Demo City"))
    stats = _run(conn, ws, c["id"], fake)
    assert stats["update"] == 1 and stats["allocation"] is not None
    o = _order(conn, ws, "PO-DEMO-D01")
    assert str(o["price_cap"]) == "45.50" and o["city"] == "New Demo City" and o["status"] == "queued"
    ev = conn.execute("SELECT kind FROM procure.events WHERE order_id = %s AND kind = 'order.source_updated'",
                      (o["id"],)).fetchall()
    assert len(ev) == 1


def test_known_task_still_receives_review_revocation(conn, workspace, fake):
    """准入只挡「新建」：已有任务的人审被撤回，下一次读取把它送回待确认。"""
    ws = workspace["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-E01")
    c = create_demo_connection(conn, ws, fake)
    _run(conn, ws, c["id"], fake)
    fake.edit_record(DEMO_APP, DEMO_TABLE, "recDEMO0001", 人工审核="拒绝", 采购方="外采购DEMO")
    stats = _run(conn, ws, c["id"], fake)
    # 人审撤回照常应用；采购方移走另外转「待核实」（admission 冲突），二者都传达到已有任务，不算 skipped
    assert stats["conflict"] == 1 and stats["skipped"] == 0
    o = _order(conn, ws, "PO-DEMO-E01")
    assert o["status"] == "pending_review" and o["review_status"] == "rejected"
    assert o["conflict"]["kind"] == "admission" and o["assigned_account_id"] is None


def test_cancel_value_cancels_pre_execution_task(conn, workspace, fake):
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-A")
    _add(fake, "recDEMO0001", "PO-DEMO-F01")
    _add(fake, "recDEMO0002", "PO-DEMO-F02", 售后状态="已取消")           # 新单就已取消 → 不建
    c = create_demo_connection(conn, ws, fake, admission={"purchaser_values": ["自采购DEMO"],
                                                          "cancel_after_sale_values": ["已取消"]})
    first = _run(conn, ws, c["id"], fake)
    assert first["new"] == 1 and first["skipped"] == 1
    assert _order(conn, ws, "PO-DEMO-F01")["status"] == "assigned"
    fake.edit_record(DEMO_APP, DEMO_TABLE, "recDEMO0001", 售后状态="已取消")
    stats = _run(conn, ws, c["id"], fake)
    assert stats["update"] == 1
    o = _order(conn, ws, "PO-DEMO-F01")
    assert o["status"] == "cancelled" and o["upstream_cancelled"] and o["assigned_account_id"] is None
    assert "已取消" in o["status_reason"]


# ── 历史采购 ──────────────────────────────────────────────────────────────────


def test_historical_rows_only_with_import_historical_and_never_repurchased(conn, workspace, fake):
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-A")
    _add(fake, "recDEMO0001", "PO-DEMO-G01", AMZ订单号=text("111-0000000-0000001"), 采购状态="已拍单",
         买家号=text("环境-DEMO-A"))
    c = create_demo_connection(conn, ws, fake)
    off = _run(conn, ws, c["id"], fake)
    assert off["skipped"] == 1 and off["historical"] == 0 and f.count(conn, "purchase_orders") == 0

    fc.update(conn, ws, c["id"], {"admission": {"purchaser_values": ["自采购DEMO"], "import_historical": True}},
              None, client=fake)
    on = _run(conn, ws, c["id"], fake)
    assert on["historical"] == 1
    o = _order(conn, ws, "PO-DEMO-G01")
    assert o["status"] == "purchased" and o["external_amz_order_no"] == "111-0000000-0000001"
    atts = conn.execute("SELECT * FROM procure.purchase_attempts WHERE purchase_order_id = %s", (o["id"],)).fetchall()
    assert [(a["kind"], a["status"], a["simulated"]) for a in atts] == [("external", "succeeded", False)]
    allocation.allocate(conn, ws, trigger="manual")
    again = _run(conn, ws, c["id"], fake)
    assert again["unchanged"] == 1
    o = _order(conn, ws, "PO-DEMO-G01")
    assert o["status"] == "purchased" and o["assigned_account_id"] is None
    assert f.count(conn, "purchase_attempts", "purchase_order_id = %s", (o["id"],)) == 1
    assert f.count(conn, "writebacks") == 0                  # 历史单号不写回


# ── 跨来源 ────────────────────────────────────────────────────────────────────


def _csv_bytes(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    headers = ["平台订单号", "ASIN", "数量", "采购单价", "限价", "建议采购日期", "人工审核", "收货人姓名", "收货人地址",
               "收货人城市", "收货人州/省", "邮编", "收货人电话"]
    w.writerow(headers)
    for r in rows:
        w.writerow([r.get(h, "") for h in headers])
    return buf.getvalue().encode("utf-8")


def _file_row(no, **kw):
    row = {"平台订单号": no, "ASIN": "B0DEMO0001", "数量": "1", "采购单价": "25.00", "限价": "40.00",
           "建议采购日期": "2026-10-31", "人工审核": "通过", "收货人姓名": "Demo Buyer", "收货人地址": "100 Demo Street",
           "收货人城市": "Demo City", "收货人州/省": "FL", "邮编": "32960", "收货人电话": "555-0100"}
    row.update(kw)
    return row


def test_feishu_then_file_with_same_key_gives_one_task_and_conflict_on_difference(conn, workspace, fake):
    ws = workspace["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-H01")
    _add(fake, "recDEMO0002", "PO-DEMO-H02")
    c = create_demo_connection(conn, ws, fake)
    _run(conn, ws, c["id"], fake)
    data = _csv_bytes([_file_row("PO-DEMO-H01"), _file_row("PO-DEMO-H02", ASIN="B0DEMO0999")])
    up = imports.upload(conn, ws, filename="demo-same-key.csv", data=data, actor_user_id=workspace["user_id"])
    pv = imports.preview(conn, ws, up["batch_id"])
    verdicts = {r["upstream_order_no"]: r["verdict"] for r in pv["rows"]["rows"]}
    assert verdicts == {"PO-DEMO-H01": "unchanged", "PO-DEMO-H02": "conflict"}
    imports.commit(conn, ws, up["batch_id"], workspace["user_id"])
    assert f.count(conn, "purchase_orders", "workspace_id = %s", (ws,)) == 2
    h1, h2 = _order(conn, ws, "PO-DEMO-H01"), _order(conn, ws, "PO-DEMO-H02")
    assert f.count(conn, "order_sources", "purchase_order_id = %s", (h1["id"],)) == 2
    assert h2["status"] == "conflict" and h2["asin"] == "B0DEMO0001"         # 不覆盖执行商品
    assert h2["conflict"]["fields"]["asin"] == ["B0DEMO0001", "B0DEMO0999"]
    # 再读飞书：同一记录没变，冲突保持（等人工处理）
    again = _run(conn, ws, c["id"], fake)
    assert again["unchanged"] == 2 and _order(conn, ws, "PO-DEMO-H02")["status"] == "conflict"


# ── 失败与调度 ────────────────────────────────────────────────────────────────


def test_feishu_error_is_recorded_and_raised(conn, workspace, fake):
    ws = workspace["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-I01")
    c = create_demo_connection(conn, ws, fake)
    fake.fail_next("search_records", FeishuError(1254302, "Permission denied (DEMO)"))
    with pytest.raises(DomainError) as e:
        _run(conn, ws, c["id"], fake)
    assert e.value.code == "FEISHU_ERROR" and e.value.http_status == 502
    assert "读取飞书记录失败" in e.value.message and e.value.detail["run_id"]
    run = conn.execute("SELECT * FROM procure.feishu_sync_runs WHERE id = %s", (e.value.detail["run_id"],)).fetchone()
    assert run["ok"] is False and "添加文档应用" in run["error"]
    got = fc.get(conn, ws, c["id"])
    assert got["last_error"] == run["error"] and got["last_sync_ok_at"] is None and got["last_sync_at"] is not None
    assert f.count(conn, "purchase_orders") == 0
    # 平台订单号列被删掉：同样记为失败
    fake.apps[DEMO_APP][DEMO_TABLE]["fields"] = [x for x in fake.apps[DEMO_APP][DEMO_TABLE]["fields"]
                                                 if x["field_name"] != "平台订单号"]
    with pytest.raises(DomainError) as e2:
        _run(conn, ws, c["id"], fake)
    assert e2.value.code == "VALIDATION" and f.count(conn, "feishu_sync_runs", "NOT ok") == 2


def test_due_connections_follow_poll_minutes(conn, workspace, fake):
    ws = workspace["id"]
    c = create_demo_connection(conn, ws, fake, poll_minutes=15)
    assert feishu_intake.due_connections(conn) == [(ws, c["id"])]
    _run(conn, ws, c["id"], fake, trigger="schedule")
    assert feishu_intake.due_connections(conn) == []
    assert feishu_intake.due_connections(conn, NOW + timedelta(minutes=14)) == []
    assert feishu_intake.due_connections(conn, NOW + timedelta(minutes=15)) == [(ws, c["id"])]


def test_workflow_reads_due_connections_in_own_transactions(conn, workspace, fake):
    ws = workspace["id"]
    ws_b = f.make_workspace(conn)["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-J01")
    ok = create_demo_connection(conn, ws, fake)
    other_app = "bascnDEMOOTHERAPP1"
    fake2 = FakeFeishuClient.with_order_table(app_token=other_app, table_id="tblDEMOOTHER9")
    fc.grant_base(conn, ws_b, other_app)            # 乙的表格由部署所有者授权给乙
    bad = fc.create(conn, ws_b, _demo_data(fake2, "tblDEMOOTHER9", other_app), None, client=fake2)
    conn.commit()
    # 后台任务用同一个客户端：乙的表在这个客户端里不存在 → 乙失败、甲成功
    out = intake_workflow.run()
    assert out["connections"][str(ok["id"])]["new"] == 1
    assert out["connections"][str(bad["id"])]["code"] == "FEISHU_ERROR"
    with db.pg_conn() as c2:
        assert c2.execute("SELECT count(*) AS n FROM procure.purchase_orders WHERE workspace_id = %s",
                          (ws,)).fetchone()["n"] == 1
        runs = c2.execute("SELECT connection_id, ok FROM procure.feishu_sync_runs ORDER BY id").fetchall()
        assert {(r["connection_id"], r["ok"]) for r in runs} == {(ok["id"], True), (bad["id"], False)}
    # 指定连接 → manual
    out = intake_workflow.run(connection_id=str(ok["id"]))
    assert out["connections"][str(ok["id"])]["unchanged"] == 1


def _demo_data(client, table_id, app_token=DEMO_APP):
    from services import feishu_fields

    fields = client.list_fields(app_token, table_id)
    return {"name": "DEMO 乙订单表", "link": f"https://example-demo.feishu.cn/base/{app_token}?table={table_id}",
            "app_token": app_token, "source_table_id": table_id, "source_view_id": None,
            "field_map": feishu_fields.suggest_field_map([x["field_name"] for x in fields]),
            "writeback_map": feishu_fields.suggest_writeback_map(fields)}


def test_workflow_skips_when_feishu_not_configured(monkeypatch):
    def boom():
        raise DomainError("FEISHU_NOT_CONFIGURED", fc.NOT_CONFIGURED_MESSAGE, 409)

    monkeypatch.setattr(fc, "make_client", boom)
    out = intake_workflow.run()
    assert out == {"due": 0, "connections": {}}                  # 没有到期连接：根本不建客户端
    with db.pg_conn() as conn:
        create_demo_connection(conn, f.make_workspace(conn)["id"], FakeFeishuClient.with_order_table())
    out = intake_workflow.run()
    assert out == {"due": 1, "connections": {}, "skipped": fc.NOT_CONFIGURED_MESSAGE}


# ── 坏数据与意外异常不能拖垮整次读取（D4-02）────────────────────────────────────


def test_one_absurd_record_does_not_block_the_whole_read(conn, workspace, fake):
    ws = workspace["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-Z01")
    _add(fake, "recDEMO0002", "PO-DEMO-Z02", 建议采购日期=None, 下单时间=ms(datetime(9999, 12, 31, tzinfo=timezone.utc)))
    _add(fake, "recDEMO0003", "PO-DEMO-Z03", 限价=1e25)
    c = create_demo_connection(conn, ws, fake)
    stats = _run(conn, ws, c["id"], fake)
    assert stats["read"] == 3 and stats["new"] == 3
    assert _order(conn, ws, "PO-DEMO-Z01")["status"] in ("queued", "assigned")
    assert _order(conn, ws, "PO-DEMO-Z02")["status"] == "needs_info"
    assert _order(conn, ws, "PO-DEMO-Z03")["status"] == "needs_info"


def test_unexpected_error_for_one_record_only_invalidates_it(conn, workspace, fake, monkeypatch):
    from services import orders

    ws = workspace["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-Z11")
    _add(fake, "recDEMO0002", "PO-DEMO-Z12")
    c = create_demo_connection(conn, ws, fake)
    real = orders.ingest

    def flaky(conn_, ws_, rec, src, **kw):
        if rec.upstream_order_no == "PO-DEMO-Z12":
            raise OverflowError("date value out of range")
        return real(conn_, ws_, rec, src, **kw)

    monkeypatch.setattr(orders, "ingest", flaky)
    stats = _run(conn, ws, c["id"], fake)
    assert stats["new"] == 1 and stats["invalid"] == 1
    assert "OverflowError" in next(p["reason"] for p in stats["problems"] if p["upstream_order_no"] == "PO-DEMO-Z12")


def test_unexpected_error_in_intake_is_recorded_as_failed_run(conn, workspace, fake, monkeypatch):
    """接收阶段意外出错：回滚本次接收、记失败的读取记录与 last_error、last_sync_at 前进（不每 30 秒重读）。"""
    ws = workspace["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-Z21")
    c = create_demo_connection(conn, ws, fake)
    conn.commit()

    def boom(*a, **kw):
        raise RuntimeError("DEMO 意外")

    monkeypatch.setattr(feishu_intake, "evaluate_records", boom)
    with pytest.raises(DomainError) as e:
        _run(conn, ws, c["id"], fake)
    assert e.value.code == "INTERNAL" and e.value.detail["run_id"]
    conn.commit()
    run = conn.execute("SELECT * FROM procure.feishu_sync_runs WHERE id = %s", (e.value.detail["run_id"],)).fetchone()
    got = fc.get_row(conn, ws, c["id"])
    assert run["ok"] is False and "RuntimeError" in run["error"] and got["last_error"] == run["error"]
    assert got["last_sync_at"] is not None and _order(conn, ws, "PO-DEMO-Z21") is None
    assert (ws, c["id"]) not in feishu_intake.due_connections(conn, clock.now())


def test_writeback_test_record_is_never_ingested(conn, workspace, fake):
    ws = workspace["id"]
    _add(fake, "recDEMO0001", "PO-DEMO-Z31")
    _add(fake, "recDEMOTEST", "PO-DEMO-TEST", AMZ订单号=text("回填验收 2026-10-31 10:00:00"), 采购状态="已拍单")
    c = create_demo_connection(conn, ws, fake)
    conn.execute("UPDATE procure.feishu_connections SET writeback_test_record = 'recDEMOTEST' WHERE id = %s",
                 (c["id"],))
    stats = _run(conn, ws, c["id"], fake)
    assert stats["new"] == 1 and stats["skipped"] == 1 and _order(conn, ws, "PO-DEMO-TEST") is None
    prev = fc.preview(conn, ws, app_token=DEMO_APP, table_id=DEMO_TABLE, field_map=c["field_map"],
                      admission=c["admission"], client=fake)
    assert next(r for r in prev["records"] if r["record_id"] == "recDEMOTEST")["verdict"] == "skipped"


# ── 已有任务的采购方 / 采购状态变化（D4-01 / D1-03）──────────────────────────────


def test_purchaser_change_or_bought_elsewhere_pauses_assigned_task(conn, workspace, fake):
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-A")
    _add(fake, "recDEMO0001", "PO-DEMO-Q01")
    _add(fake, "recDEMO0002", "PO-DEMO-Q02")
    c = create_demo_connection(conn, ws, fake)
    _run(conn, ws, c["id"], fake)
    assert _order(conn, ws, "PO-DEMO-Q01")["status"] == "assigned"
    fake.edit_record(DEMO_APP, DEMO_TABLE, "recDEMO0001", 采购方="外采购DEMO")
    fake.edit_record(DEMO_APP, DEMO_TABLE, "recDEMO0002", 采购状态="已拍单")
    stats = _run(conn, ws, c["id"], fake)
    assert stats["conflict"] == 2 and stats["unchanged"] == 0
    for no in ("PO-DEMO-Q01", "PO-DEMO-Q02"):
        o = _order(conn, ws, no)
        assert o["status"] == "conflict" and o["assigned_account_id"] is None and o["conflict"]["kind"] == "admission"
    again = _run(conn, ws, c["id"], fake)
    assert again["unchanged"] == 2 and again["conflict"] == 0
