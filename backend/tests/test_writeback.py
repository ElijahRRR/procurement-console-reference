"""飞书写回队列：只为真实采购成功且有飞书写回目标的任务入队；未验收只积压；验收后按列类型批量发送；
失败退避与 failed；新版本取代旧版本（旧版本绝不覆盖新版本）；改绑结果表；改写回列；重试；跨工作区隔离。全部数据虚构。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from api.feishu import FeishuError
from registry import db
from services import clock, feishu_intake, orders, writeback
from services import feishu_connections as fc
from services.errors import DomainError
from tests import factories as f
from tests.fake_feishu import (
    DEMO_APP,
    DEMO_RESULT_TABLE,
    DEMO_TABLE,
    FakeFeishuClient,
    create_demo_connection,
    order_fields,
    text,
)
from workflows import feishu_writeback as writeback_workflow

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
_seq = iter(range(1, 10_000))


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


def _setup(conn, ws, fake, nos=("PO-DEMO-W01",), *, verified=False, **conn_kw):
    acc = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id="A1DEMO00001")
    for i, no in enumerate(nos, start=1):
        fake.add_record(DEMO_APP, DEMO_TABLE, f"recDEMOW{i:03d}", order_fields(no))
    c = create_demo_connection(conn, ws, fake, **conn_kw)
    feishu_intake.run_connection(conn, ws, c["id"], trigger="manual", client=fake)
    if verified:
        _verify(conn, c["id"])
    return acc, c


def _verify(conn, cid):
    conn.execute("UPDATE procure.feishu_connections SET writeback_verified_at = %s WHERE id = %s", (NOW, cid))


def _order(conn, ws, no):
    return conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = %s",
                        (ws, no)).fetchone()


def _purchase(conn, ws, no, acc, *, simulated=False, amazon_no=None):
    """把任务登记为采购成功（新尝试 + 结果版本 +1），返回任务 id。"""
    o = _order(conn, ws, no)
    amazon_no = amazon_no or (f"SIM-20261031-{next(_seq)}" if simulated else f"111-0000000-{next(_seq):07d}")
    att = f.make_attempt(conn, ws, o["id"], acc["id"], status="succeeded", simulated=simulated,
                         amazon_order_no=amazon_no, submitted=True)
    conn.execute("UPDATE procure.purchase_attempts SET subtotal = 41.97, total_before_tax = 41.97, shipping = 0.00, "
                 "tax = 2.52, total = 44.49, card_last4 = '7860' WHERE id = %s", (att["id"],))
    f.set_status(conn, o["id"], "purchased", purchased_attempt_id=att["id"], result_version=o["result_version"] + 1)
    return o["id"]


def _wb(conn, wid):
    return conn.execute("SELECT * FROM procure.writebacks WHERE id = %s", (wid,)).fetchone()


# ── 入队 ─────────────────────────────────────────────────────────────────────


def test_enqueue_builds_typed_fields_for_real_purchase(conn, workspace, fake):
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake)
    oid = _purchase(conn, ws, "PO-DEMO-W01", acc, amazon_no="111-0000000-0000001")
    wid = writeback.enqueue_for_order(conn, ws, oid)
    row = _wb(conn, wid)
    assert row["status"] == "pending" and row["result_version"] == 1 and row["tries"] == 0
    assert row["table_id"] == DEMO_TABLE and row["record_id"] == "recDEMOW001" and row["connection_id"] == c["id"]
    assert row["fields"] == {"买家号": "环境-DEMO-A", "买家号id": "A1DEMO00001", "AMZ订单号": "111-0000000-0000001",
                             "采购状态": "已拍单", "税前总额USD": 41.97, "运费USD": 0.0, "税费USD": 2.52,
                             "付款卡号": "7860"}
    assert writeback.enqueue_for_order(conn, ws, oid) == wid          # 同一结果版本只入队一次
    assert f.count(conn, "writebacks") == 1
    ev = conn.execute("SELECT detail FROM procure.events WHERE order_id = %s AND kind = 'order.writeback_enqueued'",
                      (oid,)).fetchone()
    assert ev["detail"]["waiting_verification"] is True


def test_never_enqueue_simulated_external_or_file_only_results(conn, workspace, fake):
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake, ("PO-DEMO-S01", "PO-DEMO-S02"))
    # 模拟结果
    sim = _purchase(conn, ws, "PO-DEMO-S01", acc, simulated=True)
    assert writeback.enqueue_for_order(conn, ws, sim) is None
    # 尚未采购
    assert writeback.enqueue_for_order(conn, ws, _order(conn, ws, "PO-DEMO-S02")["id"]) is None
    # 来源带来的外部单号（历史采购）
    fc.update(conn, ws, c["id"], {"admission": {"purchaser_values": ["自采购DEMO"], "import_historical": True}}, None,
              client=fake)
    fake.add_record(DEMO_APP, DEMO_TABLE, "recDEMOX001",
                    order_fields("PO-DEMO-S03", AMZ订单号=text("111-0000000-0000303"), 采购状态="已拍单"))
    feishu_intake.run_connection(conn, ws, c["id"], trigger="manual", client=fake)
    ext = _order(conn, ws, "PO-DEMO-S03")
    assert ext["status"] == "purchased" and writeback.enqueue_for_order(conn, ws, ext["id"]) is None
    # 只有文件来源
    file_only = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-S04")
    fo = _purchase(conn, ws, "PO-DEMO-S04", acc)
    assert file_only["primary_source_kind"] == "file" and writeback.enqueue_for_order(conn, ws, fo) is None
    # 连接关闭写回
    fc.update(conn, ws, c["id"], {"writeback_enabled": False}, None)
    real = _purchase(conn, ws, "PO-DEMO-S02", acc)
    assert writeback.enqueue_for_order(conn, ws, real) is None
    assert f.count(conn, "writebacks") == 0


def test_enqueue_with_broken_column_config_records_failed_row(conn, workspace, fake):
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake)
    conn.execute("UPDATE procure.feishu_connections SET writeback_field_types = writeback_field_types || "
                 "'{\"税前总额USD\": 20}'::jsonb WHERE id = %s", (c["id"],))
    oid = _purchase(conn, ws, "PO-DEMO-W01", acc)
    wid = writeback.enqueue_for_order(conn, ws, oid)         # 不抛异常，不能挡住采购结果登记
    row = _wb(conn, wid)
    assert row["status"] == "failed" and "写回列配置有误" in row["last_error"] and row["fields"] == {}


def test_resolve_unknown_ordered_enqueues_writeback(conn, workspace, fake):
    """orders.resolve_unknown（人工核实已下单）调用 enqueue_for_order。"""
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake)
    o = _order(conn, ws, "PO-DEMO-W01")
    att = f.make_attempt(conn, ws, o["id"], acc["id"], status="unknown", simulated=False, submitted=True)
    f.set_status(conn, o["id"], "result_unknown", current_attempt_id=att["id"])
    orders.resolve_unknown(conn, ws, o["id"], "ordered", amazon_order_no="111-0000000-0000777",
                           amounts={"total": "44.49", "tax": "2.52"}, actor_user_id=workspace["user_id"])
    rows = writeback.list_writebacks(conn, ws)["rows"]
    assert len(rows) == 1 and rows[0]["fields"]["AMZ订单号"] == "111-0000000-0000777"
    assert rows[0]["fields"]["税费USD"] == 2.52 and rows[0]["result_version"] == 1


# ── 发送 ─────────────────────────────────────────────────────────────────────


def test_unverified_connection_keeps_writebacks_pending(conn, workspace, fake):
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake)
    wid = writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, "PO-DEMO-W01", acc))
    stats = writeback.flush(conn, client_factory=lambda: fake)
    assert stats["due"] == 0 and stats["sent"] == 0 and stats["waiting_verification"] == 1
    assert fake.updates == [] and _wb(conn, wid)["status"] == "pending"
    row = writeback.list_writebacks(conn, ws)["rows"][0]
    assert row["waiting_verification"] is True and row["connection_name"] == "DEMO 订单中心"
    assert fc.get(conn, ws, c["id"])["writeback_counts"]["pending"] == 1


def test_verified_flush_sends_one_batch_with_typed_values(conn, workspace, fake):
    ws = workspace["id"]
    nos = ("PO-DEMO-V01", "PO-DEMO-V02", "PO-DEMO-V03")
    acc, c = _setup(conn, ws, fake, nos, verified=True)
    wids = [writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, no, acc)) for no in nos]
    stats = writeback.flush(conn, client_factory=lambda: fake)
    assert stats["due"] == 3 and stats["sent"] == 3 and stats["done"] == 3 and stats["waiting_verification"] == 0
    assert len(fake.updates) == 1 and len(fake.updates[0]["records"]) == 3
    assert fake.updates[0]["table_id"] == DEMO_TABLE
    rec = fake.record(DEMO_APP, DEMO_TABLE, "recDEMOW002")
    assert rec["税前总额USD"] == 41.97 and isinstance(rec["税前总额USD"], float)
    assert rec["采购状态"] == "已拍单" and rec["付款卡号"] == text("7860") and rec["买家号"] == text("环境-DEMO-A")
    assert all(_wb(conn, w)["status"] == "done" and _wb(conn, w)["done_at"] == NOW for w in wids)
    assert f.count(conn, "events", "kind = 'order.writeback_done'") == 3
    assert writeback.flush(conn, client_factory=lambda: fake)["due"] == 0   # 不重复发送


def test_failure_backs_off_then_fails_after_ten_tries_and_retry_recovers(conn, workspace, fake):
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake, verified=True)
    wid = writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, "PO-DEMO-W01", acc))
    fake.fail_next("batch_update_records", FeishuError(1254061, "NumberFieldConvFail (DEMO)"), times=10)
    t = NOW
    expected = [1, 2, 5, 10, 30, 30, 30, 30, 30]
    for i, minutes in enumerate(expected, start=1):
        stats = writeback.flush(conn, now=t, client_factory=lambda: fake)
        row = _wb(conn, wid)
        assert stats["retrying"] == 1 and row["status"] == "pending" and row["tries"] == i
        assert row["next_try_at"] == t + timedelta(minutes=minutes) and "1254061" in row["last_error"]
        assert writeback.flush(conn, now=t + timedelta(minutes=minutes) - timedelta(seconds=1),
                               client_factory=lambda: fake)["due"] == 0          # 未到期不发
        t = row["next_try_at"]
    stats = writeback.flush(conn, now=t, client_factory=lambda: fake)
    row = _wb(conn, wid)
    assert stats["failed"] == 1 and row["status"] == "failed" and row["tries"] == 10
    assert "飞书写回失败" in row["last_error"]
    out = writeback.retry(conn, ws, wid, workspace["user_id"])
    assert out["status"] == "pending" and out["tries"] == 0 and out["last_error"] is None
    assert writeback.flush(conn, now=t, client_factory=lambda: fake)["done"] == 1
    with pytest.raises(DomainError) as e:
        writeback.retry(conn, ws, wid, None)
    assert "已成功" in e.value.message


@pytest.mark.parametrize("error", [FeishuError("NETWORK", "连接飞书失败：ConnectError"),
                                   FeishuError(1254302, "Permission denied (DEMO)")])
def test_table_level_error_backs_off_whole_batch_without_splitting(conn, workspace, fake, error):
    ws = workspace["id"]
    nos = ("PO-DEMO-T01", "PO-DEMO-T02")
    acc, c = _setup(conn, ws, fake, nos, verified=True)
    for no in nos:
        writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, no, acc))
    fake.fail_next("batch_update_records", error)
    stats = writeback.flush(conn, client_factory=lambda: fake)
    assert stats["retrying"] == 2 and len(fake.calls_of("batch_update_records")) == 1


def test_one_bad_record_does_not_block_the_batch(conn, workspace, fake):
    ws = workspace["id"]
    nos = ("PO-DEMO-U01", "PO-DEMO-U02", "PO-DEMO-U03")
    acc, c = _setup(conn, ws, fake, nos, verified=True)
    wids = [writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, no, acc)) for no in nos]
    del fake.apps[DEMO_APP][DEMO_TABLE]["records"]["recDEMOW002"]          # 有人在飞书里删了这条记录
    stats = writeback.flush(conn, client_factory=lambda: fake)
    assert stats["done"] == 2 and stats["retrying"] == 1
    assert [_wb(conn, w)["status"] for w in wids] == ["done", "pending", "done"]
    assert "RecordIdNotFound" in _wb(conn, wids[1])["last_error"]


def test_newer_version_supersedes_and_older_never_overwrites(conn, workspace, fake):
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake)
    oid = _purchase(conn, ws, "PO-DEMO-W01", acc, amazon_no="111-0000000-1000001")
    v1 = writeback.enqueue_for_order(conn, ws, oid)
    oid2 = _purchase(conn, ws, "PO-DEMO-W01", acc, amazon_no="111-0000000-1000002")   # 新的成功结果（版本 2）
    v2 = writeback.enqueue_for_order(conn, ws, oid2)
    assert _wb(conn, v1)["status"] == "superseded" and _wb(conn, v2)["result_version"] == 2
    _verify(conn, c["id"])
    writeback.flush(conn, client_factory=lambda: fake)
    assert _wb(conn, v2)["status"] == "done"
    assert fake.record(DEMO_APP, DEMO_TABLE, "recDEMOW001")["AMZ订单号"] == text("111-0000000-1000002")
    # 旧版本被意外改回 pending：发送前发现有新版本 → superseded，不发
    conn.execute("UPDATE procure.writebacks SET status = 'pending' WHERE id = %s", (v1,))
    calls = len(fake.updates)
    stats = writeback.flush(conn, client_factory=lambda: fake)
    assert stats["superseded"] == 1 and len(fake.updates) == calls
    assert fake.record(DEMO_APP, DEMO_TABLE, "recDEMOW001")["AMZ订单号"] == text("111-0000000-1000002")
    with pytest.raises(DomainError) as e:
        writeback.retry(conn, ws, v1, None)
    assert "取代" in e.value.message
    # 旧版本 failed、新版本也 failed：重试旧版本 → 标 superseded，不会发送旧结果
    conn.execute("UPDATE procure.writebacks SET status = 'failed' WHERE id IN (%s, %s)", (v1, v2))
    out = writeback.retry(conn, ws, v1, None)
    assert out["id"] == v1 and out["status"] == "superseded"


def test_rebinding_result_table_fails_old_pending_and_targets_new_table(conn, workspace, fake):
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake, verified=True)
    fake.add_table(DEMO_APP, DEMO_RESULT_TABLE, "结果DEMO",
                   [("平台订单号", 1), ("AMZ订单号", 1), ("税前总额USD", 2), ("采购状态", 3)])
    fake.add_record(DEMO_APP, DEMO_RESULT_TABLE, "recDEMORES01", {"平台订单号": text("PO-DEMO-W01")})
    fake.add_record(DEMO_APP, DEMO_RESULT_TABLE, "recDEMORES02", {"平台订单号": text("PO-DEMO-OTHER")})
    old = writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, "PO-DEMO-W01", acc))
    out = fc.update(conn, ws, c["id"], {"result_table_id": DEMO_RESULT_TABLE,
                                        "writeback_map": {"amazon_order_no": "AMZ订单号",
                                                          "total_before_tax": "税前总额USD",
                                                          "purchase_status": "采购状态"}}, None, client=fake)
    assert out["writeback_state"] == "unverified" and out["writeback_test_record"] is None
    row = _wb(conn, old)
    assert row["status"] == "failed" and "改绑" in row["last_error"] and row["table_id"] == DEMO_TABLE
    with pytest.raises(DomainError) as e:
        writeback.retry(conn, ws, old, None)
    assert "不会改投新表" in e.value.message
    assert writeback.list_writebacks(conn, ws, status="failed")["rows"][0]["target_changed"] is True
    # 新结果写进新表：发送前按平台订单号定位记录
    new = writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, "PO-DEMO-W01", acc))
    assert _wb(conn, old)["status"] == "superseded"
    assert _wb(conn, new)["table_id"] == DEMO_RESULT_TABLE and _wb(conn, new)["record_id"] == ""
    _verify(conn, c["id"])
    stats = writeback.flush(conn, client_factory=lambda: fake)
    assert stats["done"] == 1 and _wb(conn, new)["record_id"] == "recDEMORES01"
    assert {u["table_id"] for u in fake.updates} == {DEMO_RESULT_TABLE}       # 旧表从未被写
    assert fake.record(DEMO_APP, DEMO_RESULT_TABLE, "recDEMORES01")["采购状态"] == "已拍单"


def test_result_table_record_not_found_backs_off(conn, workspace, fake):
    ws = workspace["id"]
    fake.add_table(DEMO_APP, DEMO_RESULT_TABLE, "结果DEMO", [("平台订单号", 1), ("AMZ订单号", 1)])
    acc, c = _setup(conn, ws, fake, result_table_id=DEMO_RESULT_TABLE, writeback_map={"amazon_order_no": "AMZ订单号"},
                    verified=True)
    wid = writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, "PO-DEMO-W01", acc))
    stats = writeback.flush(conn, client_factory=lambda: fake)
    assert stats["retrying"] == 1 and "找不到平台订单号 PO-DEMO-W01" in _wb(conn, wid)["last_error"]
    fake.add_record(DEMO_APP, DEMO_RESULT_TABLE, "recDEMORES01", {"平台订单号": text("PO-DEMO-W01")})
    fake.add_record(DEMO_APP, DEMO_RESULT_TABLE, "recDEMORES02", {"平台订单号": text("PO-DEMO-W01")})
    stats = writeback.flush(conn, now=NOW + timedelta(minutes=1), client_factory=lambda: fake)
    assert stats["failed"] == 1 and "2 条" in _wb(conn, wid)["last_error"]


def test_changing_writeback_columns_rebuilds_pending_rows(conn, workspace, fake):
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake, verified=True)
    wid = writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, "PO-DEMO-W01", acc, amazon_no="111-0000000-2000001"))
    out = fc.update(conn, ws, c["id"], {"writeback_map": {"amazon_order_no": "备注", "total": "运费USD"}}, None,
                    client=fake)
    assert out["writeback_state"] == "unverified"
    row = _wb(conn, wid)
    assert row["status"] == "pending" and row["fields"] == {"备注": "111-0000000-2000001", "运费USD": 44.49}


def test_flush_without_feishu_credentials_keeps_rows_untouched(conn, workspace, fake):
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake, verified=True)
    wid = writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, "PO-DEMO-W01", acc))

    def not_configured():
        raise DomainError("FEISHU_NOT_CONFIGURED", fc.NOT_CONFIGURED_MESSAGE, 409)

    stats = writeback.flush(conn, client_factory=not_configured)
    assert stats["error"] == fc.NOT_CONFIGURED_MESSAGE and stats["due"] == 1
    row = _wb(conn, wid)
    assert row["status"] == "pending" and row["tries"] == 0


def test_writebacks_are_workspace_scoped(conn, workspace, fake):
    ws = workspace["id"]
    ws_b = f.make_workspace(conn)["id"]
    acc, c = _setup(conn, ws, fake)
    wid = writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, "PO-DEMO-W01", acc))
    conn.execute("UPDATE procure.writebacks SET status = 'failed', last_error = 'DEMO' WHERE id = %s", (wid,))
    assert writeback.list_writebacks(conn, ws_b) == {"total": 0, "rows": []}
    for call in (lambda: writeback.retry(conn, ws_b, wid, None), lambda: writeback.get(conn, ws_b, wid)):
        with pytest.raises(DomainError) as e:
            call()
        assert e.value.code == "NOT_FOUND"
    assert writeback.list_writebacks(conn, ws, status="failed")["total"] == 1
    assert writeback.list_writebacks(conn, ws, status="done")["total"] == 0
    with pytest.raises(DomainError):
        writeback.list_writebacks(conn, ws, status="bogus")
    got = writeback.get(conn, ws, wid)
    assert got["upstream_order_no"] == "PO-DEMO-W01" and got["record_id"] == "recDEMOW001"


def test_writeback_workflow_sends_due_rows(conn, workspace, fake):
    ws = workspace["id"]
    acc, c = _setup(conn, ws, fake, verified=True)
    wid = writeback.enqueue_for_order(conn, ws, _purchase(conn, ws, "PO-DEMO-W01", acc))
    conn.commit()
    out = writeback_workflow.run()
    assert out["done"] == 1
    with db.pg_conn() as c2:
        assert c2.execute("SELECT status FROM procure.writebacks WHERE id = %s", (wid,)).fetchone()["status"] == "done"
    assert writeback.backoff_minutes(1) == 1 and writeback.backoff_minutes(4) == 10 and writeback.backoff_minutes(9) == 30
    assert Decimal("41.97") == Decimal(str(fake.record(DEMO_APP, DEMO_TABLE, "recDEMOW001")["税前总额USD"]))
