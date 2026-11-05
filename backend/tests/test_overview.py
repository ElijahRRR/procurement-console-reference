"""概览（契约 §10）与后台分配命令。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from services import allocation, clock, orders, overview
from tests import factories as f
from workflows import allocate as allocate_wf

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def test_overview_shape_and_counts(conn, workspace):
    ws = workspace["id"]
    empty = overview.overview(conn, ws)
    assert set(empty) == {"bj_date", "counts", "needs_attention", "today_executable", "future_assigned", "accounts",
                          "devices", "recent_exceptions", "last_allocation", "simulation_only", "feishu_configured"}
    assert empty["bj_date"] == "2026-10-31" and empty["last_allocation"] is None
    assert empty["simulation_only"] is True and isinstance(empty["feishu_configured"], bool)
    assert set(empty["counts"]) == set(orders.ORDER_STATUS_ZH) and sum(empty["counts"].values()) == 0

    acc = f.make_account(conn, ws)
    paused = f.make_account(conn, ws, execution_paused=True)
    f.make_account(conn, ws, purchase_enabled=False)
    f.make_account(conn, ws, archived_at=NOW)
    f.make_device(conn, ws, bound_account_id=acc["id"])
    f.make_device(conn, ws, last_seen_at=NOW - timedelta(minutes=10))
    f.make_device(conn, ws, revoked_at=NOW)
    f.ingest_ready_order(conn, ws)
    f.ingest_ready_order(conn, ws, suggested_purchase_date=TODAY + timedelta(days=2))
    f.ingest_ready_order(conn, ws, review_value=None, review="unknown")
    bad = f.ingest_ready_order(conn, ws)
    f.set_status(conn, bad["id"], "exception", status_reason="商品无库存")
    allocation.allocate(conn, ws, trigger="manual")
    out = overview.overview(conn, ws)
    assert out["counts"]["assigned"] == 2 and out["counts"]["pending_review"] == 1 and out["counts"]["exception"] == 1
    assert out["today_executable"] == 1 and out["future_assigned"] == 1
    assert out["accounts"] == {"total": 3, "enabled": 2, "paused": 1}
    assert out["devices"] == {"total": 2, "online": 1}
    assert [r["id"] for r in out["recent_exceptions"]] == [bad["id"]]
    assert out["last_allocation"]["trigger"] == "manual"
    assert paused["id"]


def test_overview_is_workspace_scoped(conn, workspace):
    other = f.make_workspace(conn)
    f.ingest_ready_order(conn, other["id"])
    f.make_account(conn, other["id"])
    out = overview.overview(conn, workspace["id"])
    assert sum(out["counts"].values()) == 0 and out["accounts"]["total"] == 0


def test_allocate_workflow_runs_every_workspace_in_own_transaction(conn, workspace):
    ws = workspace["id"]
    other = f.make_workspace(conn)
    f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws)
    f.ingest_ready_order(conn, other["id"])
    conn.commit()
    out = allocate_wf.run(trigger="daily")
    assert set(out["workspaces"]) == {str(ws), str(other["id"])}
    assert out["workspaces"][str(ws)]["trigger"] == "daily"
    assert out["workspaces"][str(ws)]["stats"]["assigned"] == 1
    assert out["workspaces"][str(other["id"])]["stats"]["unassigned"] == 1
    assert orders.get_row(conn, ws, o["id"])["status"] == "assigned"
    one = allocate_wf.run(workspace_id=str(ws))                      # CLI 缺省 = 人工「重新分配」
    assert list(one["workspaces"]) == [str(ws)] and one["workspaces"][str(ws)]["trigger"] == "manual"
