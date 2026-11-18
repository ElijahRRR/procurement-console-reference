"""浏览器（devices 行）：自动登记（幂等、并发、多工作区、上限、版本闸）、令牌认证、身份清洗、登录状态全集与解析、
心跳形状、列表 / 改名 / 移除与自动恢复（契约 §5、§10）。全部数据虚构。"""

from __future__ import annotations

import dataclasses
import threading
from datetime import datetime, timedelta, timezone

import pytest

from registry import db, settings
from services import accounts, clock, devices, execution, overview
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TOKEN = "DEMO-device-token-AAAAAAAAAAAAAAAAAAAAAAAAA"      # 43 个字符，虚构


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _events(conn, entity_id, kind):
    return conn.execute("SELECT * FROM procure.events WHERE entity = 'device' AND entity_id = %s AND kind = %s "
                        "ORDER BY id", (entity_id, kind)).fetchall()


def _err(fn, *a, **kw) -> DomainError:
    with pytest.raises(DomainError) as e:
        fn(*a, **kw)
    return e.value


def _setting(monkeypatch, **over):
    monkeypatch.setattr(settings, "_loaded", dataclasses.replace(settings.get(), **over))


# ── 登记 ──────────────────────────────────────────────────────────────────────


def test_register_creates_browser_without_any_manual_step(conn, workspace):
    out = devices.register(conn, TOKEN, ext_version="0.2.0", user_agent="DEMO-UA " + "x" * 400)
    assert set(out) == {"device_id", "device_name", "workspace_id", "workspace_name", "created"}
    assert out["created"] is True and out["workspace_id"] == workspace["id"] and out["workspace_name"] == "测试工作区"
    assert out["device_name"] == f"浏览器 {out['device_id']}"
    row = conn.execute("SELECT * FROM procure.devices WHERE id = %s", (out["device_id"],)).fetchone()
    assert row["token_hash"] != TOKEN and len(row["token_hash"]) == 64          # 库里只存 sha256
    assert row["name"] == out["device_name"] and row["ext_version"] == "0.2.0" and row["last_seen_at"] == NOW
    assert row["identity"] is None and row["login_customer_id"] is None and row["hidden_at"] is None
    assert row["browser"] is None                                              # 历史列，不再写入
    ev = _events(conn, out["device_id"], "device.registered")
    assert len(ev) == 1 and ev[0]["actor_kind"] == "device" and ev[0]["actor_id"] == out["device_id"]
    assert ev[0]["detail"]["ext_version"] == "0.2.0" and len(ev[0]["detail"]["user_agent"]) == 200
    assert devices.authenticate(conn, TOKEN)["id"] == out["device_id"]


@pytest.mark.parametrize("token", ["", "short", "a" * 31, "a" * 129, "has space " + "a" * 32, "bad/char+" + "a" * 32,
                                   "中文" + "a" * 32, "a" * 40 + "\n", " " + "a" * 40, None,
                                   12345678901234567890123456789012, ["a" * 40]])
def test_register_rejects_malformed_token(conn, workspace, token):
    e = _err(devices.register, conn, token)
    assert e.code == "VALIDATION" and e.http_status == 422 and e.detail["field"] == "device_token"
    assert f.count(conn, "devices") == 0


@pytest.mark.parametrize("token", ["a" * 32, "A-b_9" * 8, "z" * 128])
def test_register_accepts_token_shapes(conn, workspace, token):
    assert devices.register(conn, token)["created"] is True


def test_register_is_idempotent_and_restores_removed_browser(conn, workspace):
    ws = workspace["id"]
    first = devices.register(conn, TOKEN, ext_version="0.2.0")
    devices.remove_device(conn, ws, first["device_id"], workspace["user_id"])
    clock.freeze(NOW + timedelta(minutes=5))
    again = devices.register(conn, TOKEN, ext_version="0.2.1", workspace_id=987654)   # 已登记：不看 workspace_id
    assert again == {**first, "created": False}
    assert f.count(conn, "devices") == 1 and len(_events(conn, first["device_id"], "device.registered")) == 1
    row = devices.authenticate(conn, TOKEN)
    assert row["last_seen_at"] == NOW + timedelta(minutes=5) and row["ext_version"] == "0.2.1"
    assert row["hidden_at"] is None
    ev = _events(conn, first["device_id"], "device.ext_version_changed")
    assert [e["detail"] for e in ev] == [{"from": "0.2.0", "to": "0.2.1"}]
    # 改过的名字不会被重新登记冲掉
    devices.update_device(conn, ws, first["device_id"], name="AdsPower-DEMO-7", actor_user_id=workspace["user_id"])
    assert devices.register(conn, TOKEN)["device_name"] == "AdsPower-DEMO-7"


def test_concurrent_register_of_same_token_creates_one_row(clean_db, conn, workspace):
    conn.commit()
    n = 6
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i):
        try:
            with db.pg_conn(clean_db, statement_timeout_s=20) as c:
                barrier.wait(timeout=10)
                results[i] = devices.register(c, TOKEN, ext_version="0.2.0", user_agent=f"DEMO-UA-{i}")
        except Exception as exc:  # noqa: BLE001 - 带回主线程断言
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(isinstance(r, dict) for r in results), results
    assert len({r["device_id"] for r in results}) == 1
    assert sum(r["created"] for r in results) == 1
    assert f.count(conn, "devices") == 1
    assert f.count(conn, "events", "kind = 'device.registered'") == 1


def test_register_needs_initialised_server(conn):
    e = _err(devices.register, conn, TOKEN)
    assert e.code == "CONFLICT" and e.http_status == 409
    assert e.message == "服务端还没有初始化：请先打开控制台创建用户"


def test_register_single_workspace_ignores_workspace_id(conn, workspace):
    assert devices.register(conn, TOKEN, workspace_id=424242)["workspace_id"] == workspace["id"]


def test_register_with_several_workspaces_requires_existing_workspace_id(conn, workspace):
    other = f.make_workspace(conn, "DEMO 工作区 B")
    for bad in (None, "", "abc", 999999, True, -1, 2 ** 70):
        e = _err(devices.register, conn, TOKEN, workspace_id=bad)
        assert e.code == "WORKSPACE_REQUIRED" and e.http_status == 409, bad
        assert "工作区 ID" in e.message and "设置" in e.message
    assert f.count(conn, "devices") == 0
    out = devices.register(conn, TOKEN, workspace_id=str(other["id"]))
    assert out["workspace_id"] == other["id"] and out["workspace_name"] == "DEMO 工作区 B"
    mine = devices.register(conn, fx.device_token(), workspace_id=workspace["id"])
    assert mine["workspace_id"] == workspace["id"]
    # 工作区登记时确定，之后不变：换一个 workspace_id 再登记同一个令牌，还是原来那一行
    assert devices.register(conn, TOKEN, workspace_id=workspace["id"])["workspace_id"] == other["id"]
    assert [d["id"] for d in devices.list_devices(conn, other["id"])] == [out["device_id"]]
    assert [d["id"] for d in devices.list_devices(conn, workspace["id"])] == [mine["device_id"]]


def test_register_refuses_beyond_max_devices_per_workspace(conn, workspace, monkeypatch):
    _setting(monkeypatch, max_devices=2)
    other = f.make_workspace(conn)
    first = fx.device_token()
    devices.register(conn, first, workspace_id=workspace["id"])
    hidden = devices.register(conn, fx.device_token(), workspace_id=workspace["id"])
    devices.remove_device(conn, workspace["id"], hidden["device_id"], None)      # 已移除的行也占名额
    e = _err(devices.register, conn, TOKEN, workspace_id=workspace["id"])
    assert e.code == "RATE_LIMITED" and e.http_status == 429 and e.message == "登记的浏览器太多：请稍后再试"
    assert devices.register(conn, first, workspace_id=workspace["id"])["created"] is False   # 已登记的不受影响
    assert devices.register(conn, TOKEN, workspace_id=other["id"])["created"] is True        # 别的工作区另算
    assert settings.get().max_devices == 2


def test_default_limits_come_from_registry():
    s = settings.get()
    assert s.max_devices == 500 and s.device_retention_days == 30 and s.plugin_extension_ids == ()
    assert not hasattr(s, "pairing_ttl_s") and not hasattr(s, "pair_fail_limit")


@pytest.mark.parametrize("version", ["0.1.0", "0.1.9", "0.1", "0", "0.1.99.99"])
def test_version_gate_blocks_outdated_plugin_on_register_and_heartbeat(conn, workspace, version):
    e = _err(devices.register, conn, TOKEN, ext_version=version)
    assert e.code == "PLUGIN_OUTDATED" and e.http_status == 409
    assert e.message == "插件版本过旧：请重新构建并加载新版插件（需要 0.2.0 及以上）"
    assert f.count(conn, "devices") == 0
    dev = fx.registered_device(conn, workspace)
    clock.freeze(NOW + timedelta(minutes=1))
    for call in (lambda: devices.heartbeat(conn, dev, ext_version=version),
                 lambda: devices.report_identity(conn, dev, fx.identity("A1DEMO00000"), ext_version=version)):
        assert _err(call).code == "PLUGIN_OUTDATED"
    row = devices.authenticate(conn, dev["token"])
    assert row["last_seen_at"] == NOW and row["ext_version"] == "0.2.0" and row["identity"] is None


@pytest.mark.parametrize("version", ["0.2.0", "0.2", "0.10.0", "1.0.0.5", None, "", "dev-build", "0.2.0-beta", 2])
def test_version_gate_lets_current_missing_and_unparsable_versions_through(conn, workspace, version):
    dev = fx.registered_device(conn, workspace)
    assert devices.heartbeat(conn, dev, ext_version=version)["device"]["id"] == dev["id"]
    assert devices.register(conn, dev["token"], ext_version=version)["created"] is False


def test_authenticate_and_require_active(conn, workspace):
    dev = fx.registered_device(conn, workspace)
    assert devices.authenticate(conn, dev["token"])["id"] == dev["id"]
    assert devices.authenticate(conn, "no-such-token") is None
    assert devices.authenticate(conn, "") is None and devices.authenticate(conn, None) is None
    assert devices.require_active(conn, dev)["id"] == dev["id"]
    assert devices.require_active(conn, dev, lock=True)["id"] == dev["id"]
    # 被移除的浏览器照样能认证（移除只清列表，不动令牌）
    devices.remove_device(conn, workspace["id"], dev["id"], workspace["user_id"])
    assert devices.authenticate(conn, dev["token"])["id"] == dev["id"]
    # 行被清扫删掉之后：按「还没有登记」处理，插件会用同一个令牌重新登记
    conn.execute("DELETE FROM procure.devices WHERE id = %s", (dev["id"],))
    e = _err(devices.heartbeat, conn, dev)
    assert e.code == "UNAUTHORIZED" and e.http_status == 401
    assert e.message == devices.UNREGISTERED_MESSAGE == "这个浏览器还没有登记，插件会自动重新登记"
    assert devices.authenticate(conn, dev["token"]) is None
    assert not any(hasattr(devices, name) for name in ("create_pairing_code", "pair", "revoke",
                                                       "compute_identity_status", "effective_identity_status"))


# ── 身份清洗 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,clean", [
    ("A1DEMO00000", "A1DEMO00000"), ("  a1demo00000 ", "A1DEMO00000"), ("A1DEMO 000\t00", "A1DEMO00000"),
    (None, None), ("", None), ("   ", None), ("not-an-id", None), ("B1DEMO00000", None), ("A1234567", None),
    ("A1DEMO-0000", None), ("A" + "9" * 64, None), (12345678901, None), (["A1DEMO00000"], None),
    ({"id": "A1DEMO00000"}, None), (True, None),
])
def test_clean_identity_customer_id_never_raises(raw, clean):
    out = devices.clean_identity({"signed_in": True, "customer_id": raw, "site": "US", "page_url": None})
    assert out["customer_id"] == clean
    assert devices.login_customer_id(out) == clean


def test_clean_identity_shape_flags_and_page_url():
    out = devices.clean_identity({"signed_in": True, "customer_id": "A1DEMO00000", "site": "US-EAST-LONG",
                                  "page_url": "https://www.amazon.com/gp/buy/spc?session=SECRET&x=1#frag",
                                  "conflict": 1, "cookies": "never stored", "account_name": "Demo"})
    assert out == {"signed_in": True, "customer_id": "A1DEMO00000", "site": "US-EAST-", "conflict": True,
                   "page_url": "https://www.amazon.com/gp/buy/spc"}
    assert devices.login_customer_id(out) is None                    # 有分歧时不能拿它领任务
    assert devices.clean_identity({"signed_in": "true", "customer_id": "A1DEMO00000"})["signed_in"] is False
    assert devices.clean_identity({})  == {"signed_in": False, "customer_id": None, "site": None, "page_url": None,
                                           "conflict": False}
    assert devices.clean_identity({"page_url": "/gp/css?x=1#y"})["page_url"] == "/gp/css"
    assert devices.login_customer_id({"signed_in": False, "customer_id": "A1DEMO00000", "conflict": False}) is None
    assert devices.login_customer_id(None) is None
    for bad in ("signed in", None, ["x"], 5):
        with pytest.raises(DomainError) as e:
            devices.clean_identity(bad)
        assert e.value.code == "VALIDATION"


def test_report_with_bad_customer_id_is_stored_as_not_read(conn, workspace):
    dev = fx.registered_device(conn, workspace)
    out = devices.report_identity(conn, dev, fx.identity("<script>alert(1)</script>"))
    assert out["login"] == {"state": "no_id", "customer_id": None, "at": NOW.isoformat(), "stale": False}
    row = devices.authenticate(conn, dev["token"])
    assert row["identity"]["customer_id"] is None and row["login_customer_id"] is None
    assert row["identity"]["at"] == NOW.isoformat() and row["identity_at"] == NOW
    assert _err(devices.report_identity, conn, dev, "signed in").code == "VALIDATION"
    assert _err(devices.report_identity, conn, dev, None).code == "VALIDATION"


# ── 登录状态与解析 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("identity,login_id,state", [
    (None, None, "unknown"),
    ({}, None, "unknown"),
    ({"signed_in": True, "customer_id": "A1DEMO00000", "conflict": True}, None, "conflict"),
    ({"signed_in": False, "customer_id": None, "conflict": True}, None, "conflict"),
    ({"signed_in": False, "customer_id": "A1DEMO00000", "conflict": False}, None, "signed_out"),
    ({"signed_in": True, "customer_id": None, "conflict": False}, None, "no_id"),
    ({"signed_in": True, "customer_id": "A1DEMO00000"}, "A1DEMO00000", "signed_in"),      # 旧库的身份没有 conflict 键
    ({"signed_in": True, "customer_id": "A1DEMO00000", "conflict": False}, "A1DEMO00000", "signed_in"),
])
def test_login_state_is_a_pure_function_of_the_row(identity, login_id, state):
    assert devices.login_state({"identity": identity, "login_customer_id": login_id}) == state
    assert state in devices.LOGIN_STATES


def test_plugin_states_full_set_through_heartbeat(conn, workspace):
    """插件侧状态全集：unknown | signed_out | no_id | conflict | unregistered | ignored | ready。"""
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, name="环境-DEMO-A")
    cid = acc["amazon_customer_id"]
    dev = fx.registered_device(conn, workspace)
    seen = [devices.heartbeat(conn, dev)["login"]]
    assert seen[0] == {"state": "unknown", "customer_id": None, "at": None, "stale": False}
    stranger = fx.customer_id(777_001)
    for ident in (fx.identity(None, signed_in=False), fx.identity(None), fx.identity(cid, conflict=True),
                  fx.identity(stranger)):
        seen.append(devices.heartbeat(conn, dev, identity=ident)["login"])
    accounts.ignore_buyer_id(conn, ws, stranger, workspace["user_id"])
    seen.append(devices.heartbeat(conn, dev)["login"])
    out = devices.heartbeat(conn, dev, identity=fx.identity(cid.lower()))
    seen.append(out["login"])
    assert [s["state"] for s in seen] == ["unknown", "signed_out", "no_id", "conflict", "unregistered", "ignored",
                                          "ready"]
    assert set(s["state"] for s in seen) == set(devices.PLUGIN_STATES)
    assert [s["customer_id"] for s in seen] == [None, None, None, None, stranger, stranger, cid]
    assert out["account"]["id"] == acc["id"] and out["account"]["name"] == "环境-DEMO-A"
    # account 只在 ready 时非空
    assert devices.heartbeat(conn, dev, identity=fx.identity(stranger))["account"] is None
    assert devices.heartbeat(conn, dev, identity=fx.identity(cid, conflict=True))["account"] is None


def test_resolve_is_computed_every_time_and_scoped_to_the_browser_workspace(conn, workspace):
    ws = workspace["id"]
    cid = fx.customer_id()
    other_ws = f.make_workspace(conn)
    fx.exec_account(conn, other_ws["id"], amazon_customer_id=cid, name="别的工作区的账号")
    dev = fx.registered_device(conn, workspace)
    assert devices.report_identity(conn, dev, fx.identity(cid))["login"]["state"] == "unregistered"
    row = devices.authenticate(conn, dev["token"])
    assert devices.resolve_login(conn, row) == ("unregistered", None)
    # 所有者登记了这个 ID：不用等下一次身份上报，下一次心跳就是 ready
    acc = accounts.create(conn, ws, {"name": "环境-DEMO-新", "amazon_customer_id": cid}, workspace["user_id"])
    state, got = devices.resolve_login(conn, row)
    assert state == "ready" and got["id"] == acc["id"]
    assert devices.heartbeat(conn, dev)["account"]["id"] == acc["id"]
    # 改掉账号的 ID → 回到未登记；改回来、归档 → 未登记（已归档账号不算登记）
    accounts.update(conn, ws, acc["id"], {"amazon_customer_id": fx.customer_id()}, workspace["user_id"])
    assert devices.heartbeat(conn, dev)["login"]["state"] == "unregistered"
    accounts.update(conn, ws, acc["id"], {"amazon_customer_id": cid}, workspace["user_id"])
    assert devices.heartbeat(conn, dev)["login"]["state"] == "ready"
    accounts.archive(conn, ws, acc["id"], workspace["user_id"])
    out = devices.heartbeat(conn, dev)
    assert out["login"]["state"] == "unregistered" and out["account"] is None
    assert f.count(conn, "buyer_accounts", "workspace_id = %s", (ws,)) == 1          # 服务端不替插件建账号


def test_login_stale_after_identity_fresh_window(conn, workspace):
    acc = fx.exec_account(conn, workspace["id"])
    dev = fx.logged_in_device(conn, workspace, account=acc)
    fresh_s = settings.get().identity_fresh_s
    assert fresh_s == 600
    clock.freeze(NOW + timedelta(seconds=fresh_s))
    assert devices.heartbeat(conn, dev)["login"]["stale"] is False
    clock.freeze(NOW + timedelta(seconds=fresh_s + 1))
    out = devices.heartbeat(conn, dev)                     # 心跳不带身份：不刷新 identity_at
    assert out["login"] == {"state": "ready", "customer_id": acc["amazon_customer_id"], "at": NOW.isoformat(),
                            "stale": True}
    assert devices.list_devices(conn, workspace["id"])[0]["login"]["stale"] is True
    out = devices.heartbeat(conn, dev, identity=fx.identity(acc["amazon_customer_id"]))
    assert out["login"]["stale"] is False and out["login"]["at"] == (NOW + timedelta(seconds=fresh_s + 1)).isoformat()


def test_identity_changed_event_only_when_state_or_id_changes(conn, workspace):
    acc = fx.exec_account(conn, workspace["id"])
    cid, other = acc["amazon_customer_id"], fx.customer_id(777_002)
    dev = fx.registered_device(conn, workspace)
    for ident in (fx.identity(cid), fx.identity(cid), fx.identity(other), fx.identity(other, conflict=True),
                  fx.identity(None, signed_in=False), fx.identity(None, signed_in=False)):
        devices.report_identity(conn, dev, ident)
    evs = _events(conn, dev["id"], "device.identity_changed")
    assert [e["detail"] for e in evs] == [
        {"from": {"state": "unknown", "customer_id": None}, "to": {"state": "signed_in", "customer_id": cid}},
        {"from": {"state": "signed_in", "customer_id": cid}, "to": {"state": "signed_in", "customer_id": other}},
        {"from": {"state": "signed_in", "customer_id": other}, "to": {"state": "conflict", "customer_id": None}},
        {"from": {"state": "conflict", "customer_id": None}, "to": {"state": "signed_out", "customer_id": None}},
    ]
    assert all(e["actor_kind"] == "device" and e["actor_id"] == dev["id"] for e in evs)


# ── 心跳 ──────────────────────────────────────────────────────────────────────


def test_heartbeat_shape_matches_contract(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, name="环境-DEMO", min_amount="20", max_amount="50", purchase_mode="scheduled",
                          schedule_times=["14:00"], expected_card_last4="7860")
    dev = fx.logged_in_device(conn, workspace, account=acc, report=None)
    clock.freeze(NOW + timedelta(seconds=30))
    out = devices.heartbeat(conn, dev, ext_version="0.2.1", identity=fx.identity(acc["amazon_customer_id"]))
    assert set(out) == {"device", "login", "account", "device_run", "server_time", "bj_date", "simulation_only"}
    assert out["device"] == {"id": dev["id"], "name": dev["name"]}
    assert out["login"] == {"state": "ready", "customer_id": acc["amazon_customer_id"],
                            "at": (NOW + timedelta(seconds=30)).isoformat(), "stale": False}
    a = out["account"]
    assert set(a) == set(devices.HEARTBEAT_ACCOUNT_KEYS) == {
        "id", "name", "amazon_customer_id", "purchase_enabled", "execution_paused", "pause_reason", "purchase_mode",
        "schedule_times", "effective_max_delivery_days", "quota_today", "executable_today", "future_assigned",
        "active_run"}
    # 卡尾号与金额区间只在领取后的尝试载荷里，心跳不下发
    assert not {"expected_card_last4", "min_amount", "max_amount", "daily_limit"} & set(a)
    assert a["id"] == acc["id"] and a["name"] == "环境-DEMO" and a["amazon_customer_id"] == acc["amazon_customer_id"]
    assert a["purchase_enabled"] is True and a["execution_paused"] is False and a["pause_reason"] is None
    assert a["purchase_mode"] == "scheduled" and a["schedule_times"] == ["14:00"]
    assert a["effective_max_delivery_days"] == 10
    assert a["quota_today"] == {"limit": 20, "used": 0, "planned": 0, "available": 20}
    assert a["executable_today"] == 0 and a["future_assigned"] == 0 and a["active_run"] is None
    assert out["device_run"] is None and out["simulation_only"] is True
    assert out["bj_date"] == "2026-10-31" and datetime.fromisoformat(out["server_time"]) == NOW + timedelta(seconds=30)
    row = devices.authenticate(conn, dev["token"])
    assert row["last_seen_at"] == NOW + timedelta(seconds=30) and row["ext_version"] == "0.2.1"
    # 身份上报接口返回同一形状
    again = devices.report_identity(conn, dev, fx.identity(acc["amazon_customer_id"]), ext_version="0.2.1")
    assert again == out


def test_heartbeat_counts_match_claim_rules_and_are_not_capped_by_quota(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, daily_limit=1)
    other = fx.exec_account(conn, ws)
    today = clock.bj_today()
    fx.assigned_order(conn, ws, acc)                                                  # 今天
    fx.assigned_order(conn, ws, acc, suggested_purchase_date=today - timedelta(days=2))   # 过期未采，也算今天
    fx.assigned_order(conn, ws, acc, suggested_purchase_date=today + timedelta(days=1))   # 未来
    fx.assigned_order(conn, ws, acc, suggested_purchase_date=today + timedelta(days=3))   # 未来
    held = fx.assigned_order(conn, ws, acc)
    conn.execute("UPDATE procure.purchase_orders SET conflict = '{\"kind\": \"demo\"}' WHERE id = %s", (held["id"],))
    fx.assigned_order(conn, ws, other)                                                # 别的账号的
    f.ingest_ready_order(conn, ws)                                                    # 待分配的
    dev = fx.logged_in_device(conn, workspace, account=acc)
    a = devices.heartbeat(conn, dev)["account"]
    assert a["executable_today"] == 2 and a["future_assigned"] == 2      # 额度只有 1，可执行笔数照实给 2
    assert a["quota_today"]["limit"] == 1 and a["quota_today"]["used"] == 0
    assert execution.claimable_counts(conn, ws, acc["id"]) == {"executable_today": 2, "future_assigned": 2}
    run_id = fx.start(conn, dev)
    fx.claim(conn, dev, run_id)                                          # 领走一笔：在途的不再算可执行
    a = devices.heartbeat(conn, dev)["account"]
    assert a["executable_today"] == 1 and a["quota_today"]["used"] == 1


def test_heartbeat_account_run_and_device_run(conn, workspace):
    ws = workspace["id"]
    acc_a = fx.exec_account(conn, ws, name="环境-DEMO-A")
    acc_b = fx.exec_account(conn, ws, name="环境-DEMO-B")
    mac = fx.logged_in_device(conn, workspace, account=acc_a, name="Mac-DEMO")
    pc = fx.logged_in_device(conn, workspace, account=acc_a, name="PC-DEMO")
    run_id = fx.start(conn, mac)
    lease = fx.run_row(conn, run_id)["lease_expires_at"]
    clock.freeze(NOW + timedelta(seconds=60))
    out = devices.heartbeat(conn, mac)
    assert out["account"]["active_run"] == {"id": run_id, "device_id": mac["id"], "device_name": "Mac-DEMO",
                                            "started_at": NOW.isoformat(), "is_mine": True}
    assert out["device_run"] == {"id": run_id, "account_id": acc_a["id"], "account_name": "环境-DEMO-A",
                                 "started_at": NOW.isoformat()}
    assert fx.run_row(conn, run_id)["lease_expires_at"] == lease          # 插件心跳不续批次租约
    theirs = devices.heartbeat(conn, pc)
    assert theirs["account"]["active_run"]["is_mine"] is False and theirs["device_run"] is None
    # 这个浏览器换登了另一个买家号：account 跟着换，device_run 仍是它自己没结束的那个批次（不论账号）
    out = devices.heartbeat(conn, mac, identity=fx.identity(acc_b["amazon_customer_id"]))
    assert out["account"]["id"] == acc_b["id"] and out["account"]["active_run"] is None
    assert out["device_run"]["id"] == run_id and out["device_run"]["account_name"] == "环境-DEMO-A"
    execution.finish_run(conn, mac, run_id)
    assert devices.heartbeat(conn, mac)["device_run"] is None


def test_heartbeat_reflects_pause_and_purchase_switch(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, purchase_enabled=False)
    dev = fx.logged_in_device(conn, workspace, account=acc)
    accounts.pause(conn, ws, acc["id"], "DEMO 人工暂停", actor_kind="user", actor_id=workspace["user_id"])
    a = devices.heartbeat(conn, dev)["account"]
    assert a["purchase_enabled"] is False and a["execution_paused"] is True and a["pause_reason"] == "DEMO 人工暂停"


# ── 列表、改名、移除 ──────────────────────────────────────────────────────────


def test_list_devices_shape_states_online_and_workspace_isolation(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, name="环境-DEMO-LIST")
    ready = fx.logged_in_device(conn, workspace, account=acc, name="A-就绪")
    fx.registered_device(conn, workspace, name="B-未上报")
    stranger = fx.logged_in_device(conn, workspace, account=acc, name="C-未登记", report="mismatch")
    ignored = fx.registered_device(conn, workspace, name="D-已忽略")
    ignored_id = fx.customer_id(777_003)
    devices.report_identity(conn, ignored, fx.identity(ignored_id))
    accounts.ignore_buyer_id(conn, ws, ignored_id, workspace["user_id"])
    fx.logged_in_device(conn, workspace, account=acc, name="E-未登录", report="signed_out")
    fx.logged_in_device(conn, workspace, account=acc, name="F-没读到", report="no_id")
    fx.logged_in_device(conn, workspace, account=acc, name="G-不一致", report="conflict")
    other_ws = f.make_workspace(conn)
    fx.registered_device(conn, other_ws, name="别人的浏览器")
    run_id = fx.start(conn, ready)
    rows = devices.list_devices(conn, ws)
    assert [(r["name"], r["login"]["state"]) for r in rows] == [
        ("A-就绪", "ready"), ("B-未上报", "unknown"), ("C-未登记", "unregistered"), ("D-已忽略", "ignored"),
        ("E-未登录", "signed_out"), ("F-没读到", "no_id"), ("G-不一致", "conflict")]
    r = rows[0]
    assert set(r) == {"id", "name", "ext_version", "online", "last_seen_at", "created_at", "hidden_at", "login",
                      "account", "active_run"}
    assert set(r["login"]) == {"state", "customer_id", "at", "stale"}
    assert r["account"] == {"id": acc["id"], "name": "环境-DEMO-LIST"} and r["online"] is True
    assert r["login"]["customer_id"] == acc["amazon_customer_id"] and r["hidden_at"] is None
    assert r["active_run"] == {"id": run_id, "account_id": acc["id"], "started_at": NOW.isoformat(),
                               "simulated": True}
    assert rows[2]["account"] is None and rows[2]["login"]["customer_id"] == stranger["login_customer_id"]
    assert rows[3]["login"]["customer_id"] == ignored_id and rows[1]["active_run"] is None
    clock.freeze(NOW + timedelta(seconds=181))
    assert devices.get_device(conn, ws, ready["id"])["online"] is False
    assert _err(devices.get_device, conn, other_ws["id"], ready["id"]).code == "NOT_FOUND"
    assert _err(devices.get_device, conn, ws, "abc").code == "NOT_FOUND"
    assert [d["name"] for d in devices.list_devices(conn, other_ws["id"])] == ["别人的浏览器"]


def test_update_device_only_renames(conn, workspace):
    ws = workspace["id"]
    dev = fx.registered_device(conn, workspace)
    out = devices.update_device(conn, ws, dev["id"], name="  AdsPower-DEMO-改名 ", actor_user_id=workspace["user_id"])
    assert out["name"] == "AdsPower-DEMO-改名"
    assert devices.update_device(conn, ws, dev["id"], actor_user_id=workspace["user_id"])["name"] == out["name"]
    assert [e["detail"] for e in _events(conn, dev["id"], "device.updated")] == [
        {"changes": {"name": [dev["name"], "AdsPower-DEMO-改名"]}}]
    for bad in (" ", "", None, 5, "长" * 65):
        e = _err(devices.update_device, conn, ws, dev["id"], name=bad, actor_user_id=workspace["user_id"])
        assert e.code == "VALIDATION" and e.detail["field"] == "name"
    other_ws = f.make_workspace(conn)
    e = _err(devices.update_device, conn, other_ws["id"], dev["id"], name="x", actor_user_id=other_ws["user_id"])
    assert e.code == "NOT_FOUND"
    with pytest.raises(TypeError):                         # 没有绑定账号这回事了
        devices.update_device(conn, ws, dev["id"], bound_account_id=1, actor_user_id=workspace["user_id"])


def test_remove_only_hides_and_browser_comes_back_on_next_contact(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.logged_in_device(conn, workspace, account=acc, name="Mac-DEMO")
    assert overview.overview(conn, ws)["devices"] == {"total": 1, "online": 1}
    out = devices.remove_device(conn, ws, dev["id"], uid)
    assert out["hidden_at"] == NOW.isoformat() and out["id"] == dev["id"]
    assert devices.remove_device(conn, ws, dev["id"], uid)["hidden_at"] == NOW.isoformat()      # 幂等
    ev = _events(conn, dev["id"], "device.removed")
    assert len(ev) == 1 and ev[0]["actor_kind"] == "user" and ev[0]["actor_id"] == uid
    assert devices.list_devices(conn, ws) == []
    assert [d["id"] for d in devices.list_devices(conn, ws, include_hidden=True)] == [dev["id"]]
    assert devices.get_device(conn, ws, dev["id"])["hidden_at"] is not None
    assert overview.overview(conn, ws)["devices"] == {"total": 0, "online": 0}
    assert accounts.get(conn, ws, acc["id"])["browsers"] == []
    row = conn.execute("SELECT * FROM procure.devices WHERE id = %s", (dev["id"],)).fetchone()
    assert row["token_hash"] == dev["token_hash"]                        # 不动令牌
    # 再次联系（心跳 / 身份上报 / 登记）自动回到列表，还是原来的 id
    clock.freeze(NOW + timedelta(minutes=1))
    assert devices.heartbeat(conn, dev)["device"] == {"id": dev["id"], "name": "Mac-DEMO"}
    assert [d["id"] for d in devices.list_devices(conn, ws)] == [dev["id"]]
    assert accounts.get(conn, ws, acc["id"])["browsers"][0]["id"] == dev["id"]
    devices.remove_device(conn, ws, dev["id"], uid)
    devices.report_identity(conn, dev, fx.identity(acc["amazon_customer_id"]))
    assert devices.get_device(conn, ws, dev["id"])["hidden_at"] is None
    devices.remove_device(conn, ws, dev["id"], uid)
    assert devices.register(conn, dev["token"])["device_id"] == dev["id"]
    assert devices.get_device(conn, ws, dev["id"])["hidden_at"] is None
    assert len(_events(conn, dev["id"], "device.removed")) == 3


def test_remove_refused_while_browser_has_active_run(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.logged_in_device(conn, workspace, account=acc)
    run_id = fx.start(conn, dev)
    e = _err(devices.remove_device, conn, ws, dev["id"], uid)
    assert e.code == "CONFLICT" and e.http_status == 409 and e.message == "这个浏览器正在执行，结束后才能移除"
    assert e.detail["run_id"] == run_id
    assert fx.run_row(conn, run_id)["ended_at"] is None and not _events(conn, dev["id"], "device.removed")
    execution.finish_run(conn, dev, run_id)
    assert devices.remove_device(conn, ws, dev["id"], uid)["hidden_at"] is not None


def test_remove_cross_workspace_is_not_found(conn, workspace):
    dev = fx.registered_device(conn, workspace)
    other = f.make_workspace(conn)
    assert _err(devices.remove_device, conn, other["id"], dev["id"], other["user_id"]).code == "NOT_FOUND"
    assert devices.get_device(conn, workspace["id"], dev["id"])["hidden_at"] is None
