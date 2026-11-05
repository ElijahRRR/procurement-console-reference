"""插件设备：配对码（一次性、过期）、令牌认证、心跳形状、身份比对、设备列表与改绑、撤销（契约 §5.1–5.3、§10）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import accounts, clock, devices, execution
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _events(conn, entity_id, kind):
    return conn.execute("SELECT * FROM procure.events WHERE entity = 'device' AND entity_id = %s AND kind = %s "
                        "ORDER BY id", (entity_id, kind)).fetchall()


# ── 配对 ──────────────────────────────────────────────────────────────────────


def test_pairing_code_shape_hash_only_and_ttl(conn, workspace):
    out = devices.create_pairing_code(conn, workspace["id"], workspace["user_id"])
    code = out["code"]
    assert len(code) == 8 and all(ch in devices.PAIRING_ALPHABET for ch in code)
    assert not set(code) & set("0O1IL")
    assert datetime.fromisoformat(out["expires_at"]) == NOW + timedelta(minutes=10)
    row = conn.execute("SELECT * FROM procure.device_pairings").fetchone()
    assert row["code_hash"] != code and len(row["code_hash"]) == 64 and row["created_by"] == workspace["user_id"]


def test_pair_returns_token_and_workspace_and_code_is_one_time(conn, workspace):
    code = devices.create_pairing_code(conn, workspace["id"], workspace["user_id"])["code"]
    out = devices.pair(conn, code.lower()[:4] + "-" + code[4:], device_name="Mac-AdsPower-DEMO", browser="AdsPower",
                       ext_version="0.1.0", user_agent="DEMO-UA")
    assert set(out) == {"device_id", "device_token", "workspace_name"}
    assert out["workspace_name"] == "测试工作区" and len(out["device_token"]) >= 40
    dev = devices.authenticate(conn, out["device_token"])
    assert dev["id"] == out["device_id"] and dev["workspace_id"] == workspace["id"]
    assert dev["browser"] == "adspower" and dev["token_hash"] != out["device_token"]
    assert dev["identity_status"] == "unknown" and dev["last_seen_at"] == NOW
    with pytest.raises(DomainError) as e:
        devices.pair(conn, code, device_name="第二台", browser="chrome", ext_version="0.1.0")
    assert e.value.code == "VALIDATION" and e.value.message == "配对码无效或已过期"
    assert f.count(conn, "devices") == 1
    assert _events(conn, dev["id"], "device.paired")


def test_pair_code_expires_after_ttl(conn, workspace):
    code = devices.create_pairing_code(conn, workspace["id"], workspace["user_id"])["code"]
    clock.freeze(NOW + timedelta(minutes=10))
    with pytest.raises(DomainError) as e:
        devices.pair(conn, code, device_name="Mac-DEMO", browser="chrome", ext_version="0.1.0")
    assert e.value.code == "VALIDATION" and e.value.message == "配对码无效或已过期"
    clock.freeze(NOW + timedelta(minutes=9, seconds=59))
    assert devices.pair(conn, code, device_name="Mac-DEMO", browser="chrome", ext_version="0.1.0")["device_id"]


@pytest.mark.parametrize("code", ["", "ABC", "ZZZZZZZZ", None, 12345678])
def test_pair_unknown_or_malformed_code(conn, workspace, code):
    devices.create_pairing_code(conn, workspace["id"], workspace["user_id"])
    with pytest.raises(DomainError) as e:
        devices.pair(conn, code, device_name="Mac-DEMO", browser="chrome", ext_version="0.1.0")
    assert e.value.code == "VALIDATION"


def test_pair_requires_name_without_consuming_code(conn, workspace):
    code = devices.create_pairing_code(conn, workspace["id"], workspace["user_id"])["code"]
    with pytest.raises(DomainError) as e:
        devices.pair(conn, code, device_name="  ", browser="chrome", ext_version="0.1.0")
    assert e.value.code == "VALIDATION" and e.value.detail["field"] == "name"
    assert devices.pair(conn, code, device_name="Mac-DEMO", browser="weird", ext_version="0.1.0")["device_id"]
    assert conn.execute("SELECT browser FROM procure.devices").fetchone()["browser"] == "other"


def test_authenticate_unknown_empty_and_revoked(conn, workspace):
    dev = fx.paired_device(conn, workspace)
    assert devices.authenticate(conn, dev["token"])["id"] == dev["id"]
    assert devices.authenticate(conn, "no-such-token") is None
    assert devices.authenticate(conn, "") is None and devices.authenticate(conn, None) is None
    devices.revoke(conn, workspace["id"], dev["id"], workspace["user_id"])
    assert devices.authenticate(conn, dev["token"]) is None
    with pytest.raises(DomainError) as e:
        devices.heartbeat(conn, dev)
    assert e.value.code == "UNAUTHORIZED" and e.value.http_status == 401


# ── 心跳与身份 ────────────────────────────────────────────────────────────────


def test_heartbeat_shape_matches_contract(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, name="环境-DEMO", min_amount="20", max_amount="50", purchase_mode="scheduled",
                          schedule_times=["14:00"])
    dev = fx.paired_device(conn, workspace, account=acc, report=None)
    clock.freeze(NOW + timedelta(seconds=30))
    out = devices.heartbeat(conn, dev, ext_version="0.2.0", identity=fx.identity(acc["amazon_customer_id"]))
    assert set(out) == {"device", "account", "identity_status", "server_time", "bj_date", "simulation_only",
                        "active_run"}
    assert out["device"] == {"id": dev["id"], "name": dev["name"], "bound_account_id": acc["id"]}
    assert set(out["account"]) == set(devices.HEARTBEAT_ACCOUNT_KEYS)
    a = out["account"]
    assert a["name"] == "环境-DEMO" and a["min_amount"] == "20.00" and a["max_amount"] == "50.00"
    assert a["schedule_times"] == ["14:00"] and a["effective_max_delivery_days"] == 10
    assert a["quota_today"] == {"limit": 20, "used": 0, "planned": 0, "available": 20}
    assert out["identity_status"] == "match" and out["simulation_only"] is True
    assert out["bj_date"] == "2026-10-31" and datetime.fromisoformat(out["server_time"]) == NOW + timedelta(seconds=30)
    assert out["active_run"] is None
    row = devices.authenticate(conn, dev["token"])
    assert row["last_seen_at"] == NOW + timedelta(seconds=30) and row["ext_version"] == "0.2.0"
    run_id = fx.start(conn, dev)
    out = devices.heartbeat(conn, dev)
    assert out["active_run"] == {"id": run_id, "device_id": dev["id"], "device_name": dev["name"], "is_mine": True}
    other = fx.paired_device(conn, workspace, account=acc)
    assert devices.heartbeat(conn, other)["active_run"]["is_mine"] is False


def test_heartbeat_unbound_device(conn, workspace):
    dev = fx.paired_device(conn, workspace)
    out = devices.heartbeat(conn, dev)
    assert out["account"] is None and out["active_run"] is None and out["identity_status"] == "unknown"
    assert out["device"]["bound_account_id"] is None


def test_identity_match_mismatch_signed_out_unknown(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc, report=None)
    cid = acc["amazon_customer_id"]
    assert devices.report_identity(conn, dev, fx.identity(cid.lower())) == "match"
    assert devices.report_identity(conn, dev, fx.identity("A9OTHER0001")) == "mismatch"
    assert devices.report_identity(conn, dev, fx.identity(None, signed_in=False)) == "signed_out"
    assert devices.report_identity(conn, dev, fx.identity(None)) == "unknown"
    no_id = fx.exec_account(conn, ws, amazon_customer_id=None)
    dev2 = fx.paired_device(conn, workspace, account=no_id, report=None)
    assert devices.report_identity(conn, dev2, fx.identity("A9OTHER0001")) == "unknown"
    row = devices.authenticate(conn, dev["token"])
    assert row["identity"]["at"] == NOW.isoformat() and row["identity_at"] == NOW
    assert row["identity"]["page_url"] == "https://www.amazon.com/gp/your-account/order-history"


def test_identity_event_only_on_status_change_and_page_url_stripped(conn, workspace):
    acc = fx.exec_account(conn, workspace["id"])
    dev = fx.paired_device(conn, workspace, account=acc, report=None)
    ident = {**fx.identity(acc["amazon_customer_id"]), "page_url": "https://www.amazon.com/gp/buy?session=SECRET#x"}
    devices.report_identity(conn, dev, ident)
    devices.report_identity(conn, dev, ident)
    devices.report_identity(conn, dev, fx.identity("A9OTHER0001"))
    evs = _events(conn, dev["id"], "device.identity_changed")
    assert [(e["detail"]["from"], e["detail"]["to"]) for e in evs] == [("unknown", "match"), ("match", "mismatch")]
    devices.report_identity(conn, dev, ident)
    assert devices.authenticate(conn, dev["token"])["identity"]["page_url"] == "https://www.amazon.com/gp/buy"
    with pytest.raises(DomainError) as e:
        devices.report_identity(conn, dev, "signed in")
    assert e.value.code == "VALIDATION"


def test_effective_identity_follows_account_customer_id_change(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    accounts.update(conn, ws, acc["id"], {"amazon_customer_id": "A9OTHER0001"}, workspace["user_id"])
    assert devices.list_devices(conn, ws)[0]["identity_status"] == "mismatch"
    assert devices.heartbeat(conn, dev)["identity_status"] == "mismatch"


# ── 列表、改绑、撤销 ──────────────────────────────────────────────────────────


def test_list_devices_shape_online_and_workspace_isolation(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, name="环境-DEMO-LIST")
    dev = fx.paired_device(conn, workspace, account=acc, name="Mac-DEMO-A")
    old = fx.paired_device(conn, workspace, name="Mac-DEMO-B")
    other_ws = f.make_workspace(conn)
    fx.paired_device(conn, other_ws, name="别人的设备")
    rows = devices.list_devices(conn, ws)
    assert [r["name"] for r in rows] == ["Mac-DEMO-A", "Mac-DEMO-B"]
    r = rows[0]
    assert set(r) == {"id", "name", "browser", "ext_version", "bound_account", "online", "last_seen_at", "identity",
                      "identity_status", "created_at", "revoked_at", "active_run"}
    assert r["bound_account"] == {"id": acc["id"], "name": "环境-DEMO-LIST"}
    assert r["online"] is True and r["identity_status"] == "match" and r["identity"]["customer_id"]
    clock.freeze(NOW + timedelta(seconds=181))
    assert devices.get_device(conn, ws, old["id"])["online"] is False
    with pytest.raises(DomainError) as e:
        devices.get_device(conn, other_ws["id"], dev["id"])
    assert e.value.code == "NOT_FOUND"


def test_update_device_rename_bind_and_validation(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    acc2 = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    out = devices.update_device(conn, ws, dev["id"], name="Mac-DEMO-改名", actor_user_id=workspace["user_id"])
    assert out["name"] == "Mac-DEMO-改名" and out["bound_account"]["id"] == acc["id"]
    # 改绑：按新账号重算身份（插件报的是 acc 的 ID → 对 acc2 不符）
    out = devices.update_device(conn, ws, dev["id"], bound_account_id=acc2["id"], actor_user_id=workspace["user_id"])
    assert out["bound_account"]["id"] == acc2["id"] and out["identity_status"] == "mismatch"
    assert devices.authenticate(conn, dev["token"])["identity_status"] == "mismatch"
    out = devices.update_device(conn, ws, dev["id"], bound_account_id=None, actor_user_id=workspace["user_id"])
    assert out["bound_account"] is None and out["identity_status"] == "unknown"
    other_ws = f.make_workspace(conn)
    foreign = fx.exec_account(conn, other_ws["id"])
    with pytest.raises(DomainError) as e:
        devices.update_device(conn, ws, dev["id"], bound_account_id=foreign["id"], actor_user_id=workspace["user_id"])
    assert e.value.code == "VALIDATION"
    with pytest.raises(DomainError) as e:
        devices.update_device(conn, other_ws["id"], dev["id"], name="x", actor_user_id=other_ws["user_id"])
    assert e.value.code == "NOT_FOUND"
    with pytest.raises(DomainError) as e:
        devices.update_device(conn, ws, dev["id"], name=" ", actor_user_id=workspace["user_id"])
    assert e.value.code == "VALIDATION"
    assert _events(conn, dev["id"], "device.updated")


def test_rebinding_refused_while_device_has_active_run(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    acc2 = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    run_id = fx.start(conn, dev)
    with pytest.raises(DomainError) as e:
        devices.update_device(conn, ws, dev["id"], bound_account_id=acc2["id"], actor_user_id=workspace["user_id"])
    assert e.value.code == "CONFLICT" and e.value.http_status == 409
    # 改名不受影响
    assert devices.update_device(conn, ws, dev["id"], name="改名-DEMO", actor_user_id=workspace["user_id"])["name"]
    execution.finish_run(conn, dev, run_id)
    out = devices.update_device(conn, ws, dev["id"], bound_account_id=acc2["id"], actor_user_id=workspace["user_id"])
    assert out["bound_account"]["id"] == acc2["id"]


def test_revoke_nulls_token_ends_runs_and_is_idempotent(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, default_device_id=None)
    dev = fx.paired_device(conn, workspace, account=acc)
    accounts.update(conn, ws, acc["id"], {"default_device_id": dev["id"]}, workspace["user_id"])
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    att = fx.claim(conn, dev, run_id)
    out = devices.revoke(conn, ws, dev["id"], workspace["user_id"])
    assert out["revoked_at"] == NOW.isoformat() and out["online"] is False
    row = conn.execute("SELECT * FROM procure.devices WHERE id = %s", (dev["id"],)).fetchone()
    assert row["token_hash"] is None
    run = fx.run_row(conn, run_id)
    assert run["ended_at"] == NOW and run["end_reason"] == "device_revoked"
    assert fx.attempt_row(conn, att["attempt_id"])["status"] == "aborted"
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "assigned" and o["assigned_account_id"] == acc["id"] and o["current_attempt_id"] is None
    assert fx.account_row(conn, acc["id"])["default_device_id"] is None
    again = devices.revoke(conn, ws, dev["id"], workspace["user_id"])
    assert again["revoked_at"] == out["revoked_at"]
    assert len(_events(conn, dev["id"], "device.revoked")) == 1
    with pytest.raises(DomainError) as e:
        devices.update_device(conn, ws, dev["id"], name="x", actor_user_id=workspace["user_id"])
    assert e.value.code == "VALIDATION"


def test_revoke_cross_workspace_is_not_found(conn, workspace):
    dev = fx.paired_device(conn, workspace)
    other = f.make_workspace(conn)
    with pytest.raises(DomainError) as e:
        devices.revoke(conn, other["id"], dev["id"], other["user_id"])
    assert e.value.code == "NOT_FOUND"
    assert devices.authenticate(conn, dev["token"]) is not None
