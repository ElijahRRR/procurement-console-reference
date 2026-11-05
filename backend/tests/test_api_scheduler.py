"""后台调度（scheduler.py，契约 §9）：run_jobs_once 依次跑清扫、每日分配、飞书读取、飞书写回且从不抛异常；
到期连接按 poll_minutes 读取；每日分配北京时间 00:01 之后每个北京日期只跑一次；一个任务失败不影响其他任务；
调度线程能启动并按停止事件退出。全部数据虚构。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

import scheduler
from services import clock, execution
from services import feishu_connections as fc
from tests import factories as f
from tests import factories_exec as fx
from tests import fake_feishu as ff
from tests.api_helpers import tx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


@pytest.fixture()
def fake(monkeypatch):
    client = ff.FakeFeishuClient.with_order_table()
    monkeypatch.setattr(fc, "make_client", lambda: client)
    return client


def _workspace():
    with tx() as conn:
        return f.make_workspace(conn)


def test_run_jobs_once_sweeps_reads_feishu_and_flushes(fake):
    ws = _workspace()
    with tx() as conn:
        acc = fx.exec_account(conn, ws["id"])
        dev = fx.paired_device(conn, ws, account=acc)
        run_id = fx.start(conn, dev)
        fake.add_record(ff.DEMO_APP, ff.DEMO_TABLE, "recDEMO0001", ff.order_fields("PO-DEMO-SCH01"))
        cid = ff.create_demo_connection(conn, ws["id"], fake)["id"]

    later = NOW + timedelta(seconds=181)            # 批次租约（180 秒）已过期
    clock.freeze(later)
    out = scheduler.run_jobs_once(later)
    assert set(out) == {"sweep", "daily_allocation", "feishu_intake", "feishu_writeback"}
    assert all("error" not in v for v in out.values()), out
    assert out["sweep"]["expired_runs"] == [run_id]
    assert out["feishu_intake"]["due"] == 1 and out["feishu_intake"]["connections"][str(cid)]["new"] == 1
    assert out["feishu_writeback"]["due"] == 0
    assert out["daily_allocation"]["bj_date"] == "2026-10-31"
    with tx() as conn:
        assert fx.run_row(conn, run_id)["end_reason"] == "lease_expired"
        assert f.count(conn, "purchase_orders", "upstream_order_no = 'PO-DEMO-SCH01'") == 1
        assert f.count(conn, "allocation_runs", "trigger = 'daily'") >= 1

    again = scheduler.run_jobs_once(later + timedelta(minutes=1))
    assert again["feishu_intake"]["due"] == 0                     # poll_minutes 未到，不再读取
    assert again["sweep"]["expired_runs"] == []


def test_intake_without_feishu_credentials_is_skipped(monkeypatch):
    import dataclasses

    from registry import settings

    monkeypatch.setattr(settings, "_loaded", dataclasses.replace(settings.get(), feishu_app_id=None,
                                                                 feishu_app_secret=None))
    ws = _workspace()
    client = ff.FakeFeishuClient.with_order_table()
    with tx() as conn:
        ff.create_demo_connection(conn, ws["id"], client)
    out = scheduler.run_jobs_once(NOW)
    assert out["feishu_intake"]["due"] == 1 and "skipped" in out["feishu_intake"]
    assert "error" not in out["feishu_writeback"]


def test_one_failing_job_does_not_stop_the_others(monkeypatch, fake):
    def boom(conn, now=None):
        raise RuntimeError("DEMO sweep failure")

    monkeypatch.setattr(execution, "sweep", boom)
    state = scheduler.JobState()
    out = scheduler.run_jobs_once(NOW, state)
    assert out["sweep"] == {"error": "DEMO sweep failure"}
    assert "error" not in out["feishu_intake"] and "error" not in out["feishu_writeback"]
    assert "error" not in out["daily_allocation"]
    assert state.failures["sweep"] == 1
    scheduler.run_jobs_once(NOW, state)
    assert state.failures["sweep"] == 2
    monkeypatch.undo()
    scheduler.run_jobs_once(NOW, state)
    assert state.failures["sweep"] == 0                            # 恢复后清零


def test_daily_allocation_runs_once_per_beijing_date_after_0001():
    _workspace()
    before = datetime(2026, 10, 31, 16, 0, 30, tzinfo=timezone.utc)   # 北京 11-01 00:00:30
    after = datetime(2026, 10, 31, 16, 1, 0, tzinfo=timezone.utc)     # 北京 11-01 00:01
    assert scheduler.daily_due(NOW, None) is True                     # 10:00 启动、当天还没跑过 → 补跑
    assert scheduler.daily_due(before, None) is False                 # 11-01 还没到 00:01
    assert scheduler.daily_due(before, clock.bj_date(NOW)) is False
    assert scheduler.daily_due(after, clock.bj_date(NOW)) is True
    state = scheduler.JobState(last_daily=clock.bj_date(NOW))
    clock.freeze(after)
    first = scheduler.run_jobs_once(after, state)["daily_allocation"]
    assert first["bj_date"] == "2026-11-01" and state.last_daily.isoformat() == "2026-11-01"
    second = scheduler.run_jobs_once(after + timedelta(hours=3), state)["daily_allocation"]
    assert "skipped" in second
    with tx() as conn:
        assert f.count(conn, "allocation_runs", "trigger = 'daily'") == 1


def test_scheduler_threads_start_and_stop(monkeypatch):
    calls: list[str] = []
    seen = threading.Event()

    def fake_sweep(now):
        calls.append("sweep")
        seen.set()
        return {"expired_runs": [], "revoked_runs": [], "orphan_attempts": [], "aborted": 0, "unknown": 0}

    monkeypatch.setattr(scheduler, "run_sweep", fake_sweep)
    monkeypatch.setattr(scheduler, "run_feishu_intake", lambda now: {"due": 0, "connections": {}})
    monkeypatch.setattr(scheduler, "run_feishu_writeback", lambda now: {"due": 0})
    monkeypatch.setattr(scheduler, "run_daily_allocation", lambda now: {"bj_date": "x", "workspaces": {}})
    s = scheduler.Scheduler(sweep_interval_s=60, writeback_interval_s=60, intake_check_s=60, tick_s=0.05)
    s.start()
    try:
        assert seen.wait(5) and s.running
        names = {t.name for t in threading.enumerate()}
        assert {"pc-scheduler-core", "pc-scheduler-feishu"} <= names
    finally:
        s.stop(timeout=5)
    assert not s.running and calls.count("sweep") == 1            # 间隔 60 秒内只跑一次
    assert s.state.last_daily == clock.bj_date(NOW)


def test_serve_rejects_unknown_params():
    with pytest.raises(SystemExit):
        scheduler.serve(nonsense="1")


def test_daily_allocation_is_restart_proof_and_catches_up_once():
    """K11：00:01 之后重启（进程内状态丢了）不会重做当天的每日分配；当天没做过的（服务停过）启动后补做一次。"""
    from services import allocation

    ws = _workspace()
    with tx() as conn:
        assert allocation.daily_done(conn, ws["id"], clock.bj_date(NOW)) is False
    first = scheduler.run_jobs_once(NOW, scheduler.JobState())["daily_allocation"]      # 10:00 启动：补做
    assert "id" in first["workspaces"][str(ws["id"])]
    restarted = scheduler.JobState()                                                      # 进程重启
    second = scheduler.run_jobs_once(NOW + timedelta(hours=1), restarted)["daily_allocation"]
    assert second["workspaces"][str(ws["id"])] == {"skipped": "今天已做过每日分配"}
    assert restarted.last_daily == clock.bj_date(NOW)
    with tx() as conn:
        assert allocation.daily_done(conn, ws["id"], clock.bj_date(NOW)) is True
        assert f.count(conn, "allocation_runs", "trigger = 'daily'") == 1
        allocation.allocate(conn, ws["id"], trigger="manual")                             # 人工分配不算每日分配
        assert allocation.daily_done(conn, ws["id"], clock.bj_date(NOW) + timedelta(days=1)) is False


def test_cli_allocate_defaults_to_manual_and_daily_skips_done_workspaces():
    from workflows import allocate

    ws = _workspace()
    out = allocate.run()
    assert out["workspaces"][str(ws["id"])]["trigger"] == "manual"
    assert "id" in allocate.run(trigger="daily")["workspaces"][str(ws["id"])]
    assert allocate.run(trigger="daily")["workspaces"][str(ws["id"])] == {"skipped": "今天已做过每日分配"}


def test_scheduler_intake_interval_comes_from_settings(monkeypatch):
    import dataclasses

    from registry import settings

    monkeypatch.setattr(settings, "_loaded", dataclasses.replace(settings.get(), intake_check_s=45))
    s = scheduler.Scheduler()
    assert s._feishu[0].name == "feishu_intake" and s._feishu[0].interval_s == 45


def test_run_sweep_passes_its_now_to_the_sweep(monkeypatch):
    seen: list = []
    monkeypatch.setattr(execution, "sweep", lambda conn, now=None: seen.append(now) or {
        "expired_runs": [], "revoked_runs": [], "orphan_attempts": [], "aborted": 0, "unknown": 0})
    at = NOW + timedelta(minutes=5)
    scheduler.run_sweep(at)
    scheduler.run_jobs_once(at)
    assert seen == [at, at]
