"""基座自检：建表、时钟、模型序列化。"""

from datetime import date, datetime, timezone
from decimal import Decimal

from services import clock, events
from services.models import UpstreamRecord, money


def test_schema_tables_exist(conn):
    names = {r["tablename"] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'procure'").fetchall()}
    for t in ("purchase_orders", "purchase_attempts", "execution_runs", "buyer_accounts",
              "devices", "feishu_connections", "import_batches", "writebacks", "events"):
        assert t in names


def test_bj_date_crosses_midnight():
    # 2026-10-27 16:30 UTC = 2026-10-28 00:30 北京
    ts = datetime(2026, 10, 27, 16, 30, tzinfo=timezone.utc)
    assert clock.bj_date(ts) == date(2026, 10, 28)
    clock.freeze(ts)
    try:
        assert clock.bj_today() == date(2026, 10, 28)
        assert clock.site_today("US") == date(2026, 10, 27)
    finally:
        clock.unfreeze()


def test_record_roundtrip_and_money():
    r = UpstreamRecord(upstream_order_no="P1", unit_price=money("$1,041.9"), quantity=2,
                       suggested_purchase_date=date(2026, 10, 31))
    back = UpstreamRecord.from_json(r.to_json())
    assert back.unit_price == Decimal("1041.90")
    assert back.suggested_purchase_date == date(2026, 10, 31)
    assert back.execution_fingerprint() == r.execution_fingerprint()
    assert money("") is None and money(None) is None and money("abc") is None


def test_events_record(conn, workspace):
    eid = events.record(conn, workspace["id"], "settings", workspace["id"], "settings.updated",
                        actor_kind="user", actor_id=workspace["user_id"], detail={"x": Decimal("1.50")})
    rows = events.list_for(conn, workspace["id"], entity="settings")
    assert rows[0]["id"] == eid and rows[0]["detail"] == {"x": "1.50"}
