"""买家账号：新建与校验、账号形状（额度、设备、执行批次）、改配置触发分配、暂停 / 恢复、采用买家号 ID、跨工作区。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services import accounts, allocation, clock, orders, settings_svc
from services.errors import DomainError
from tests import factories as f

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00

ACCOUNT_KEYS = {
    "id", "name", "amazon_customer_id", "site", "min_amount", "max_amount", "daily_limit", "priority",
    "max_delivery_days", "effective_max_delivery_days", "purchase_enabled", "sync_enabled", "execution_paused",
    "pause_reason", "paused_at", "purchase_mode", "schedule_times", "default_device_id", "expected_card_last4",
    "notes", "quota_today", "devices", "active_run", "created_at", "updated_at",
}


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def test_create_and_get_shape(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = accounts.create(conn, ws, {"name": " 环境-DEMO-1 ", "min_amount": "20", "max_amount": "50.5",
                                     "daily_limit": 15, "priority": 7, "purchase_mode": "scheduled",
                                     "schedule_times": ["14:00", "09:30", "14:00"],
                                     "amazon_customer_id": "a1demo00000"}, uid)
    assert ACCOUNT_KEYS <= set(acc)
    assert acc["name"] == "环境-DEMO-1" and acc["min_amount"] == "20.00" and acc["max_amount"] == "50.50"
    assert acc["schedule_times"] == ["09:30", "14:00"] and acc["amazon_customer_id"] == "A1DEMO00000"
    assert acc["max_delivery_days"] is None and acc["effective_max_delivery_days"] == 10
    assert acc["quota_today"] == {"limit": 15, "used": 0, "planned": 0, "available": 15}
    assert acc["devices"] == [] and acc["active_run"] is None
    assert accounts.get(conn, ws, acc["id"]) == acc
    assert conn.execute("SELECT kind FROM procure.events WHERE entity = 'account'").fetchone()["kind"] == \
        "account.created"


@pytest.mark.parametrize("data,field", [
    ({"name": ""}, "name"),
    ({"name": "X", "min_amount": "30", "max_amount": "20"}, "min_amount"),
    ({"name": "X", "min_amount": "-1"}, "min_amount"),
    ({"name": "X", "min_amount": "abc"}, "min_amount"),
    ({"name": "X", "priority": 10}, "priority"),
    ({"name": "X", "priority": True}, "priority"),
    ({"name": "X", "daily_limit": -1}, "daily_limit"),
    ({"name": "X", "max_delivery_days": 0}, "max_delivery_days"),
    ({"name": "X", "schedule_times": ["25:00"]}, "schedule_times"),
    ({"name": "X", "purchase_mode": "scheduled", "schedule_times": []}, "schedule_times"),
    ({"name": "X", "purchase_mode": "auto"}, "purchase_mode"),
    ({"name": "X", "amazon_customer_id": "B123"}, "amazon_customer_id"),
    ({"name": "X", "expected_card_last4": "12a4"}, "expected_card_last4"),
    ({"name": "X", "site": "UK"}, "site"),
    ({"name": "X", "default_device_id": 999999}, "default_device_id"),
    ({"name": "X", "bogus": 1}, "bogus"),
])
def test_create_validation(conn, workspace, data, field):
    with pytest.raises(DomainError) as e:
        accounts.create(conn, workspace["id"], data, workspace["user_id"])
    assert e.value.code == "VALIDATION" and e.value.detail.get("field") == field


def test_name_unique_per_workspace_but_not_across(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    accounts.create(conn, ws, {"name": "环境-DEMO-U"}, uid)
    with pytest.raises(DomainError) as e:
        accounts.create(conn, ws, {"name": "环境-DEMO-U"}, uid)
    assert e.value.code == "VALIDATION"
    other = f.make_workspace(conn)
    assert accounts.create(conn, other["id"], {"name": "环境-DEMO-U"}, other["user_id"])["name"] == "环境-DEMO-U"


def test_effective_max_delivery_days_override_and_default(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws)
    b = f.make_account(conn, ws, max_delivery_days=5)
    assert accounts.effective_max_delivery_days(conn, ws, a) == 10
    assert accounts.effective_max_delivery_days(conn, ws, b) == 5
    settings_svc.update(conn, ws, {"default_max_delivery_days": 12}, uid)
    assert accounts.effective_max_delivery_days(conn, ws, a) == 12
    assert accounts.get(conn, ws, a["id"])["effective_max_delivery_days"] == 12


def test_devices_active_run_and_quota_in_shape(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws, daily_limit=4)
    online = f.make_device(conn, ws, name="Mac-DEMO-ON", bound_account_id=acc["id"], identity_status="match")
    f.make_device(conn, ws, name="Mac-DEMO-OFF", bound_account_id=acc["id"], last_seen_at=NOW - timedelta(minutes=5))
    f.make_device(conn, ws, name="Mac-DEMO-GONE", bound_account_id=acc["id"], revoked_at=NOW)
    run = f.make_run(conn, ws, acc["id"], online["id"])
    o = f.ingest_ready_order(conn, ws)
    f.make_attempt(conn, ws, o["id"], acc["id"], status="succeeded")
    o2 = f.ingest_ready_order(conn, ws)
    f.set_status(conn, o2["id"], "assigned", assigned_account_id=acc["id"], assignment_kind="auto")
    got = accounts.get(conn, ws, acc["id"])
    assert [(d["name"], d["online"], d["identity_status"]) for d in got["devices"]] == [
        ("Mac-DEMO-OFF", False, "unknown"), ("Mac-DEMO-ON", True, "match")]
    assert got["active_run"] == {"id": run["id"], "device_id": online["id"], "device_name": "Mac-DEMO-ON",
                                 "started_at": NOW.isoformat()}
    assert got["quota_today"] == {"limit": 4, "used": 1, "planned": 1, "available": 2}


def test_update_affecting_allocation_reallocates(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = accounts.create(conn, ws, {"name": "环境-DEMO-R", "min_amount": "30", "max_amount": "50"}, uid)
    o = f.ingest_ready_order(conn, ws)             # $25：不在区间
    allocation.allocate(conn, ws, trigger="import")
    assert orders.get_row(conn, ws, o["id"])["status"] == "queued"
    accounts.update(conn, ws, acc["id"], {"min_amount": "20"}, uid)
    row = orders.get_row(conn, ws, o["id"])
    assert row["status"] == "assigned" and row["assigned_account_id"] == acc["id"]
    assert allocation.last_run(conn, ws)["trigger"] == "account_change"
    runs = f.count(conn, "allocation_runs")
    accounts.update(conn, ws, acc["id"], {"notes": "DEMO 备注"}, uid)          # 不影响分配 → 不重新分配
    assert f.count(conn, "allocation_runs") == runs
    accounts.update(conn, ws, acc["id"], {"notes": "DEMO 备注"}, uid)          # 无变化
    assert f.count(conn, "events", "kind = 'account.updated'") == 2


def test_update_cross_field_validation_uses_current_values(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws, min_amount="20", max_amount="50")
    with pytest.raises(DomainError):
        accounts.update(conn, ws, acc["id"], {"max_amount": "10"}, uid)
    out = accounts.update(conn, ws, acc["id"], {"max_amount": None}, uid)
    assert out["max_amount"] is None


def test_purchase_disabled_keeps_existing_and_stops_new(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = accounts.create(conn, ws, {"name": "环境-DEMO-S"}, uid)
    o = f.ingest_ready_order(conn, ws)
    allocation.allocate(conn, ws, trigger="import")
    accounts.update(conn, ws, acc["id"], {"purchase_enabled": False}, uid)
    assert orders.get_row(conn, ws, o["id"])["assigned_account_id"] == acc["id"]
    o2 = f.ingest_ready_order(conn, ws)
    allocation.allocate(conn, ws, trigger="import")
    assert orders.get_row(conn, ws, o2["id"])["status"] == "queued"


def test_pause_and_resume(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    p = accounts.pause(conn, ws, acc["id"], "结果待核实", actor_kind="device", actor_id=None)
    assert p["execution_paused"] and p["pause_reason"] == "结果待核实" and p["paused_at"] == NOW.isoformat()
    clock.freeze(NOW + timedelta(minutes=5))
    p = accounts.pause(conn, ws, acc["id"], "  ", actor_kind="user", actor_id=uid)
    assert p["pause_reason"] == "人工暂停" and p["paused_at"] == NOW.isoformat()
    o = f.ingest_ready_order(conn, ws)
    allocation.allocate(conn, ws, trigger="manual")
    assert orders.get_row(conn, ws, o["id"])["status"] == "queued"
    r = accounts.resume(conn, ws, acc["id"], actor_kind="user", actor_id=uid)
    assert not r["execution_paused"] and r["pause_reason"] is None
    assert orders.get_row(conn, ws, o["id"])["assigned_account_id"] == acc["id"]      # 恢复后立即重新分配
    kinds = [e["kind"] for e in conn.execute("SELECT kind FROM procure.events WHERE entity = 'account' ORDER BY id")]
    assert kinds == ["account.paused", "account.paused", "account.resumed"]


def test_archive_unassigns_and_excludes_from_list(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    gone = f.make_account(conn, ws, name="环境-DEMO-OLD")
    o = f.ingest_ready_order(conn, ws)
    allocation.allocate(conn, ws, trigger="manual")
    accounts.archive(conn, ws, gone["id"], uid)
    assert orders.get_row(conn, ws, o["id"])["status"] == "queued"
    assert accounts.list_accounts(conn, ws) == []
    assert [a["name"] for a in accounts.list_accounts(conn, ws, include_archived=True)] == ["环境-DEMO-OLD"]
    with pytest.raises(DomainError):
        accounts.update(conn, ws, gone["id"], {"notes": "x"}, uid)


def test_archive_refused_while_attempt_in_flight(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws)
    f.make_attempt(conn, ws, o["id"], acc["id"], status="unknown")
    with pytest.raises(DomainError) as e:
        accounts.archive(conn, ws, acc["id"], uid)
    assert e.value.code == "CONFLICT"


def test_adopt_customer_id_from_bound_device(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    dev = f.make_device(conn, ws, bound_account_id=acc["id"], identity_status="unknown",
                        identity={"signed_in": True, "customer_id": "A2DEMO0000X", "site": "US"})
    out = accounts.adopt_customer_id(conn, ws, acc["id"], dev["id"], uid)
    assert out["amazon_customer_id"] == "A2DEMO0000X"
    assert out["devices"][0]["identity_status"] == "match"
    unbound = f.make_device(conn, ws, identity={"customer_id": "A2DEMO0000Y"})
    with pytest.raises(DomainError) as e:
        accounts.adopt_customer_id(conn, ws, acc["id"], unbound["id"], uid)
    assert e.value.code == "VALIDATION"
    bad = f.make_device(conn, ws, bound_account_id=acc["id"], identity={"customer_id": "not-an-id"})
    with pytest.raises(DomainError):
        accounts.adopt_customer_id(conn, ws, acc["id"], bad["id"], uid)
    other_acc = f.make_account(conn, ws)
    twin = f.make_device(conn, ws, bound_account_id=other_acc["id"], identity={"customer_id": "A2DEMO0000X"})
    with pytest.raises(DomainError) as e:
        accounts.adopt_customer_id(conn, ws, other_acc["id"], twin["id"], uid)
    assert "已登记在账号" in e.value.message


def test_cross_workspace_account_access_is_404(conn, workspace):
    """v1 §10：跨工作区访问账号与设备 → 404。"""
    ws, uid = workspace["id"], workspace["user_id"]
    other = f.make_workspace(conn)
    theirs = f.make_account(conn, other["id"])
    their_dev = f.make_device(conn, other["id"], identity={"customer_id": "A3DEMO00000"})
    mine = f.make_account(conn, ws)
    calls = [
        lambda: accounts.get(conn, ws, theirs["id"]),
        lambda: accounts.get_row(conn, ws, theirs["id"]),
        lambda: accounts.update(conn, ws, theirs["id"], {"notes": "x"}, uid),
        lambda: accounts.pause(conn, ws, theirs["id"], "x", actor_kind="user", actor_id=uid),
        lambda: accounts.resume(conn, ws, theirs["id"], actor_kind="user", actor_id=uid),
        lambda: accounts.adopt_customer_id(conn, ws, mine["id"], their_dev["id"], uid),
    ]
    for call in calls:
        with pytest.raises(DomainError) as e:
            call()
        assert e.value.code == "NOT_FOUND" and e.value.http_status == 404
    assert [a["id"] for a in accounts.list_accounts(conn, ws)] == [mine["id"]]
    assert Decimal(accounts.get(conn, ws, mine["id"])["min_amount"]) == Decimal("0")
