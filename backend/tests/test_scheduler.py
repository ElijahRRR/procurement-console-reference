"""后台调度的每日分配（scheduler._daily）：只有全部工作区都没报错才记下当天已做；有工作区失败（例如被数据库判为死锁
回滚）或整次抛错 → 隔 DAILY_RETRY_S 秒再试，已做完的工作区不重复做。只走服务层 / 工作流，全部数据虚构。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

import scheduler
from services import allocation, clock
from tests import factories as f

NOW = datetime(2026, 11, 18, 17, 0, tzinfo=timezone.utc)      # 北京 2026-11-19 01:00
BJ_DAY = date(2026, 11, 19)
RETRY = timedelta(seconds=scheduler.DAILY_RETRY_S)


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _daily_rows(conn, ws: int | None = None) -> int:
    if ws is None:
        return f.count(conn, "allocation_runs", "trigger = 'daily'")
    return f.count(conn, "allocation_runs", "trigger = 'daily' AND workspace_id = %s", (ws,))


def _flaky(monkeypatch, fail_workspaces: set[int], times: int = 1) -> list[int]:
    """allocate_daily 对 fail_workspaces 里的工作区前 times 次抛死锁错误 → 输出：调用过的工作区 id 列表。"""
    real = allocation.allocate_daily
    calls: list[int] = []
    left = {w: times for w in fail_workspaces}

    def flaky(c, w, **kw):
        calls.append(w)
        if left.get(w, 0) > 0:
            left[w] -= 1
            raise psycopg.errors.DeadlockDetected("deadlock detected (DEMO)")
        return real(c, w, **kw)

    monkeypatch.setattr(allocation, "allocate_daily", flaky)
    return calls


def test_failed_daily_allocation_is_retried(conn, workspace, monkeypatch):
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-A")
    f.ingest_ready_order(conn, ws)
    conn.commit()
    calls = _flaky(monkeypatch, {ws})
    state = scheduler.JobState()

    first = scheduler.run_jobs_once(NOW, state)["daily_allocation"]
    assert "error" in first["workspaces"][str(ws)]
    assert state.last_daily is None and state.daily_retry_at == NOW + RETRY      # 不记当天已做
    assert _daily_rows(conn) == 0

    soon = scheduler._daily(NOW + timedelta(seconds=1), state)                   # 重试间隔内不重跑
    assert soon == {"skipped": "上次有工作区分配失败，稍后重试", "retry_at": (NOW + RETRY).isoformat()}
    assert calls == [ws]

    later = NOW + RETRY
    clock.freeze(later)
    again = scheduler.run_jobs_once(later, state)["daily_allocation"]
    assert "id" in again["workspaces"][str(ws)]
    assert state.last_daily == BJ_DAY and state.daily_retry_at is None
    assert _daily_rows(conn) == 1                                                # 当天恰好一次
    assert "skipped" in scheduler._daily(later + timedelta(hours=1), state)
    assert calls == [ws, ws]


def test_daily_retry_redoes_only_the_failed_workspace(conn, workspace, monkeypatch):
    ws_ok = workspace["id"]
    ws_bad = f.make_workspace(conn)["id"]
    conn.commit()
    _flaky(monkeypatch, {ws_bad})
    state = scheduler.JobState()

    first = scheduler._daily(NOW, state)
    assert "id" in first["workspaces"][str(ws_ok)] and "error" in first["workspaces"][str(ws_bad)]
    assert state.last_daily is None
    again = scheduler._daily(NOW + RETRY, state)
    assert again["workspaces"][str(ws_ok)] == {"skipped": "今天已做过每日分配"}      # 做完的不重复做
    assert "id" in again["workspaces"][str(ws_bad)]
    assert state.last_daily == BJ_DAY
    assert (_daily_rows(conn, ws_ok), _daily_rows(conn, ws_bad)) == (1, 1)


def test_daily_run_that_raises_is_retried_after_the_interval(conn, workspace, monkeypatch):
    ws = workspace["id"]
    conn.commit()
    real = scheduler.run_daily_allocation
    boom = {"left": 1}

    def flaky(now):
        if boom["left"]:
            boom["left"] -= 1
            raise psycopg.OperationalError("DEMO 数据库暂时连不上")
        return real(now)

    monkeypatch.setattr(scheduler, "run_daily_allocation", flaky)
    state = scheduler.JobState()
    assert scheduler.run_jobs_once(NOW, state)["daily_allocation"] == {"error": "DEMO 数据库暂时连不上"}
    assert state.last_daily is None and state.failures["daily_allocation"] == 1
    assert "skipped" in scheduler.run_jobs_once(NOW + timedelta(seconds=5), state)["daily_allocation"]
    out = scheduler.run_jobs_once(NOW + RETRY, state)["daily_allocation"]
    assert "id" in out["workspaces"][str(ws)] and state.last_daily == BJ_DAY
    assert state.failures["daily_allocation"] == 0
    assert _daily_rows(conn) == 1
