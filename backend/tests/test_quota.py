"""每日额度（契约 §3.1）：已用、计划、剩余。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services import clock, quota
from tests import factories as f

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def test_used_counts_inflight_unknown_succeeded_but_not_failed_or_aborted(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    for status in ("submitting", "unknown", "succeeded", "failed", "aborted"):
        o = f.ingest_ready_order(conn, ws)
        f.make_attempt(conn, ws, o["id"], acc["id"], status=status)
    assert quota.used(conn, acc["id"], TODAY, True) == 3
    assert quota.used(conn, acc["id"], TODAY, False) == 0
    assert quota.used(conn, acc["id"], TODAY + timedelta(days=1), True) == 0
    other = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws)
    f.make_attempt(conn, ws, o["id"], other["id"], status="verifying")
    assert quota.used(conn, other["id"], TODAY, True, workspace_id=ws) == 1
    assert quota.used_map(conn, ws, [TODAY], True) == {(acc["id"], TODAY): 3, (other["id"], TODAY): 1}


def test_one_success_counts_once_regardless_of_quantity(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws, quantity=4)
    f.make_attempt(conn, ws, o["id"], acc["id"], status="succeeded")
    assert quota.used(conn, acc["id"], TODAY, True) == 1


def test_external_attempts_do_not_consume_quota(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws, name="环境-DEMO-EXT")
    o = f.ingest_ready_order(conn, ws, external_amz_order_no="111-0000000-0000009",
                             external_buyer_account="环境-DEMO-EXT")
    att = conn.execute("SELECT * FROM procure.purchase_attempts WHERE purchase_order_id = %s", (o["id"],)).fetchone()
    assert att["account_id"] == acc["id"] and att["kind"] == "external"
    assert quota.used(conn, acc["id"], TODAY, False) == 0


def test_attempt_bj_date_is_creation_day_across_midnight(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    clock.freeze(datetime(2026, 10, 31, 15, 59, tzinfo=timezone.utc))   # 北京 23:59
    o = f.ingest_ready_order(conn, ws)
    f.make_attempt(conn, ws, o["id"], acc["id"], status="submitting")
    clock.freeze(datetime(2026, 10, 31, 16, 1, tzinfo=timezone.utc))    # 北京次日 00:01
    assert quota.used(conn, acc["id"], date(2026, 10, 31), True) == 1
    assert quota.used(conn, acc["id"], date(2026, 11, 1), True) == 0


def test_planned_counts_assigned_by_plan_date(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    def assigned(d, kind="auto"):
        o = f.ingest_ready_order(conn, ws, suggested_purchase_date=d)
        return f.set_status(conn, o["id"], "assigned", assigned_account_id=acc["id"], assignment_kind=kind)
    assigned(TODAY)
    assigned(TODAY - timedelta(days=2))                 # 过期未采 → 算今天
    assigned(TODAY + timedelta(days=1), kind="manual")
    running = assigned(TODAY)
    f.make_attempt(conn, ws, running["id"], acc["id"], status="checking")   # 有在途尝试 → 不算计划
    assert quota.planned(conn, acc["id"], TODAY) == 2
    assert quota.planned(conn, acc["id"], TODAY + timedelta(days=1)) == 1
    assert quota.planned(conn, acc["id"], TODAY - timedelta(days=1)) == 0
    assert quota.fixed(conn, acc["id"], TODAY + timedelta(days=1)) == 1
    assert quota.fixed(conn, acc["id"], TODAY) == 0
    assert quota.fixed_map(conn, ws, TODAY) == {(acc["id"], TODAY + timedelta(days=1)): 1}


def test_summary_available_is_limit_minus_used_minus_planned(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws, daily_limit=5)
    done = f.ingest_ready_order(conn, ws)
    f.make_attempt(conn, ws, done["id"], acc["id"], status="succeeded")
    for _ in range(2):
        o = f.ingest_ready_order(conn, ws)
        f.set_status(conn, o["id"], "assigned", assigned_account_id=acc["id"], assignment_kind="auto")
    assert quota.summary(conn, acc, TODAY, True) == {"limit": 5, "used": 1, "planned": 2, "available": 2}
    small = f.make_account(conn, ws, daily_limit=1)
    for _ in range(2):
        o = f.ingest_ready_order(conn, ws, unit_price=Decimal("20.00"))
        f.set_status(conn, o["id"], "assigned", assigned_account_id=small["id"], assignment_kind="manual")
    assert quota.summary(conn, small, None, True)["available"] == 0
