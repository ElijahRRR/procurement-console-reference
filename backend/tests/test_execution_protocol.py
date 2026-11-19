"""执行协议：开始批次的各项拒绝、执行权互斥、批次心跳、领取规则（日期门槛、额度、跨零点）、进度与上游取消、
提交登记前置条件、归属校验（跨工作区 404）、租约失效（v1 §10 / 方案 v2 §4 验收条目）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from services import accounts, clock, devices, execution, orders, quota
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _err(fn, *a, **kw) -> DomainError:
    with pytest.raises(DomainError) as e:
        fn(*a, **kw)
    return e.value


def _setup(conn, workspace, **acc_kw):
    acc = fx.exec_account(conn, workspace["id"], **acc_kw)
    dev = fx.logged_in_device(conn, workspace, account=acc)
    return acc, dev


# ── 开始批次 ──────────────────────────────────────────────────────────────────


def _run_events(conn, kind="run.started"):
    return conn.execute("SELECT * FROM procure.events WHERE entity = 'run' AND kind = %s ORDER BY id",
                        (kind,)).fetchall()


def test_start_run_returns_lease_account_and_writes_event(conn, workspace):
    acc, dev = _setup(conn, workspace, name="环境-DEMO-A")
    cid = acc["amazon_customer_id"]
    out = execution.start_run(conn, dev, customer_id=f" {cid.lower()} ", trigger="manual", simulated=True)
    assert set(out) == {"run_id", "lease_ttl_s", "lease_expires_at", "account"} and out["lease_ttl_s"] == 180
    assert out["account"] == {"id": acc["id"], "name": "环境-DEMO-A"}
    assert datetime.fromisoformat(out["lease_expires_at"]) == NOW + timedelta(seconds=180)
    run = fx.run_row(conn, out["run_id"])
    assert run["account_id"] == acc["id"] and run["device_id"] == dev["id"] and run["simulated"] is True
    assert run["trigger"] == "manual" and run["ended_at"] is None
    assert run["bj_date"] == TODAY and run["scheduled_slot"] is None
    ev = _run_events(conn)
    assert len(ev) == 1 and ev[0]["actor_kind"] == "device" and ev[0]["actor_id"] == dev["id"]
    assert ev[0]["detail"] == {"account_id": acc["id"], "trigger": "manual", "simulated": True, "customer_id": cid,
                               "scheduled_slot": None}


def test_start_run_any_browser_logged_into_the_buyer_can_start_no_binding(conn, workspace):
    """账号上登记了买家号 ID，任何登录着这个买家号的浏览器都能执行它的任务；同一个浏览器换登另一个买家号后
    执行的就是另一个账号。"""
    ws = workspace["id"]
    acc_a = fx.exec_account(conn, ws, name="环境-DEMO-A")
    acc_b = fx.exec_account(conn, ws, name="环境-DEMO-B")
    first = fx.logged_in_device(conn, workspace, account=acc_a)
    second = fx.logged_in_device(conn, workspace, account=acc_a)
    run_id = fx.start(conn, first)
    execution.finish_run(conn, first, run_id)
    assert fx.run_row(conn, fx.start(conn, second))["account_id"] == acc_a["id"]
    fx.refresh_identity(conn, first, acc_b)
    out = fx.start_run(conn, first)
    assert out["account"] == {"id": acc_b["id"], "name": "环境-DEMO-B"}


@pytest.mark.parametrize("cid", [None, "", "   "])
def test_start_run_without_customer_id_tells_old_plugin_to_update(conn, workspace, cid):
    """旧插件（0.1.0）开始批次时不带 customer_id：得到看得懂的报错。"""
    acc, dev = _setup(conn, workspace)
    e = _err(execution.start_run, conn, dev, customer_id=cid, trigger="manual", simulated=True)
    assert e.code == "VALIDATION" and e.http_status == 422 and e.message == "插件版本过旧，请更新插件"
    assert e.detail["field"] == "customer_id"
    assert _err(execution.start_run, conn, dev, trigger="manual", simulated=True).message == "插件版本过旧，请更新插件"
    assert f.count(conn, "execution_runs") == 0


@pytest.mark.parametrize("cid", ["not-an-id", "B1DEMO000001", "A123", 12345678901, ["A1DEMO000001"], "A" + "9" * 70])
def test_start_run_rejects_malformed_customer_id(conn, workspace, cid):
    acc, dev = _setup(conn, workspace)
    e = _err(execution.start_run, conn, dev, customer_id=cid, trigger="manual", simulated=True)
    assert e.code == "VALIDATION" and e.detail["field"] == "customer_id" and "格式不对" in e.message


def test_start_run_validates_trigger_and_simulated(conn, workspace):
    acc, dev = _setup(conn, workspace)
    cid = acc["amazon_customer_id"]
    assert _err(execution.start_run, conn, dev, customer_id=cid, trigger="cron", simulated=True).code == "VALIDATION"
    assert _err(execution.start_run, conn, dev, customer_id=cid, trigger="manual",
                simulated="yes").code == "VALIDATION"


@pytest.mark.parametrize("report,state,message", [
    (None, "unknown", "插件还没有上报这个浏览器的登录状态，请打开或刷新亚马逊页面后重试"),
    ("signed_out", "signed_out", "浏览器没有登录亚马逊"),
    ("conflict", "conflict", "这个浏览器的亚马逊标签页显示的买家号不一致，请刷新或关闭较早打开的标签页"),
    ("no_id", "no_id", "插件没有读到浏览器登录的买家号 ID，请打开亚马逊订单页后重试"),
])
def test_start_run_refused_unless_browser_is_signed_in(conn, workspace, report, state, message):
    acc = fx.exec_account(conn, workspace["id"])
    dev = fx.logged_in_device(conn, workspace, account=acc, report=report)
    e = _err(execution.start_run, conn, dev, customer_id=acc["amazon_customer_id"], trigger="manual", simulated=True)
    assert e.code == "IDENTITY_MISMATCH" and e.http_status == 403 and e.message == message
    assert set(e.detail) == {"login_state", "identity_at", "stale"}
    assert e.detail["login_state"] == state and e.detail["stale"] is False
    assert e.detail["identity_at"] == (None if report is None else NOW.isoformat())
    assert f.count(conn, "execution_runs") == 0


def test_start_run_refused_when_page_id_differs_from_last_report(conn, workspace):
    """执行页面读到的买家号与对账后上报的不是同一个（多标签页读数错位）：服务端直接拒绝，不靠插件自查。"""
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    other = fx.exec_account(conn, ws)
    dev = fx.logged_in_device(conn, workspace, account=acc)
    e = _err(execution.start_run, conn, dev, customer_id=other["amazon_customer_id"], trigger="manual",
             simulated=True)
    assert e.code == "IDENTITY_MISMATCH" and e.http_status == 403
    assert e.message == "页面上的买家号与插件最近上报的不一致，请刷新亚马逊页面后重试"
    assert e.detail == {"login_state": "signed_in", "identity_at": NOW.isoformat(), "stale": False}
    assert f.count(conn, "execution_runs") == 0


def test_start_run_refused_for_stale_identity(conn, workspace):
    acc, dev = _setup(conn, workspace)
    clock.freeze(NOW + timedelta(seconds=601))
    e = _err(fx.start_run, conn, dev)
    assert e.code == "IDENTITY_MISMATCH" and e.http_status == 403
    assert e.message == "插件超过 10 分钟没有上报登录状态，请打开或刷新亚马逊页面后重试"
    assert e.detail == {"login_state": "signed_in", "identity_at": NOW.isoformat(), "stale": True}
    clock.freeze(NOW + timedelta(seconds=600))
    assert fx.start(conn, dev)


def test_start_run_refused_when_buyer_id_is_not_registered(conn, workspace):
    """没登记的买家号、已忽略的、只登记在已归档账号或别的工作区账号上的：都不能开始，也不会替它建账号。"""
    ws = workspace["id"]
    stranger = fx.customer_id(555_001)
    ignored = fx.customer_id(555_002)
    archived = fx.exec_account(conn, ws, archived_at=NOW)
    foreign = fx.exec_account(conn, f.make_workspace(conn)["id"])
    accounts.ignore_buyer_id(conn, ws, ignored, workspace["user_id"])
    dev = fx.registered_device(conn, workspace)
    before = f.count(conn, "buyer_accounts")
    for cid in (stranger, ignored, archived["amazon_customer_id"], foreign["amazon_customer_id"]):
        devices.report_identity(conn, dev, fx.identity(cid))
        e = _err(execution.start_run, conn, dev, customer_id=cid, trigger="manual", simulated=True)
        assert e.code == "ACCOUNT_NOT_REGISTERED" and e.http_status == 403, cid
        assert e.message == "这个买家号还没有登记：请在控制台「账号」页登记后再开始"
    assert f.count(conn, "execution_runs") == 0 and f.count(conn, "buyer_accounts") == before
    # 所有者登记之后，不用重新上报就能开始
    accounts.create(conn, ws, {"name": "环境-DEMO-新", "amazon_customer_id": stranger}, workspace["user_id"])
    devices.report_identity(conn, dev, fx.identity(stranger))
    assert fx.start(conn, dev)


def test_start_run_refused_when_account_paused(conn, workspace):
    acc, dev = _setup(conn, workspace)
    accounts.pause(conn, workspace["id"], acc["id"], "DEMO 人工暂停", actor_kind="user", actor_id=workspace["user_id"])
    e = _err(fx.start_run, conn, dev)
    assert e.code == "ACCOUNT_PAUSED" and e.http_status == 403 and "DEMO 人工暂停" in e.message


def test_start_run_refuses_non_simulated(conn, workspace):
    acc, dev = _setup(conn, workspace)
    e = _err(fx.start_run, conn, dev, simulated=False)
    assert e.code == "REAL_PURCHASE_DISABLED" and e.http_status == 403
    # 即使设置里的模拟开关被（绕过校验）改成 false，本阶段也一律拒绝
    conn.execute("UPDATE procure.workspace_settings SET simulation_only = false WHERE workspace_id = %s",
                 (workspace["id"],))
    assert _err(fx.start_run, conn, dev, simulated=False).code == "REAL_PURCHASE_DISABLED"
    assert f.count(conn, "execution_runs") == 0


def test_two_devices_same_account_only_one_gets_execution_right(conn, workspace):
    """同账号在 Mac 和另一台电脑同时触发：只有一个执行者，另一端看到占用位置。"""
    acc = fx.exec_account(conn, workspace["id"], name="环境-DEMO-A")
    mac = fx.logged_in_device(conn, workspace, account=acc, name="Mac-DEMO")
    pc = fx.logged_in_device(conn, workspace, account=acc, name="PC-DEMO")
    run_id = fx.start(conn, mac)
    e = _err(fx.start_run, conn, pc)
    assert e.code == "ACCOUNT_BUSY" and e.http_status == 409
    assert e.detail == {"holder_device_id": mac["id"], "holder_device_name": "Mac-DEMO", "since": NOW.isoformat()}
    assert "浏览器「Mac-DEMO」" in e.message and "设备" not in e.message
    execution.finish_run(conn, mac, run_id)
    assert fx.start(conn, pc)


def test_device_busy_when_this_browser_has_an_unfinished_run(conn, workspace):
    """一个浏览器同时只有一个未结束的批次：同一个账号再开一次（两个插件页面）、换登另一个买家号再开，都被拒绝。"""
    ws = workspace["id"]
    acc_a = fx.exec_account(conn, ws, name="环境-DEMO-A")
    acc_b = fx.exec_account(conn, ws, name="环境-DEMO-B")
    dev = fx.logged_in_device(conn, workspace, account=acc_a)
    run_id = fx.start(conn, dev)
    lease = fx.run_row(conn, run_id)["lease_expires_at"]
    expected = {"run_id": run_id, "account_name": "环境-DEMO-A", "since": NOW.isoformat(),
                "lease_expires_at": lease.isoformat()}
    e = _err(fx.start_run, conn, dev)
    assert e.code == "DEVICE_BUSY" and e.http_status == 409 and e.message == "这个浏览器上一个批次还没有结束"
    assert e.detail == expected
    fx.refresh_identity(conn, dev, acc_b)                      # 换登另一个买家号：账号 B 没人占，但这个浏览器还没结束
    e = _err(fx.start_run, conn, dev)
    assert e.code == "DEVICE_BUSY" and e.detail == expected
    devices.report_identity(conn, dev, fx.identity(None, signed_in=False))
    assert _err(fx.start_run, conn, dev).code == "DEVICE_BUSY"   # 先看浏览器是否空闲，再看身份
    assert f.count(conn, "execution_runs") == 1
    execution.finish_run(conn, dev, run_id)
    fx.refresh_identity(conn, dev, acc_b)
    assert fx.start_run(conn, dev)["account"]["id"] == acc_b["id"]


def test_unique_index_backstops_busy_check_and_transaction_stays_usable(conn, workspace, monkeypatch):
    """预查漏掉时（并发）靠唯一索引拒绝；冲突在 SAVEPOINT 里捕获，调用方事务仍可继续使用。"""
    acc = fx.exec_account(conn, workspace["id"])
    mac = fx.logged_in_device(conn, workspace, account=acc, name="Mac-DEMO")
    pc = fx.logged_in_device(conn, workspace, account=acc, name="PC-DEMO")
    fx.start(conn, mac)
    real = execution._holder
    calls = {"n": 0}

    def flaky(c, ws, aid):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(c, ws, aid)

    monkeypatch.setattr(execution, "_holder", flaky)
    e = _err(fx.start_run, conn, pc)
    assert e.code == "ACCOUNT_BUSY" and e.detail["holder_device_id"] == mac["id"]
    assert conn.execute("SELECT count(*) AS n FROM procure.execution_runs").fetchone()["n"] == 1


def test_unique_index_backstops_device_busy(conn, workspace, monkeypatch):
    """浏览器级的预查漏掉时，execution_runs_one_active_per_device 兜底并映射成 DEVICE_BUSY。"""
    ws = workspace["id"]
    acc_a = fx.exec_account(conn, ws, name="环境-DEMO-A")
    acc_b = fx.exec_account(conn, ws)
    dev = fx.logged_in_device(conn, workspace, account=acc_a)
    run_id = fx.start(conn, dev)
    fx.refresh_identity(conn, dev, acc_b)
    real = execution._device_run
    calls = {"n": 0}

    def flaky(c, d):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(c, d)

    monkeypatch.setattr(execution, "_device_run", flaky)
    e = _err(fx.start_run, conn, dev)
    assert e.code == "DEVICE_BUSY" and e.http_status == 409
    assert e.detail["run_id"] == run_id and e.detail["account_name"] == "环境-DEMO-A"
    assert f.count(conn, "execution_runs") == 1                    # 事务仍可用，也没有多出批次


def test_start_conflict_maps_each_unique_index(conn, workspace):
    acc = fx.exec_account(conn, workspace["id"], purchase_mode="scheduled", schedule_times=["09:30"])
    dev = fx.logged_in_device(conn, workspace, account=acc)
    row = devices.authenticate(conn, dev["token"])
    mapped = {name: execution._start_conflict(conn, row, acc, TODAY, slot, name).code for name, slot in [
        ("execution_runs_one_active_per_account", None), ("execution_runs_one_active_per_device", None),
        ("execution_runs_slot_once", "09:30"), ("", None), ("some_future_index", None)]}
    # 撞了时间点的索引、此刻却既没有持有者也没有 finished 的批次 = 占着它的批次没跑完就结束了：时间点又空了，
    # 回 ACCOUNT_BUSY（下一分钟再问），不回 SLOT_DONE。SLOT_DONE 只看事实（见下一条用例）。
    assert mapped == {"execution_runs_one_active_per_account": "ACCOUNT_BUSY",
                      "execution_runs_one_active_per_device": "DEVICE_BUSY", "execution_runs_slot_once": "ACCOUNT_BUSY",
                      "": "ACCOUNT_BUSY", "some_future_index": "ACCOUNT_BUSY"}
    indexes = {r["indexname"] for r in conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'procure' AND tablename = 'execution_runs'")}
    assert {"execution_runs_one_active_per_account", "execution_runs_one_active_per_device",
            "execution_runs_slot_once"} <= indexes


def test_start_conflict_on_a_slot_answers_by_whether_its_run_finished_not_by_index_name(conn, workspace):
    """两个浏览器同时为同一个时间点开始：…_per_account 与 …_slot_once 都会撞上，数据库报哪个不一定。答复只看事实：
    占着时间点的批次还在执行 → ACCOUNT_BUSY（带持有者）；它以 finished 结束了 → SLOT_DONE。"""
    acc = fx.exec_account(conn, workspace["id"], purchase_mode="scheduled", schedule_times=["09:30"], name="环境-DEMO-A")
    holder = fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-1")
    late = devices.authenticate(conn, fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-2")["token"])
    names = ("execution_runs_slot_once", "execution_runs_one_active_per_account", "", "some_future_index")
    run_id = fx.start(conn, holder, trigger="scheduled", slot="09:30")
    for name in names:
        e = execution._start_conflict(conn, late, acc, TODAY, "09:30", name)
        assert e.code == "ACCOUNT_BUSY" and e.http_status == 409, name
        assert e.detail["holder_device_id"] == holder["id"] and e.detail["holder_device_name"] == "定时-DEMO-1"
    assert execution._slot_finished(conn, workspace["id"], acc["id"], TODAY, "09:30") is False
    execution.finish_run(conn, holder, run_id)
    assert execution._slot_finished(conn, workspace["id"], acc["id"], TODAY, "09:30") is True
    for name in names + ("execution_runs_one_active_per_device",):
        e = execution._start_conflict(conn, late, acc, TODAY, "09:30", name)
        assert (e.code, e.message) == ("SLOT_DONE", "这个时间点今天已经执行过"), name
    # 别的日期、别的时间点、手动批次都不算「这个时间点已经执行过」
    assert execution._slot_finished(conn, workspace["id"], acc["id"], TODAY + timedelta(days=1), "09:30") is False
    assert execution._slot_finished(conn, workspace["id"], acc["id"], TODAY, "10:00") is False
    assert execution._start_conflict(conn, late, acc, TODAY, None, "execution_runs_one_active_per_account").code == \
        "ACCOUNT_BUSY"


def test_start_conflict_whose_slot_run_died_meanwhile_is_account_busy_not_slot_done(conn, workspace):
    """插入撞上了时间点的唯一索引，查持有者时那个批次已经没跑完就结束了（租约过期）：时间点又空出来，答复是不带
    持有者的 ACCOUNT_BUSY（插件下一分钟再问就能开始），不能因为撞的是 …_slot_once 就回 SLOT_DONE、让插件把它记掉。"""
    acc = fx.exec_account(conn, workspace["id"], purchase_mode="scheduled", schedule_times=["09:30"], name="环境-DEMO-A")
    holder = fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-1")
    late = devices.authenticate(conn, fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-2")["token"])
    run_id = fx.start(conn, holder, trigger="scheduled", slot="09:30")
    conn.execute("UPDATE procure.execution_runs SET ended_at = %s, end_reason = 'lease_expired' WHERE id = %s",
                 (NOW, run_id))
    for name in ("execution_runs_slot_once", "execution_runs_one_active_per_account"):
        e = execution._start_conflict(conn, late, acc, TODAY, "09:30", name)
        assert e.code == "ACCOUNT_BUSY" and e.http_status == 409, name
        assert e.detail == {"holder_device_id": None, "holder_device_name": None, "since": None}
    assert fx.start(conn, late, trigger="scheduled", slot="09:30") != run_id          # 再问就能开始


# ── 定时批次与时间点 ──────────────────────────────────────────────────────────


def _scheduled(conn, workspace, times=("09:30", "10:00", "14:00"), **kw):
    acc = fx.exec_account(conn, workspace["id"], purchase_mode="scheduled", schedule_times=list(times), **kw)
    return acc, fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-1")


def test_scheduled_run_records_slot_and_slot_runs_once_per_day(conn, workspace):
    """定时的权威在服务端：同一 (账号, 北京日期, 时间点) 只执行一次，不论哪个浏览器来问。"""
    acc, dev = _scheduled(conn, workspace)
    other = fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-2")
    out = fx.start_run(conn, dev, trigger="scheduled", slot="9:30")              # 规范成 HH:MM
    run = fx.run_row(conn, out["run_id"])
    assert run["trigger"] == "scheduled" and run["scheduled_slot"] == "09:30" and run["bj_date"] == TODAY
    assert _run_events(conn)[0]["detail"]["scheduled_slot"] == "09:30"
    # 同一时间点还在执行：别的浏览器来问 → ACCOUNT_BUSY（带持有者，下一分钟再问）。还没执行完，不能说「已经执行过」：
    # 批次要是没跑完就结束，时间点会重新空出来，那时得有人接手。
    e = _err(fx.start_run, conn, other, trigger="scheduled", slot="09:30")
    assert e.code == "ACCOUNT_BUSY" and e.http_status == 409
    assert e.detail["holder_device_id"] == dev["id"] and e.detail["holder_device_name"] == "定时-DEMO-1"
    # 另一个时间点：账号被占用 → 同样是 ACCOUNT_BUSY
    assert _err(fx.start_run, conn, other, trigger="scheduled", slot="10:00").code == "ACCOUNT_BUSY"
    execution.finish_run(conn, dev, out["run_id"])
    # 正常结束之后才是 SLOT_DONE：这个时间点今天不再执行；另一个时间点可以
    e = _err(fx.start_run, conn, other, trigger="scheduled", slot="09:30")
    assert e.code == "SLOT_DONE" and e.http_status == 409 and e.message == "这个时间点今天已经执行过"
    assert e.detail == {}
    assert _err(fx.start_run, conn, dev, trigger="scheduled", slot="09:30").code == "SLOT_DONE"
    second = fx.start_run(conn, other, trigger="scheduled", slot="10:00")
    assert fx.run_row(conn, second["run_id"])["scheduled_slot"] == "10:00"
    execution.finish_run(conn, other, second["run_id"])
    # 手动批次不占时间点，也不受时间点限制
    manual = fx.start_run(conn, dev, trigger="manual", slot="09:30")
    assert fx.run_row(conn, manual["run_id"])["scheduled_slot"] is None
    execution.finish_run(conn, dev, manual["run_id"])
    # 第二天（北京日期变了）同一个时间点又可以执行
    clock.freeze(NOW + timedelta(days=1))
    fx.refresh_identity(conn, dev, acc)
    again = fx.start_run(conn, dev, trigger="scheduled", slot="09:30")
    assert fx.run_row(conn, again["run_id"])["bj_date"] == TODAY + timedelta(days=1)


def test_slot_is_free_again_when_the_run_did_not_finish(conn, workspace):
    """租约过期（或被清除模拟结果）结束的定时批次不占时间点：可以再来一次。"""
    acc, dev = _scheduled(conn, workspace)
    first = fx.start(conn, dev, trigger="scheduled", slot="09:30")
    clock.freeze(NOW + timedelta(seconds=200))
    assert execution.sweep(conn)["expired_runs"] == [first]
    assert fx.run_row(conn, first)["end_reason"] == "lease_expired"
    fx.refresh_identity(conn, dev, acc)
    second = fx.start(conn, dev, trigger="scheduled", slot="09:30")
    assert second != first and fx.run_row(conn, second)["scheduled_slot"] == "09:30"


@pytest.mark.parametrize("death", ["lease_expired", "buyer_block"])
def test_slot_held_by_an_unfinished_run_is_account_busy_and_is_picked_up_after_the_run_dies(conn, workspace, death):
    """到点时两个浏览器都登录着同一个买家号：A 开了这个时间点的批次，B 来问。批次还在跑时 B 得到 ACCOUNT_BUSY
    （插件不记这个时间点，下一分钟再问），而不是「已经执行过」——否则 A 的批次没跑完就结束（浏览器崩了租约过期，
    或结算页登录的是别的买家号被护栏拦下）之后，时间点虽然空出来了，却没有人会再来问，这个时间点的任务要等到
    下一个时间点。B 再问时接手，把放下的那一笔做完；正常结束之后两个浏览器才得到 SLOT_DONE。"""
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, name="环境-DEMO-定时", purchase_mode="scheduled", schedule_times=["09:30"])
    a = fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-A")
    b = fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-B")
    order = fx.assigned_order(conn, ws, acc)
    run_a = fx.start(conn, a, trigger="scheduled", slot="09:30")
    first = fx.claim(conn, a, run_a)

    e = _err(fx.start_run, conn, b, trigger="scheduled", slot="09:30")
    assert e.code == "ACCOUNT_BUSY" and e.http_status == 409
    assert e.detail == {"holder_device_id": a["id"], "holder_device_name": "定时-DEMO-A", "since": NOW.isoformat()}
    assert fx.run_row(conn, run_a)["ended_at"] is None and f.count(conn, "execution_runs") == 1

    if death == "lease_expired":
        clock.freeze(NOW + timedelta(seconds=400))
        assert execution.sweep(conn)["expired_runs"] == [run_a]
        fx.refresh_identity(conn, b, acc)
    else:
        v = execution.submit_guard(conn, a, first["attempt_id"],
                                   fx.facts_for(conn, order, customer_id=fx.customer_id(999_999)))
        assert v["code"] == "IDENTITY_MISMATCH"
    assert fx.run_row(conn, run_a)["end_reason"] == {"lease_expired": "lease_expired",
                                                     "buyer_block": "identity_mismatch"}[death]
    assert fx.order_row(conn, order["id"])["status"] == "assigned"

    run_b = fx.start(conn, b, trigger="scheduled", slot="09:30")                   # 空出来的时间点被接手
    second = fx.claim(conn, b, run_b)
    assert second["order_id"] == order["id"] and second["attempt_no"] == 2
    assert fx.run_row(conn, run_b)["scheduled_slot"] == "09:30"
    # B 的批次还在跑：A 再来问也是 ACCOUNT_BUSY
    fx.refresh_identity(conn, a, acc)
    assert _err(fx.start_run, conn, a, trigger="scheduled", slot="09:30").detail["holder_device_id"] == b["id"]
    assert fx.complete_success(conn, b, second)["order_status"] == "purchased"
    execution.finish_run(conn, b, run_b)
    for dev in (a, b):
        e = _err(fx.start_run, conn, dev, trigger="scheduled", slot="09:30")
        assert (e.code, e.message) == ("SLOT_DONE", "这个时间点今天已经执行过")
    assert f.count(conn, "execution_runs", "scheduled_slot = '09:30'") == 2


def test_manual_run_blocks_scheduled_start_with_account_busy_not_slot_done(conn, workspace):
    acc, dev = _scheduled(conn, workspace)
    other = fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-2")
    run_id = fx.start(conn, dev)                                                   # 手动批次占着账号
    e = _err(fx.start_run, conn, other, trigger="scheduled", slot="09:30")
    assert e.code == "ACCOUNT_BUSY" and e.detail["holder_device_id"] == dev["id"]
    execution.finish_run(conn, dev, run_id)
    assert fx.start(conn, other, trigger="scheduled", slot="09:30")                # 手动批次没有用掉这个时间点


@pytest.mark.parametrize("slot,field_word", [(None, "需要带上时间点"), ("", "需要带上时间点"), ("25:00", "格式不对"),
                                              ("9:5", "格式不对"), ("0930", "格式不对"), ("09:60", "格式不对"),
                                              (930, "格式不对"), ("09:30:00", "格式不对")])
def test_scheduled_run_requires_well_formed_slot(conn, workspace, slot, field_word):
    acc, dev = _scheduled(conn, workspace)
    e = _err(fx.start_run, conn, dev, trigger="scheduled", slot=slot)
    assert e.code == "VALIDATION" and e.http_status == 422 and e.detail["field"] == "slot" and field_word in e.message
    assert f.count(conn, "execution_runs") == 0


def test_scheduled_run_checks_mode_schedule_and_time(conn, workspace):
    """账号不是定时模式、时间点不在执行时间里、北京时间今天还没到点：422 并说明原因。"""
    ws = workspace["id"]
    manual_acc = fx.exec_account(conn, ws, name="环境-DEMO-手动")
    manual_dev = fx.logged_in_device(conn, workspace, account=manual_acc)
    e = _err(fx.start_run, conn, manual_dev, trigger="scheduled", slot="09:30")
    assert e.code == "VALIDATION" and "不是定时执行模式" in e.message and e.detail["field"] == "slot"
    acc, dev = _scheduled(conn, workspace, name="环境-DEMO-定时")
    e = _err(fx.start_run, conn, dev, trigger="scheduled", slot="11:11")
    assert e.code == "VALIDATION" and "不在账号 环境-DEMO-定时 的执行时间里" in e.message
    assert e.detail["schedule_times"] == ["09:30", "10:00", "14:00"]
    e = _err(fx.start_run, conn, dev, trigger="scheduled", slot="14:00")           # 现在是北京 10:00
    assert e.code == "VALIDATION" and "还没有到 14:00" in e.message
    assert f.count(conn, "execution_runs") == 0
    assert fx.start(conn, dev, trigger="scheduled", slot="10:00")                  # 正好到点
    # 定时模式的账号照样可以手动开始
    assert fx.start(conn, manual_dev, trigger="manual")


def test_run_bj_date_is_the_beijing_date_at_start(conn, workspace):
    clock.freeze(datetime(2026, 10, 31, 15, 58, tzinfo=timezone.utc))              # 北京 10-31 23:58
    acc, dev = _setup(conn, workspace)
    run_id = fx.start(conn, dev)
    assert fx.run_row(conn, run_id)["bj_date"] == TODAY
    execution.finish_run(conn, dev, run_id)
    clock.freeze(datetime(2026, 10, 31, 16, 2, tzinfo=timezone.utc))               # 北京 11-01 00:02
    fx.refresh_identity(conn, dev, acc)
    assert fx.run_row(conn, fx.start(conn, dev))["bj_date"] == TODAY + timedelta(days=1)


# ── 批次心跳 ──────────────────────────────────────────────────────────────────


def test_run_heartbeat_extends_lease_and_reports_stop(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    cid = acc["amazon_customer_id"]
    run_id = fx.start(conn, dev)
    clock.freeze(NOW + timedelta(seconds=100))
    out = execution.run_heartbeat(conn, dev, run_id)
    assert out == {"lease_expires_at": (NOW + timedelta(seconds=280)).isoformat(), "stop": False, "stop_reason": None}
    accounts.pause(conn, ws, acc["id"], "DEMO", actor_kind="user", actor_id=workspace["user_id"])
    out = execution.run_heartbeat(conn, dev, run_id)
    assert out["stop"] is True and out["stop_reason"] == "account_paused"
    accounts.resume(conn, ws, acc["id"], actor_kind="user", actor_id=workspace["user_id"])
    for ident in (fx.identity("A9OTHER0001"), fx.identity(None, signed_in=False), fx.identity(None),
                  fx.identity(cid, conflict=True)):
        devices.report_identity(conn, dev, ident)
        out = execution.run_heartbeat(conn, dev, run_id)
        assert out["stop"] is True and out["stop_reason"] == "identity_mismatch", ident
    devices.report_identity(conn, dev, fx.identity(cid))
    assert execution.run_heartbeat(conn, dev, run_id)["stop"] is False
    assert execution.STOP_REASONS == ("account_paused", "account_archived", "identity_mismatch")


def test_run_heartbeat_ignores_identity_freshness_and_stops_on_archive(conn, workspace):
    acc, dev = _setup(conn, workspace)
    run_id = fx.start(conn, dev)
    for step in range(1, 6):                                     # 一直续租、一直不上报身份：超过 10 分钟也不因此停
        clock.freeze(NOW + timedelta(seconds=150 * step))
        out = execution.run_heartbeat(conn, dev, run_id)
    assert devices.identity_stale(devices.authenticate(conn, dev["token"])) is True
    assert out["stop"] is False and out["stop_reason"] is None
    conn.execute("UPDATE procure.buyer_accounts SET archived_at = %s WHERE id = %s", (clock.now(), acc["id"]))
    out = execution.run_heartbeat(conn, dev, run_id)
    assert out["stop"] is True and out["stop_reason"] == "account_archived"


def test_lease_lost_after_expiry_or_finish(conn, workspace):
    acc, dev = _setup(conn, workspace)
    run_id = fx.start(conn, dev)
    clock.freeze(NOW + timedelta(seconds=180))
    e = _err(execution.run_heartbeat, conn, dev, run_id)
    assert e.code == "LEASE_LOST" and e.http_status == 409
    assert _err(execution.next_attempt, conn, dev, run_id).code == "LEASE_LOST"
    clock.freeze(NOW + timedelta(seconds=200))
    fx.refresh_identity(conn, dev, acc)
    execution.sweep(conn)
    run2 = fx.start(conn, dev)
    out = execution.finish_run(conn, dev, run2, "completed")
    assert out["end_reason"] == "finished" and out["ended_at"]
    assert _err(execution.next_attempt, conn, dev, run2).code == "LEASE_LOST"
    assert _err(execution.finish_run, conn, dev, run2).code == "LEASE_LOST"


# ── 领取 ──────────────────────────────────────────────────────────────────────


def test_next_attempt_payload_and_order_executing(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace, min_amount="20", max_amount="50", expected_card_last4="7860",
                      max_delivery_days=7)
    order = fx.assigned_order(conn, ws, acc, upstream_order_no="PO-DEMO-PAYLOAD", unit_price="24.00", quantity=2,
                              price_cap="60.00")
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    assert a["upstream_order_no"] == "PO-DEMO-PAYLOAD" and a["asin"] == "B0DEMO0001" and a["quantity"] == 2
    assert a["unit_price"] == "24.00" and a["subtotal"] == "48.00" and a["price_cap"] == "60.00"
    assert a["max_delivery_days"] == 7 and a["amount_range"] == {"min": "20.00", "max": "50.00"}
    assert a["expected_card_last4"] == "7860" and a["simulated"] is True and a["kind"] == "auto"
    assert a["recipient_name"] == "Demo Buyer" and a["postal_code"] == "32960" and a["bj_date"] == "2026-10-31"
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "claimed" and att["bj_date"] == TODAY and att["run_id"] == run_id
    assert att["task_snapshot"]["attempt_id"] == a["attempt_id"] and att["attempt_no"] == 1
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "executing" and o["current_attempt_id"] == a["attempt_id"]
    assert o["assigned_account_id"] == acc["id"]
    assert quota.used(conn, acc["id"], TODAY, True) == 1


def test_next_attempt_uses_override_values(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    orders.set_override(conn, ws, order["id"], asin="B0DEMO0099", price_cap="38.50", note="DEMO",
                        actor_user_id=workspace["user_id"])
    a = fx.claim(conn, dev, fx.start(conn, dev))
    assert a["asin"] == "B0DEMO0099" and a["price_cap"] == "38.50"


def test_next_attempt_refuses_while_run_has_attempt_in_flight(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    fx.claim(conn, dev, run_id)
    e = _err(execution.next_attempt, conn, dev, run_id)
    assert e.code == "ATTEMPT_IN_FLIGHT" and e.http_status == 409


def test_suggested_date_gate_and_claim_order(conn, workspace):
    """建议采购日期为明天不执行；今天或过去的按日期先后领取。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    tomorrow = fx.assigned_order(conn, ws, acc, suggested_purchase_date=TODAY + timedelta(days=1))
    today = fx.assigned_order(conn, ws, acc, suggested_purchase_date=TODAY)
    old = fx.assigned_order(conn, ws, acc, suggested_purchase_date=TODAY - timedelta(days=3))
    yesterday = fx.assigned_order(conn, ws, acc, suggested_purchase_date=TODAY - timedelta(days=1))
    run_id = fx.start(conn, dev)
    got = []
    for _ in range(3):
        a = fx.claim(conn, dev, run_id)
        got.append(a["order_id"])
        fx.complete_success(conn, dev, a)
    assert got == [old["id"], yesterday["id"], today["id"]]
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "1 笔建议采购日期在未来" in out["reason"]
    assert fx.order_row(conn, tomorrow["id"])["status"] == "assigned"
    # 第二天（北京时间）它就到期了
    clock.freeze(datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc))
    fx.refresh_identity(conn, dev, acc)
    execution.sweep(conn)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    assert a["order_id"] == tomorrow["id"]


def test_same_day_ordered_by_order_time_then_id(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    late = fx.assigned_order(conn, ws, acc, order_time=datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc))
    early = fx.assigned_order(conn, ws, acc, order_time=datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc))
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    assert a["order_id"] == early["id"]
    fx.complete_success(conn, dev, a)
    assert fx.claim(conn, dev, run_id)["order_id"] == late["id"]


def test_only_orders_assigned_to_this_account_and_eligible(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    other = fx.exec_account(conn, ws)
    fx.assigned_order(conn, ws, other)
    f.ingest_ready_order(conn, ws)                                  # 待分配
    cancelled = fx.assigned_order(conn, ws, acc)
    f.set_status(conn, cancelled["id"], "assigned", upstream_cancelled=True)
    out = execution.next_attempt(conn, dev, fx.start(conn, dev))
    assert out["attempt"] is None and out["reason"].startswith("今天没有可执行的任务")


def test_amount_out_of_range_at_claim_is_unassigned_and_skipped(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace, min_amount="20", max_amount="50")
    drifted = fx.assigned_order(conn, ws, acc, unit_price="30.00",
                                suggested_purchase_date=TODAY - timedelta(days=1))
    ok = fx.assigned_order(conn, ws, acc, unit_price="30.00")
    conn.execute("UPDATE procure.buyer_accounts SET max_amount = 29 WHERE id = %s", (acc["id"],))
    conn.execute("UPDATE procure.purchase_orders SET unit_price = 25 WHERE id = %s", (ok["id"],))
    a = fx.claim(conn, dev, fx.start(conn, dev))
    assert a["order_id"] == ok["id"]
    d = fx.order_row(conn, drifted["id"])
    assert d["status"] == "queued" and d["assigned_account_id"] is None and "已不在账号" in d["status_reason"]


def test_daily_limit_counts_inflight_unknown_and_succeeded(conn, workspace):
    """达到每日额度或额度被在途 / 待核实占满时不能再领取；未知结果不释放额度。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace, daily_limit=3)
    for status in ("unknown", "verifying", "succeeded"):
        o = f.ingest_ready_order(conn, ws)
        f.make_attempt(conn, ws, o["id"], acc["id"], status=status, submitted=True)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "额度已用完" in out["reason"] and "3 / 上限 3" in out["reason"]
    # 失败与中止的尝试不占额度
    conn.execute("UPDATE procure.purchase_attempts SET status = 'failed' WHERE status = 'succeeded'")
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "进行中的采购尝试" in out["reason"]     # 账号还有在途尝试（verifying）
    conn.execute("UPDATE procure.purchase_attempts SET status = 'aborted' WHERE status = 'verifying'")
    assert fx.claim(conn, dev, run_id)


def test_daily_limit_reached_within_run(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace, daily_limit=1)
    fx.assigned_order(conn, ws, acc)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    fx.complete_success(conn, dev, fx.claim(conn, dev, run_id))
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "额度已用完" in out["reason"]


def test_attempt_at_2358_beijing_counts_for_that_day_and_order_never_reclaimed(conn, workspace):
    """午夜切换时仍有提交结果不明的任务：保留关联与防重复保护，不能再次购买；额度算在开始那天。"""
    ws = workspace["id"]
    clock.freeze(datetime(2026, 10, 31, 15, 58, tzinfo=timezone.utc))          # 北京 10-31 23:58
    acc, dev = _setup(conn, workspace, daily_limit=5)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_submitted(conn, dev, a)
    clock.freeze(datetime(2026, 10, 31, 15, 59, 30, tzinfo=timezone.utc))      # 插件照常续租
    execution.run_heartbeat(conn, dev, run_id)
    clock.freeze(datetime(2026, 10, 31, 16, 2, tzinfo=timezone.utc))           # 北京 11-01 00:02
    execution.run_heartbeat(conn, dev, run_id)
    assert fx.attempt_row(conn, a["attempt_id"])["bj_date"] == TODAY
    assert quota.used(conn, acc["id"], TODAY, True) == 1
    assert quota.used(conn, acc["id"], TODAY + timedelta(days=1), True) == 0
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload(None, "DEMO 断线", outcome="unknown"))
    assert fx.order_row(conn, order["id"])["status"] == "result_unknown"
    assert quota.used(conn, acc["id"], TODAY, True) == 1                        # 未知结果不释放额度
    accounts.resume(conn, ws, acc["id"], actor_kind="user", actor_id=workspace["user_id"])
    execution.finish_run(conn, dev, run_id)
    fx.refresh_identity(conn, dev, acc)
    run2 = fx.start(conn, dev)
    out = execution.next_attempt(conn, dev, run2)
    assert out["attempt"] is None
    assert f.count(conn, "purchase_attempts", "purchase_order_id = %s", (order["id"],)) == 1


def test_next_attempt_stops_when_account_paused_or_identity_mismatch(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    accounts.pause(conn, ws, acc["id"], "DEMO 暂停", actor_kind="user", actor_id=workspace["user_id"])
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "已暂停执行" in out["reason"]
    accounts.resume(conn, ws, acc["id"], actor_kind="user", actor_id=workspace["user_id"])
    devices.report_identity(conn, dev, fx.identity("A9OTHER0001"))
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "浏览器登录的买家号变了" in out["reason"] and "停止领取" in out["reason"]
    devices.report_identity(conn, dev, fx.identity(None, signed_in=False))
    assert "浏览器没有登录亚马逊" in execution.next_attempt(conn, dev, run_id)["reason"]
    fx.refresh_identity(conn, dev, acc)
    assert execution.next_attempt(conn, dev, run_id)["attempt"] is not None


def test_retry_kind_after_page_load_failed(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("PAGE_LOAD_FAILED"))
    b = fx.claim(conn, dev, run_id)
    assert b["order_id"] == a["order_id"] and b["kind"] == "retry" and b["attempt_no"] == 2


def test_repurchase_kind_after_amazon_cancel_and_approval(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    fx.complete_success(conn, dev, fx.claim(conn, dev, run_id))
    orders.mark_amazon_cancelled(conn, ws, order["id"], "DEMO", actor_user_id=workspace["user_id"])
    orders.decide_repurchase(conn, ws, order["id"], "approve", actor_user_id=workspace["user_id"])
    row = fx.order_row(conn, order["id"])
    orders.transition(conn, row, "assigned", actor_kind="system",
                      updates={"assigned_account_id": acc["id"], "assignment_kind": "manual"})
    b = fx.claim(conn, dev, run_id)
    assert b["order_id"] == order["id"] and b["kind"] == "repurchase"


# ── 进度与上游取消 ────────────────────────────────────────────────────────────


def _cancel_upstream(conn, ws, order, src):
    rec = f.make_record(upstream_order_no=order["upstream_order_no"], upstream_cancelled=True,
                        after_sale_value="DEMO 退款")
    return orders.ingest(conn, ws, rec, src)


def test_progress_moves_to_checking_and_continues(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    out = execution.progress(conn, dev, a["attempt_id"], stage="started", detail={"outcome": "pass"})
    assert out == {"continue": True, "cancel_reason": None, "cancel_code": None}
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "checking" and att["stage"] == "started"
    assert _err(execution.progress, conn, dev, a["attempt_id"], stage=" ").code == "VALIDATION"


def test_upstream_cancel_before_submit_stops_and_cancels(conn, workspace):
    """执行中收到取消、尚未提交：进度得到 continue=false → 插件上报中止 → 尝试 aborted、任务 cancelled。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    src = f.file_source(conn, ws, 2)
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-CANCEL")
    orders.transition(conn, order, "assigned", actor_kind="system",
                      updates={"assigned_account_id": acc["id"], "assignment_kind": "auto"})
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_guard_passed(conn, dev, a)
    _cancel_upstream(conn, ws, order, src)
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "executing" and o["upstream_cancelled"] is True
    out = execution.progress(conn, dev, a["attempt_id"], stage="before_submit")
    assert out["continue"] is False and "上游已取消" in out["cancel_reason"]
    e = _err(execution.mark_submitting, conn, dev, a["attempt_id"])
    assert e.http_status == 409 and "上游已取消" in e.message
    res = execution.report_result(conn, dev, a["attempt_id"],
                                  fx.failure_payload("ABORTED_UPSTREAM_CANCELLED", out["cancel_reason"]))
    assert res == {"attempt_status": "aborted", "order_status": "cancelled"}
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "cancelled" and o["assigned_account_id"] is None and o["current_attempt_id"] is None
    assert quota.used(conn, acc["id"], TODAY, True) == 0
    assert fx.account_row(conn, acc["id"])["execution_paused"] is False


def test_upstream_cancel_after_submit_goes_result_unknown(conn, workspace):
    """执行中收到取消、已经提交：先核实是否下单成功，不能直接判定为未采购。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    src = f.file_source(conn, ws, 2)
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-LATECANCEL")
    orders.transition(conn, order, "assigned", actor_kind="system",
                      updates={"assigned_account_id": acc["id"], "assignment_kind": "auto"})
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_submitted(conn, dev, a)
    _cancel_upstream(conn, ws, order, src)
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "result_unknown" and "执行中收到取消" in o["status_reason"]
    assert execution.progress(conn, dev, a["attempt_id"], stage="waiting_confirmation")["continue"] is True
    res = execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("UNEXPECTED_PAGE", "DEMO"))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    o = fx.order_row(conn, order["id"])
    assert "执行中收到取消" in o["status_reason"] and o["current_attempt_id"] == a["attempt_id"]
    assert fx.account_row(conn, acc["id"])["execution_paused"] is True


def test_upstream_cancel_after_submit_then_success_is_purchased_with_attention(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    src = f.file_source(conn, ws, 2)
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-LATECANCEL2")
    orders.transition(conn, order, "assigned", actor_kind="system",
                      updates={"assigned_account_id": acc["id"], "assignment_kind": "auto"})
    a = fx.claim(conn, dev, fx.start(conn, dev))
    fx.to_submitted(conn, dev, a)
    _cancel_upstream(conn, ws, order, src)
    res = execution.report_result(conn, dev, a["attempt_id"], fx.success_payload(a))
    assert res == {"attempt_status": "succeeded", "order_status": "purchased"}
    o = fx.order_row(conn, order["id"])
    assert o["needs_attention"] is True and "上游取消" in o["attention_note"]


def test_progress_on_finished_attempt_says_stop(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("OUT_OF_STOCK"))
    out = execution.progress(conn, dev, a["attempt_id"], stage="late")
    assert out["continue"] is False and "已结束" in out["cancel_reason"]


# ── 护栏与提交登记 ────────────────────────────────────────────────────────────


def test_submitting_without_guard_pass_is_refused(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    e = _err(execution.mark_submitting, conn, dev, a["attempt_id"])
    assert e.code == "CONFLICT" and e.http_status == 409 and "护栏" in e.message
    assert fx.attempt_row(conn, a["attempt_id"])["submitted_at"] is None


def test_guard_block_fails_attempt_sets_exception_and_refuses_submit(conn, workspace):
    """48/52/50：账号区间匹配，结算总额超限价 → 拦截，任务异常，提交登记 409。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace, min_amount="20", max_amount="50")
    order = fx.assigned_order(conn, ws, acc, unit_price="48.00", price_cap="50.00")
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    facts = fx.facts_for(conn, fx.order_row(conn, order["id"]), total="52.00", tax="4.00")
    v = execution.submit_guard(conn, dev, a["attempt_id"], facts)
    assert v["verdict"] == "block" and v["code"] == "PRICE_OVER_CAP" and v["checks"]["subtotal"]["ok"] is True
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "failed" and att["error_code"] == "PRICE_OVER_CAP"
    assert att["guard_verdict"]["code"] == "PRICE_OVER_CAP" and att["guard_input"]["total"] == "52.00"
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "exception" and o["status_reason"].startswith("结算总额超过限价")
    assert _err(execution.mark_submitting, conn, dev, a["attempt_id"]).http_status == 409
    # 插件随后照例上报同样的失败：幂等
    res = execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("PRICE_OVER_CAP", "超限"))
    assert res["duplicate"] is True and res["attempt_status"] == "failed"
    assert quota.used(conn, acc["id"], TODAY, True) == 0


def test_guard_uses_latest_account_config(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    accounts.update(conn, ws, acc["id"], {"max_delivery_days": 2}, workspace["user_id"])
    v = execution.submit_guard(conn, dev, a["attempt_id"], fx.facts_for(conn, fx.order_row(conn, order["id"])))
    assert v["code"] == "DELIVERY_TOO_LONG" and v["checks"]["delivery"]["expected"] == 2


def test_submitting_records_submitted_at_and_cannot_repeat(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    out = fx.to_submitted(conn, dev, a)
    assert out == {"ok": True, "submitted_at": NOW.isoformat()}
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "submitting" and att["submitted_at"] == NOW
    assert "已登记提交" in _err(execution.mark_submitting, conn, dev, a["attempt_id"]).message
    assert _err(execution.submit_guard, conn, dev, a["attempt_id"], {}).http_status == 409


def test_submitting_refused_when_account_paused_mid_attempt(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    fx.to_guard_passed(conn, dev, a)
    accounts.pause(conn, ws, acc["id"], "DEMO", actor_kind="user", actor_id=workspace["user_id"])
    assert execution.progress(conn, dev, a["attempt_id"], stage="before_submit")["continue"] is False
    assert _err(execution.mark_submitting, conn, dev, a["attempt_id"]).code == "ACCOUNT_PAUSED"


def test_verifying_keeps_task_and_quota(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    assert _err(execution.mark_verifying, conn, dev, a["attempt_id"]).http_status == 409
    fx.to_submitted(conn, dev, a)
    out = execution.mark_verifying(conn, dev, a["attempt_id"], "DEMO 银行验证")
    assert out == {"ok": True, "attempt_status": "verifying"}
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "executing" and "银行人工验证" in o["status_reason"]
    assert quota.used(conn, acc["id"], TODAY, True) == 1
    res = execution.report_result(conn, dev, a["attempt_id"], fx.success_payload(a))
    assert res["order_status"] == "purchased"


# ── 归属与跨工作区 ────────────────────────────────────────────────────────────


def test_cross_workspace_device_cannot_touch_attempt_or_run(conn, workspace):
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    other_ws = f.make_workspace(conn)
    other_acc = fx.exec_account(conn, other_ws["id"])
    intruder = fx.logged_in_device(conn, other_ws, account=other_acc)
    aid = a["attempt_id"]
    calls = [
        (execution.run_heartbeat, (run_id,), {}),
        (execution.next_attempt, (run_id,), {}),
        (execution.finish_run, (run_id,), {}),
        (execution.progress, (aid,), {"stage": "x"}),
        (execution.submit_guard, (aid, {}), {}),
        (execution.mark_submitting, (aid,), {}),
        (execution.mark_verifying, (aid,), {}),
        (execution.report_result, (aid, fx.success_payload(a)), {}),
    ]
    for fn, args, kw in calls:
        e = _err(fn, conn, intruder, *args, **kw)
        assert e.code == "NOT_FOUND" and e.http_status == 404, fn.__name__
    # 同工作区的另一台设备也碰不到别人的批次与尝试
    sibling = fx.logged_in_device(conn, workspace, account=fx.exec_account(conn, ws))
    assert _err(execution.progress, conn, sibling, aid, stage="x").code == "NOT_FOUND"
    assert _err(execution.run_heartbeat, conn, sibling, run_id).code == "NOT_FOUND"
    assert fx.attempt_row(conn, aid)["status"] == "claimed"
    assert _err(execution.progress, conn, dev, "abc", stage="x").code == "NOT_FOUND"


def test_removing_a_browser_never_cuts_off_its_run(conn, workspace):
    """没有「撤销」了：正在执行的浏览器不能移除；执行完移除后它照样能认证、能再开批次（再次联系即回到列表）。"""
    ws = workspace["id"]
    acc, dev = _setup(conn, workspace)
    fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_submitted(conn, dev, a)
    assert _err(devices.remove_device, conn, ws, dev["id"], workspace["user_id"]).code == "CONFLICT"
    assert execution.run_heartbeat(conn, dev, run_id)["stop"] is False
    assert execution.report_result(conn, dev, a["attempt_id"], fx.success_payload(a))["order_status"] == "purchased"
    execution.finish_run(conn, dev, run_id)
    devices.remove_device(conn, ws, dev["id"], workspace["user_id"])
    assert fx.start(conn, dev)                                 # 令牌没动：照样能开始


def test_calls_from_a_browser_whose_row_is_gone_are_unauthorized(conn, workspace):
    """浏览器行被清扫删掉（长期没联系、没有任何批次）之后拿旧令牌来的请求：401，插件据此重新登记。"""
    acc, dev = _setup(conn, workspace)
    conn.execute("DELETE FROM procure.devices WHERE id = %s", (dev["id"],))
    for call in (lambda: fx.start_run(conn, dev, customer_id=acc["amazon_customer_id"]),
                 lambda: execution.run_heartbeat(conn, dev, 1), lambda: execution.next_attempt(conn, dev, 1),
                 lambda: execution.mark_submitting(conn, dev, 1), lambda: execution.finish_run(conn, dev, 1)):
        e = _err(call)
        assert e.code == "UNAUTHORIZED" and e.http_status == 401
