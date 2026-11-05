"""多维表格授权的排他性（NEW-D3-security-isolation-1）：所有者工作区重新启用 / 验收时登记授权；授权给某个工作区时
其他工作区的同表连接一律停用；写回发送也按授权把关（没授权的只积压、不发送、不计失败）；db_init 回填停用未授权
工作区的同表连接。全部数据虚构。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from registry import paths
from services import clock, feishu_intake, writeback
from services import feishu_connections as fc
from services.errors import DomainError
from tests import factories as f
from tests.fake_feishu import DEMO_APP, DEMO_TABLE, FakeFeishuClient, create_demo_connection, order_fields

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


def _grant(conn, app_token=DEMO_APP):
    return conn.execute("SELECT * FROM procure.feishu_base_grants WHERE app_token = %s", (app_token,)).fetchone()


def _enabled(conn, cid) -> bool:
    return conn.execute("SELECT enabled FROM procure.feishu_connections WHERE id = %s", (cid,)).fetchone()["enabled"]


def _pending_writeback(conn, ws, fake, *, no="PO-DEMO-G01", record_id="recDEMOG001"):
    """甲工作区：连接 + 一笔真实采购成功的任务 → (连接, 写回行 id)。连接已验收、开启写回。"""
    acc = f.make_account(conn, ws, name="环境-DEMO-G", amazon_customer_id="A1DEMOG0001")
    fake.add_record(DEMO_APP, DEMO_TABLE, record_id, order_fields(no))
    c = create_demo_connection(conn, ws, fake)
    feishu_intake.run_connection(conn, ws, c["id"], trigger="manual", client=fake)
    conn.execute("UPDATE procure.feishu_connections SET writeback_verified_at = %s WHERE id = %s", (NOW, c["id"]))
    o = conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = %s",
                     (ws, no)).fetchone()
    att = f.make_attempt(conn, ws, o["id"], acc["id"], status="succeeded", simulated=False,
                         amazon_order_no="111-0000000-0000042", submitted=True)
    f.set_status(conn, o["id"], "purchased", purchased_attempt_id=att["id"], result_version=o["result_version"] + 1)
    wid = writeback.enqueue_for_order(conn, ws, o["id"])
    assert wid is not None
    return c, wid


def test_owner_reenable_after_revoke_records_grant_so_a_later_grant_needs_transfer(conn, workspace, fake):
    ws_a = workspace["id"]
    ws_b = f.make_workspace(conn)["id"]
    a = create_demo_connection(conn, ws_a, fake)
    fc.revoke_base(conn, DEMO_APP)
    assert _grant(conn) is None and not _enabled(conn, a["id"])
    fc.update(conn, ws_a, a["id"], {"enabled": True}, None, client=fake)
    g = _grant(conn)
    assert g["workspace_id"] == ws_a and g["granted_by"] == "owner_workspace"
    with pytest.raises(DomainError) as e:                 # 已归甲：不转授就拒绝
        fc.grant_base(conn, ws_b, DEMO_APP)
    assert e.value.code == "VALIDATION"
    moved = fc.grant_base(conn, ws_b, DEMO_APP, transfer=True)
    assert moved["previous_workspace_id"] == ws_a and moved["disabled_connections"] == [a["id"]]
    assert not _enabled(conn, a["id"])


def test_verify_writeback_records_the_owner_grant(conn, workspace, fake):
    ws = workspace["id"]
    c = create_demo_connection(conn, ws, fake)
    conn.execute("DELETE FROM procure.feishu_base_grants")
    fake.add_record(DEMO_APP, DEMO_TABLE, "recDEMOTESTG", order_fields("PO-DEMO-TESTG"))
    fc.verify_writeback(conn, ws, c["id"], "recDEMOTESTG", None, client=fake)
    g = _grant(conn)
    assert g["workspace_id"] == ws and g["granted_by"] == "owner_workspace"


def test_grant_without_prior_grant_disables_other_workspaces_connections(conn, workspace, fake):
    """表格还没登记授权（所有者工作区隐式使用、迁移遗留）时授权给乙：甲在这个表格上仍启用的连接一律停用。"""
    ws_a = workspace["id"]
    ws_b = f.make_workspace(conn)["id"]
    a = create_demo_connection(conn, ws_a, fake)
    conn.execute("DELETE FROM procure.feishu_base_grants")
    out = fc.grant_base(conn, ws_b, DEMO_APP)
    assert out["previous_workspace_id"] is None and out["disabled_connections"] == [a["id"]]
    assert not _enabled(conn, a["id"])
    ev = conn.execute("SELECT detail FROM procure.events WHERE workspace_id = %s AND kind = 'feishu.base_revoked'",
                      (ws_a,)).fetchone()
    assert ev["detail"]["disabled_connections"] == [a["id"]]


def test_flush_holds_rows_of_a_workspace_whose_base_is_granted_elsewhere(conn, workspace, fake):
    ws_a = workspace["id"]
    ws_b = f.make_workspace(conn)["id"]
    c, wid = _pending_writeback(conn, ws_a, fake)
    fc.grant_base(conn, ws_b, DEMO_APP, transfer=True)
    # 无论连接是怎么重新启用的（旧数据、直接改库），没有授权就不发送
    conn.execute("UPDATE procure.feishu_connections SET enabled = true WHERE id = %s", (c["id"],))
    before = len(fake.updates)
    stats = writeback.flush(conn, client_factory=lambda: fake)
    assert stats["due"] == 0 and stats["sent"] == 0 and stats["waiting_grant"] == 1
    assert len(fake.updates) == before
    row = conn.execute("SELECT status, tries FROM procure.writebacks WHERE id = %s", (wid,)).fetchone()
    assert row["status"] == "pending" and row["tries"] == 0
    assert writeback.list_writebacks(conn, ws_a)["rows"][0]["waiting_verification"] is True
    fc.grant_base(conn, ws_a, DEMO_APP, transfer=True)    # 转回甲：恢复后补发
    conn.execute("UPDATE procure.feishu_connections SET enabled = true WHERE id = %s", (c["id"],))
    stats = writeback.flush(conn, client_factory=lambda: fake)
    assert stats["sent"] == 1 and stats["done"] == 1 and stats["waiting_grant"] == 0
    assert fake.updates[-1]["records"][0]["fields"]["AMZ订单号"]


def test_owner_workspace_without_grant_row_still_sends(conn, workspace, fake):
    ws = workspace["id"]
    _c, wid = _pending_writeback(conn, ws, fake)
    conn.execute("DELETE FROM procure.feishu_base_grants")      # 所有者工作区隐式使用尚未登记的表格
    stats = writeback.flush(conn, client_factory=lambda: fake)
    assert stats["sent"] == 1 and stats["done"] == 1 and stats["waiting_grant"] == 0
    assert conn.execute("SELECT status FROM procure.writebacks WHERE id = %s", (wid,)).fetchone()["status"] == "done"


def test_schema_backfill_disables_enabled_connections_of_non_granted_workspaces(conn, workspace, fake):
    ws_a = workspace["id"]
    ws_b = f.make_workspace(conn)["id"]
    a = create_demo_connection(conn, ws_a, fake)
    dup = conn.execute(
        "INSERT INTO procure.feishu_connections "
        "(workspace_id, name, link, app_token, source_table_id, result_table_id) "
        "SELECT %s, 'DEMO 乙（迁移遗留）', link, app_token, source_table_id, result_table_id "
        "FROM procure.feishu_connections WHERE id = %s RETURNING id",
        (ws_b, a["id"]),
    ).fetchone()["id"]
    assert _enabled(conn, dup)
    conn.execute(paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    assert _grant(conn)["workspace_id"] == ws_a
    assert _enabled(conn, a["id"]) and not _enabled(conn, dup)
