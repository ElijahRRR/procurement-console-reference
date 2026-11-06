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
    out = events.list_for(conn, workspace["id"], entity="settings")
    assert out["total"] == 1 and out["rows"][0]["id"] == eid and out["rows"][0]["detail"] == {"x": "1.50"}
    assert out["rows"][0]["actor_name"] == "测试所有者"


def test_events_list_pages_exactly_with_names_and_workspace_scope(conn, workspace):
    """K6：分页按偏移精确取（total 是全部条数），操作者名：用户 → 显示名，设备 → 设备名，其他 → None。"""
    from tests import factories as f

    ws = workspace["id"]
    dev = f.make_device(conn, ws, name="Mac-DEMO-EV")
    for i in range(5):
        events.record(conn, ws, "settings", ws, f"demo.{i}", actor_kind="user", actor_id=workspace["user_id"])
    events.record(conn, ws, "device", dev["id"], "demo.device", actor_kind="device", actor_id=dev["id"])
    events.record(conn, ws, "settings", ws, "demo.system")
    other = f.make_workspace(conn)
    events.record(conn, other["id"], "settings", other["id"], "demo.other", actor_kind="user",
                  actor_id=other["user_id"])
    page1 = events.list_for(conn, ws, limit=3, offset=0)
    page3 = events.list_for(conn, ws, limit=3, offset=6)
    assert page1["total"] == 7 and [r["kind"] for r in page1["rows"]] == ["demo.system", "demo.device", "demo.4"]
    assert page1["rows"][0]["actor_name"] is None and page1["rows"][1]["actor_name"] == "Mac-DEMO-EV"
    assert page1["rows"][2]["actor_name"] == "测试所有者"
    assert [r["kind"] for r in page3["rows"]] == ["demo.0"]
    assert events.list_for(conn, ws, entity="device", entity_id=dev["id"])["total"] == 1
