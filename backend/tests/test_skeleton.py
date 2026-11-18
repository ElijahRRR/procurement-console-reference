"""基座自检：建表、时钟、模型序列化。"""

from datetime import date, datetime, timezone
from decimal import Decimal

from services import clock, events
from services.models import UpstreamRecord, money


def test_schema_tables_exist(conn):
    names = {r["tablename"] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'procure'").fetchall()}
    for t in ("purchase_orders", "purchase_attempts", "execution_runs", "buyer_accounts",
              "devices", "ignored_buyer_ids", "feishu_connections", "import_batches", "writebacks", "events"):
        assert t in names
    assert "device_pairings" not in names                      # 配对码表已删除


def test_api_error_codes_catalogue_covers_everything_services_raise():
    """models.API_ERROR_CODES 是接口统一错误码（契约 §8 的顺序）；服务层、接口适配层抛出的码都在里面。"""
    import re

    from registry import paths
    from services.models import API_ERROR_CODES

    assert API_ERROR_CODES == (
        "UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "VALIDATION", "CONFLICT", "ACCOUNT_BUSY", "DEVICE_BUSY",
        "ATTEMPT_IN_FLIGHT", "IDENTITY_MISMATCH", "ACCOUNT_NOT_REGISTERED", "ACCOUNT_PAUSED", "REAL_PURCHASE_DISABLED",
        "LEASE_LOST", "SLOT_DONE", "WORKSPACE_REQUIRED", "PLUGIN_OUTDATED", "FEISHU_NOT_CONFIGURED", "FEISHU_ERROR",
        "INVALID_TRANSITION", "SOURCE_CHANGED", "GUARD_BLOCKED", "RATE_LIMITED", "METHOD_NOT_ALLOWED", "INTERNAL")
    assert len(set(API_ERROR_CODES)) == len(API_ERROR_CODES) and "NOT_BOUND" not in API_ERROR_CODES
    used: set[str] = set()
    for folder in ("services", "api", "workflows"):
        for path in (paths.BACKEND_DIR / folder).glob("*.py"):
            used |= set(re.findall(r'(?:code=|DomainError\(\s*)"([A-Z_]+)"', path.read_text(encoding="utf-8")))
    assert {"DEVICE_BUSY", "SLOT_DONE", "ACCOUNT_NOT_REGISTERED", "WORKSPACE_REQUIRED", "PLUGIN_OUTDATED"} <= used
    assert used - set(API_ERROR_CODES) == set()


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
