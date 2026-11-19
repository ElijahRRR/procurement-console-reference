"""买家账号：新建与校验、账号形状（额度、所在浏览器、执行批次）、改配置触发分配、暂停 / 恢复、改买家号 ID 的拒绝条件、
待登记的买家号（发现 / 忽略 / 恢复）、跨工作区。"""

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
    "pause_reason", "paused_at", "purchase_mode", "schedule_times", "expected_card_last4",
    "notes", "quota_today", "browsers", "active_run", "created_at", "updated_at", "pause_cause", "open_result_unknown",
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
    assert acc["browsers"] == [] and acc["active_run"] is None
    assert "devices" not in acc and "default_device_id" not in acc
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
    ({"name": "X", "default_device_id": 999999}, "default_device_id"),      # 已删除的字段：按未知字段拒绝
    ({"name": "X", "devices": []}, "devices"),
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


def test_browsers_active_run_and_quota_in_shape(conn, workspace):
    """browsers = 此刻登录着这个买家号、未移除的浏览器（与哪台电脑、有没有执行过无关）。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws, daily_limit=4, amazon_customer_id="A1DEMO00000")
    online = f.make_device(conn, ws, name="Mac-DEMO-ON", customer_id="A1DEMO00000")
    off = f.make_device(conn, ws, name="Mac-DEMO-OFF", customer_id="A1DEMO00000",
                        last_seen_at=NOW - timedelta(minutes=5))
    f.make_device(conn, ws, name="Mac-DEMO-GONE", customer_id="A1DEMO00000", hidden_at=NOW)          # 已移除
    f.make_device(conn, ws, name="Mac-DEMO-OTHER", customer_id="A2TEST00000")                        # 别的买家号
    f.make_device(conn, ws, name="Mac-DEMO-OUT",
                  identity={"signed_in": False, "customer_id": "A1DEMO00000"})                       # 已退出登录
    f.make_device(conn, ws, name="Mac-DEMO-SPLIT",
                  identity={"signed_in": True, "customer_id": "A1DEMO00000", "conflict": True})      # 读数不一致
    f.make_device(conn, f.make_workspace(conn)["id"], name="别的工作区", customer_id="A1DEMO00000")
    run = f.make_run(conn, ws, acc["id"], online["id"])
    o = f.ingest_ready_order(conn, ws)
    f.make_attempt(conn, ws, o["id"], acc["id"], status="succeeded")
    o2 = f.ingest_ready_order(conn, ws)
    f.set_status(conn, o2["id"], "assigned", assigned_account_id=acc["id"], assignment_kind="auto")
    got = accounts.get(conn, ws, acc["id"])
    assert set(got) == ACCOUNT_KEYS | {"archived_at"}
    assert got["browsers"] == [
        {"id": off["id"], "name": "Mac-DEMO-OFF", "online": False,
         "last_seen_at": (NOW - timedelta(minutes=5)).isoformat()},
        {"id": online["id"], "name": "Mac-DEMO-ON", "online": True, "last_seen_at": NOW.isoformat()}]
    assert got["active_run"] == {"id": run["id"], "device_id": online["id"], "device_name": "Mac-DEMO-ON",
                                 "started_at": NOW.isoformat()}
    assert got["quota_today"] == {"limit": 4, "used": 1, "planned": 1, "available": 2}
    # 没登记 ID 的账号、已归档的账号：没有所在浏览器
    assert accounts.get(conn, ws, f.make_account(conn, ws)["id"])["browsers"] == []
    conn.execute("UPDATE procure.execution_runs SET ended_at = %s, end_reason = 'finished' WHERE id = %s",
                 (NOW, run["id"]))
    assert accounts.archive(conn, ws, acc["id"], workspace["user_id"])["browsers"] == []


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


def test_adopt_and_default_device_are_gone():
    assert not hasattr(accounts, "adopt_customer_id")
    assert "default_device_id" not in accounts._EDITABLE


# ── 改买家号 ID ───────────────────────────────────────────────────────────────


def test_changing_customer_id_refused_while_account_is_executing_or_unverified(conn, workspace):
    """账号有未结束批次、在途或结果待核实的尝试时，改 / 清空买家号 ID 都拒绝（执行中的身份检查以它为基准）。"""
    from services import execution
    from tests import factories_exec as fx

    ws, uid = workspace["id"], workspace["user_id"]
    acc = fx.exec_account(conn, ws)
    old = acc["amazon_customer_id"]
    dev = fx.logged_in_device(conn, workspace, account=acc)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)

    def refused():
        for new in ("A2TEST00000", None, ""):
            with pytest.raises(DomainError) as e:
                accounts.update(conn, ws, acc["id"], {"amazon_customer_id": new}, uid)
            assert e.value.code == "CONFLICT" and e.value.http_status == 409
            assert e.value.message == "账号正在执行或有结果待核实的采购，处理完后才能修改买家号 ID"
        assert fx.account_row(conn, acc["id"])["amazon_customer_id"] == old

    refused()                                                    # ① 有未结束的批次
    # 其他字段照常能改；原样回传同一个 ID 不算修改
    assert accounts.update(conn, ws, acc["id"], {"notes": "DEMO 备注", "amazon_customer_id": old.lower()},
                           uid)["notes"] == "DEMO 备注"
    att = fx.claim(conn, dev, run_id)
    fx.to_submitted(conn, dev, att)
    refused()                                                    # ② 有在途尝试
    execution.report_result(conn, dev, att["attempt_id"], fx.failure_payload(None, "DEMO 结果不明", "unknown"))
    execution.finish_run(conn, dev, run_id)
    assert fx.attempt_row(conn, att["attempt_id"])["status"] == "unknown"
    refused()                                                    # ③ 批次结束了，但还有结果待核实的尝试
    conn.execute("UPDATE procure.purchase_attempts SET status = 'failed' WHERE id = %s", (att["attempt_id"],))
    out = accounts.update(conn, ws, acc["id"], {"amazon_customer_id": "a2test00000"}, uid)
    assert out["amazon_customer_id"] == "A2TEST00000"
    ev = conn.execute("SELECT detail FROM procure.events WHERE entity = 'account' AND entity_id = %s "
                      "AND kind = 'account.updated' ORDER BY id DESC LIMIT 1", (acc["id"],)).fetchone()
    assert ev["detail"]["changes"]["amazon_customer_id"] == [old, "A2TEST00000"]        # 事件里记下旧值
    cleared = accounts.update(conn, ws, acc["id"], {"amazon_customer_id": None}, uid)
    assert cleared["amazon_customer_id"] is None and cleared["browsers"] == []


# ── 待登记的买家号 ────────────────────────────────────────────────────────────


def _discover_setup(conn, workspace):
    """两个没登记的买家号（X 在两台浏览器上，Y 在一台上）、一个已登记的、若干不该出现的。"""
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-已登记", amazon_customer_id="A1DEMO00001")
    f.make_account(conn, ws, name="环境-DEMO-已归档", amazon_customer_id="A1DEMO00009", archived_at=NOW)
    f.make_device(conn, ws, name="已登记的浏览器", customer_id="A1DEMO00001")
    x1 = f.make_device(conn, ws, name="浏览器-X1", customer_id="A2TEST0000X", last_seen_at=NOW - timedelta(hours=2))
    x2 = f.make_device(conn, ws, name="浏览器-X2", customer_id="A2TEST0000X", last_seen_at=NOW - timedelta(minutes=1))
    y = f.make_device(conn, ws, name="浏览器-Y", customer_id="A2TEST0000Y", last_seen_at=NOW - timedelta(minutes=30))
    arch = f.make_device(conn, ws, name="浏览器-归档号", customer_id="A1DEMO00009", last_seen_at=NOW - timedelta(days=3))
    f.make_device(conn, ws, name="已移除", customer_id="A2TEST0000Z", hidden_at=NOW)
    f.make_device(conn, ws, name="未登录", identity={"signed_in": False, "customer_id": "A2TEST0000W"})
    f.make_device(conn, ws, name="不一致", identity={"signed_in": True, "customer_id": "A2TEST0000V", "conflict": True})
    f.make_device(conn, ws, name="没读到", identity={"signed_in": True, "customer_id": None})
    f.make_device(conn, ws, name="未上报")
    other = f.make_workspace(conn)
    f.make_device(conn, other["id"], name="别的工作区", customer_id="A2TEST0000Q")
    return {"ws": ws, "uid": workspace["user_id"], "x1": x1, "x2": x2, "y": y, "arch": arch, "other": other}


def test_discovered_lists_unregistered_ids_grouped_newest_first(conn, workspace):
    e = _discover_setup(conn, workspace)
    items = accounts.discovered(conn, e["ws"])
    assert [i["customer_id"] for i in items] == ["A2TEST0000X", "A2TEST0000Y", "A1DEMO00009"]
    x = items[0]
    assert set(x) == {"customer_id", "ignored", "browsers", "last_reported_at", "name_hint", "source_orders"}
    assert x["ignored"] is False and x["name_hint"] is None and x["source_orders"] == 0     # 只来自浏览器
    assert x["last_reported_at"] == (NOW - timedelta(minutes=1)).isoformat()              # 两台里最近的一次
    assert x["browsers"] == [
        {"id": e["x1"]["id"], "name": "浏览器-X1", "online": False,
         "last_seen_at": (NOW - timedelta(hours=2)).isoformat()},
        {"id": e["x2"]["id"], "name": "浏览器-X2", "online": True,
         "last_seen_at": (NOW - timedelta(minutes=1)).isoformat()}]
    assert [b["name"] for b in items[2]["browsers"]] == ["浏览器-归档号"]                # 只登记在已归档账号上 = 没登记
    assert accounts.discovered_count(conn, e["ws"]) == 3
    assert [i["customer_id"] for i in accounts.discovered(conn, e["other"]["id"])] == ["A2TEST0000Q"]
    assert accounts.discovered(conn, f.make_workspace(conn)["id"]) == []


def test_registering_a_discovered_id_removes_it_from_the_list(conn, workspace):
    e = _discover_setup(conn, workspace)
    ws, uid = e["ws"], e["uid"]
    # 登记 = 新建账号时带上这个 ID
    acc = accounts.create(conn, ws, {"name": "环境-DEMO-X", "amazon_customer_id": "A2TEST0000X"}, uid)
    assert [b["name"] for b in acc["browsers"]] == ["浏览器-X1", "浏览器-X2"]
    assert [i["customer_id"] for i in accounts.discovered(conn, ws)] == ["A2TEST0000Y", "A1DEMO00009"]
    # 填到已有账号 = 修改一个还没登记 ID 的账号
    blank = accounts.create(conn, ws, {"name": "环境-DEMO-空"}, uid)
    out = accounts.update(conn, ws, blank["id"], {"amazon_customer_id": "A2TEST0000Y"}, uid)
    assert [b["id"] for b in out["browsers"]] == [e["y"]["id"]]
    assert [i["customer_id"] for i in accounts.discovered(conn, ws)] == ["A1DEMO00009"]
    assert accounts.discovered_count(conn, ws) == 1


def test_ignore_and_unignore_buyer_id(conn, workspace):
    e = _discover_setup(conn, workspace)
    ws, uid = e["ws"], e["uid"]
    assert accounts.ignore_buyer_id(conn, ws, " a2test0000x ", uid) == {"customer_id": "A2TEST0000X", "ignored": True}
    assert accounts.ignore_buyer_id(conn, ws, "A2TEST0000X", uid)["ignored"] is True          # 幂等
    row = conn.execute("SELECT * FROM procure.ignored_buyer_ids").fetchall()
    assert [(r["workspace_id"], r["customer_id"], r["created_by"]) for r in row] == [(ws, "A2TEST0000X", uid)]
    assert [i["customer_id"] for i in accounts.discovered(conn, ws)] == ["A2TEST0000Y", "A1DEMO00009"]
    both = accounts.discovered(conn, ws, include_ignored=True)
    assert [(i["customer_id"], i["ignored"]) for i in both] == [
        ("A2TEST0000X", True), ("A2TEST0000Y", False), ("A1DEMO00009", False)]
    assert accounts.discovered_count(conn, ws) == 2
    # 别的工作区不受影响（忽略名单按工作区分）
    f.make_device(conn, e["other"]["id"], name="同一个 ID", customer_id="A2TEST0000X")
    assert "A2TEST0000X" in [i["customer_id"] for i in accounts.discovered(conn, e["other"]["id"])]
    assert accounts.unignore_buyer_id(conn, ws, "a2test0000x", uid) == {"customer_id": "A2TEST0000X", "ignored": False}
    assert accounts.unignore_buyer_id(conn, ws, "A2TEST0000X", uid)["ignored"] is False       # 幂等
    assert accounts.unignore_buyer_id(conn, ws, "A2TEST0000N", uid)["ignored"] is False       # 没忽略过也成功
    assert [i["customer_id"] for i in accounts.discovered(conn, ws)][0] == "A2TEST0000X"
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM procure.events WHERE workspace_id = %s AND kind LIKE 'account.buyer_id_%%' ORDER BY id",
        (ws,)).fetchall()]
    assert kinds == ["account.buyer_id_ignored", "account.buyer_id_unignored"]                # 重复操作不重复记事件
    detail = conn.execute("SELECT entity, entity_id, actor_kind, actor_id, detail FROM procure.events "
                          "WHERE kind = 'account.buyer_id_ignored'").fetchone()
    assert detail == {"entity": "account", "entity_id": None, "actor_kind": "user", "actor_id": uid,
                      "detail": {"customer_id": "A2TEST0000X"}}


IGNORED_TAIL = "（这个买家号 ID 已被忽略，恢复后才能登记）"


def test_ignore_and_restore_refresh_the_reason_of_tasks_designating_the_id(conn, workspace):
    """忽略 / 恢复一个被来源指定的买家号 ID：待分配原因立即跟着变（重新分配只改原因，不写订单事件）；
    重复忽略、没有任务指定的 ID 不触发分配。"""
    ws, uid = workspace["id"], workspace["user_id"]
    f.make_account(conn, ws, name="环境-DEMO-B")
    o = f.ingest_ready_order(conn, ws, external_buyer_customer_id="A1DEMO0777")
    allocation.allocate(conn, ws, trigger="manual")
    plain = "来源指定的买家号 ID A1DEMO0777 还没有登记为账号；在「账号与浏览器」登记后会自动分配"

    def reason() -> str:
        return orders.get_row(conn, ws, o["id"])["status_reason"]

    def order_events() -> int:
        return f.count(conn, "events", "entity = 'order' AND entity_id = %s", (o["id"],))

    def runs() -> int:
        return f.count(conn, "allocation_runs", "workspace_id = %s", (ws,))

    assert reason() == plain
    n_events, n_runs = order_events(), runs()
    accounts.ignore_buyer_id(conn, ws, "a1demo0777", uid)
    assert reason() == plain + IGNORED_TAIL
    assert runs() == n_runs + 1
    assert conn.execute("SELECT trigger FROM procure.allocation_runs ORDER BY id DESC LIMIT 1").fetchone()[
        "trigger"] == "account_change"
    accounts.ignore_buyer_id(conn, ws, "A1DEMO0777", uid)                          # 重复忽略：不再分配
    accounts.ignore_buyer_id(conn, ws, "A1DEMO0888", uid)                          # 没有任务指定：不分配
    assert runs() == n_runs + 1
    accounts.unignore_buyer_id(conn, ws, "A1DEMO0777", uid)
    assert reason() == plain
    assert runs() == n_runs + 2
    accounts.unignore_buyer_id(conn, ws, "A1DEMO0777", uid)                        # 没忽略着：不分配
    accounts.unignore_buyer_id(conn, ws, "A1DEMO0888", uid)                        # 没有任务指定：不分配
    assert runs() == n_runs + 2
    assert order_events() == n_events                                              # 只改了原因
    got = orders.get_row(conn, ws, o["id"])
    assert (got["status"], got["assigned_account_id"]) == ("queued", None)


def test_ignore_does_not_reallocate_for_tasks_that_are_not_waiting(conn, workspace):
    """指定这个 ID 的任务不在待分配（例如已取消）：忽略 / 恢复不触发分配。"""
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws, external_buyer_customer_id="A1DEMO0777")
    f.set_status(conn, o["id"], "cancelled")
    n_runs = f.count(conn, "allocation_runs")
    accounts.ignore_buyer_id(conn, ws, "A1DEMO0777", uid)
    accounts.unignore_buyer_id(conn, ws, "A1DEMO0777", uid)
    assert f.count(conn, "allocation_runs") == n_runs


@pytest.mark.parametrize("bad", [None, "", "not-an-id", "B2TEST0000X", "A123", 12345678901, "A" + "9" * 70])
def test_ignore_validates_customer_id_shape(conn, workspace, bad):
    for fn in (accounts.ignore_buyer_id, accounts.unignore_buyer_id):
        with pytest.raises(DomainError) as e:
            fn(conn, workspace["id"], bad, workspace["user_id"])
        assert e.value.code == "VALIDATION" and e.value.detail["field"] == "customer_id"
    assert f.count(conn, "ignored_buyer_ids") == 0


def test_registered_id_cannot_be_ignored_and_registering_clears_the_ignore(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    f.make_device(conn, ws, name="浏览器-X", customer_id="A2TEST0000X")
    accounts.ignore_buyer_id(conn, ws, "A2TEST0000X", uid)
    accounts.ignore_buyer_id(conn, ws, "A2TEST0000Y", uid)
    # 新建账号写入这个 ID：忽略记录同时删掉
    acc = accounts.create(conn, ws, {"name": "环境-DEMO-X", "amazon_customer_id": "A2TEST0000X"}, uid)
    assert [r["customer_id"] for r in conn.execute("SELECT customer_id FROM procure.ignored_buyer_ids")] == [
        "A2TEST0000Y"]
    with pytest.raises(DomainError) as e:
        accounts.ignore_buyer_id(conn, ws, "A2TEST0000X", uid)
    assert e.value.code == "VALIDATION" and "环境-DEMO-X" in e.value.message
    # 修改账号写入另一个被忽略的 ID：同样删掉
    accounts.update(conn, ws, acc["id"], {"amazon_customer_id": "A2TEST0000Y"}, uid)
    assert f.count(conn, "ignored_buyer_ids") == 0
    # 写入失败（ID 已被占用）时不动忽略记录
    accounts.ignore_buyer_id(conn, ws, "A2TEST0000X", uid)
    other = accounts.create(conn, ws, {"name": "环境-DEMO-Z"}, uid)
    with pytest.raises(DomainError):
        accounts.update(conn, ws, other["id"], {"amazon_customer_id": "A2TEST0000Y"}, uid)
    assert f.count(conn, "ignored_buyer_ids") == 1


def test_discovered_name_hint_comes_from_source_data(conn, workspace):
    """来源数据（飞书 / 文件）里同一「买家号 ID」最近出现的「买家号」→ 登记对话框预填的账号名。"""
    ws = workspace["id"]
    f.make_device(conn, ws, name="浏览器-X", customer_id="A2TEST0000X")
    f.make_device(conn, ws, name="浏览器-Y", customer_id="A2TEST0000Y")
    f.ingest_ready_order(conn, ws, external_buyer_account="环境-DEMO-旧名", external_buyer_customer_id="A2TEST0000X")
    clock.freeze(NOW + timedelta(minutes=5))
    f.ingest_ready_order(conn, ws, external_buyer_account=" 环境-DEMO-新名 ", external_buyer_customer_id="a2test0000x ")
    f.ingest_ready_order(conn, ws, external_buyer_account=None, external_buyer_customer_id="A2TEST0000Y")
    other = f.make_workspace(conn)
    f.ingest_ready_order(conn, other["id"], external_buyer_account="别的工作区的名字",
                         external_buyer_customer_id="A2TEST0000Y")
    hints = {i["customer_id"]: i["name_hint"] for i in accounts.discovered(conn, ws)}
    assert hints == {"A2TEST0000X": "环境-DEMO-新名", "A2TEST0000Y": None}


def test_cross_workspace_account_access_is_404(conn, workspace):
    """v1 §10：跨工作区访问账号 → 404。"""
    ws, uid = workspace["id"], workspace["user_id"]
    other = f.make_workspace(conn)
    theirs = f.make_account(conn, other["id"])
    mine = f.make_account(conn, ws)
    calls = [
        lambda: accounts.get(conn, ws, theirs["id"]),
        lambda: accounts.get_row(conn, ws, theirs["id"]),
        lambda: accounts.update(conn, ws, theirs["id"], {"notes": "x"}, uid),
        lambda: accounts.pause(conn, ws, theirs["id"], "x", actor_kind="user", actor_id=uid),
        lambda: accounts.resume(conn, ws, theirs["id"], actor_kind="user", actor_id=uid),
        lambda: accounts.archive(conn, ws, theirs["id"], uid),
    ]
    for call in calls:
        with pytest.raises(DomainError) as e:
            call()
        assert e.value.code == "NOT_FOUND" and e.value.http_status == 404
    assert [a["id"] for a in accounts.list_accounts(conn, ws)] == [mine["id"]]
    assert Decimal(accounts.get(conn, ws, mine["id"])["min_amount"]) == Decimal("0")


# ── 账号卡片与执行判定一致、买家号 ID 唯一、开关与额度连续（D7-02 / D2-11 / D1-09）──────────────


def test_account_browsers_and_start_follow_current_customer_id(conn, workspace):
    """改了账号的买家号 ID：账号上的所在浏览器、浏览器列表的解析、开始批次的判定一致（都按账号当前登记的 ID 现算）。"""
    from services import devices, execution
    from tests import factories_exec as fx

    ws = {"id": workspace["id"], "user_id": workspace["user_id"]}
    acc = fx.exec_account(conn, ws["id"])
    old = acc["amazon_customer_id"]
    dev = fx.logged_in_device(conn, ws, account=acc)
    assert [b["id"] for b in accounts.get(conn, ws["id"], acc["id"])["browsers"]] == [dev["id"]]
    accounts.update(conn, ws["id"], acc["id"], {"amazon_customer_id": "A1DEMO000099"}, ws["user_id"])
    assert accounts.get(conn, ws["id"], acc["id"])["browsers"] == []
    listed = next(d for d in devices.list_devices(conn, ws["id"]) if d["id"] == dev["id"])
    assert listed["login"]["state"] == "unregistered" and listed["account"] is None
    assert [i["customer_id"] for i in accounts.discovered(conn, ws["id"])] == [old]
    with pytest.raises(DomainError) as e:
        execution.start_run(conn, devices.authenticate(conn, dev["token"]), customer_id=old, trigger="manual",
                            simulated=True)
    assert e.value.code == "ACCOUNT_NOT_REGISTERED"


def test_customer_id_unique_index_backs_the_app_check(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    accounts.create(conn, ws, {"name": "环境-DEMO-C1", "amazon_customer_id": "A1DEMO777777"}, uid)
    # 绕过应用层检查（模拟两个并发请求都通过了 SELECT 检查）：唯一索引兜底并转成 VALIDATION
    monkey = accounts._check_customer_id_unique
    accounts._check_customer_id_unique = lambda *a, **k: None
    try:
        with pytest.raises(DomainError) as e:
            accounts.create(conn, ws, {"name": "环境-DEMO-C2", "amazon_customer_id": "A1DEMO777777"}, uid)
        assert e.value.code == "VALIDATION" and e.value.detail["field"] == "amazon_customer_id"
        other = accounts.create(conn, ws, {"name": "环境-DEMO-C3"}, uid)
        with pytest.raises(DomainError) as e:
            accounts.update(conn, ws, other["id"], {"amazon_customer_id": "A1DEMO777777"}, uid)
        assert e.value.code == "VALIDATION"
    finally:
        accounts._check_customer_id_unique = monkey
    assert accounts.get(conn, ws, other["id"])["amazon_customer_id"] is None      # 失败的修改没写进去


def test_purchase_disabled_task_is_still_claimed_and_sync_flag_unchanged(conn, workspace):
    """v1 §10：关闭采购开关 → 已有任务按既定规则继续（仍可领取执行）；同步开关不受影响。"""
    from services import execution
    from tests import factories_exec as fx

    ws = {"id": workspace["id"], "user_id": workspace["user_id"]}
    acc = fx.exec_account(conn, ws["id"])
    dev = fx.logged_in_device(conn, ws, account=acc)
    o = f.ingest_ready_order(conn, ws["id"])
    allocation.allocate(conn, ws["id"], trigger="import")
    out = accounts.update(conn, ws["id"], acc["id"], {"purchase_enabled": False}, ws["user_id"])
    assert out["purchase_enabled"] is False and out["sync_enabled"] is True
    run_id = fx.start(conn, dev)
    got = fx.claim(conn, dev, run_id)
    assert got["order_id"] == o["id"]
    execution.finish_run(conn, dev, run_id)


def test_quota_is_shared_across_devices_of_one_account(conn, workspace):
    """v1 §10：同账号换电脑后继续采购 → 已用与预占额度一致：第二台电脑看到第一台用掉的额度，满额时领不到。"""
    from services import execution
    from tests import factories_exec as fx

    ws = {"id": workspace["id"], "user_id": workspace["user_id"]}
    acc = fx.exec_account(conn, ws["id"], daily_limit=1)
    mac = fx.logged_in_device(conn, ws, account=acc, name="Mac-DEMO-Q")
    pc = fx.logged_in_device(conn, ws, account=acc, name="PC-DEMO-Q")
    first = fx.assigned_order(conn, ws["id"], acc)
    fx.assigned_order(conn, ws["id"], acc)
    run1 = fx.start(conn, mac)
    att = fx.claim(conn, mac, run1)
    assert att["order_id"] == first["id"]
    fx.complete_success(conn, mac, att)
    execution.finish_run(conn, mac, run1)
    assert accounts.get(conn, ws["id"], acc["id"])["quota_today"]["used"] == 1
    run2 = fx.start(conn, pc)
    out = execution.next_attempt(conn, pc, run2)
    assert out["attempt"] is None and "额度已用完" in out["reason"]
