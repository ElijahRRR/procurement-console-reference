"""执行权互斥的并发验证：两条数据库连接（两个线程）同时开始批次 / 同时领取，只有一个取得执行权
（v1 §10「同账号同时打开两个插件页面」「同账号在 Mac 和另一台电脑同时触发」）。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from registry import db
from services import clock, execution
from services.errors import DomainError
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _race(dsn, n, fn):
    """输入：DSN、并发数、fn(conn, i) → 输出：[(i, 结果或 DomainError)]。每个线程一条连接，同时出发，各自提交。"""
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i):
        try:
            with db.pg_conn(dsn, statement_timeout_s=20) as c:
                barrier.wait(timeout=10)
                results[i] = fn(c, i)
        except DomainError as exc:
            results[i] = exc
        except Exception as exc:  # noqa: BLE001 - 把意外异常带回主线程断言
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def _commit_setup(conn, workspace, *, devices_n, **acc_kw):
    acc = fx.exec_account(conn, workspace["id"], **acc_kw)
    devs = [fx.logged_in_device(conn, workspace, account=acc, name=f"DEMO-并发-{i}") for i in range(devices_n)]
    conn.commit()
    return acc, devs


def _active_runs(conn, acc):
    return conn.execute("SELECT count(*) AS n FROM procure.execution_runs WHERE account_id = %s AND ended_at IS NULL",
                        (acc["id"],)).fetchone()["n"]


def test_two_devices_start_concurrently_exactly_one_wins(clean_db, conn, workspace):
    acc, devs = _commit_setup(conn, workspace, devices_n=2)
    results = _race(clean_db, 2, lambda c, i: fx.start_run(c, devs[i]))
    wins = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, DomainError)]
    assert len(wins) == 1 and len(losses) == 1, results
    assert losses[0].code == "ACCOUNT_BUSY"
    winner = next(devs[i] for i, r in enumerate(results) if isinstance(r, dict))
    assert losses[0].detail["holder_device_id"] == winner["id"]
    assert losses[0].detail["holder_device_name"] == winner["name"]
    assert _active_runs(conn, acc) == 1


def test_same_device_two_concurrent_starts_one_wins_other_device_busy(clean_db, conn, workspace):
    """同一个浏览器的两个插件页面同时点开始：浏览器行的锁让它们排队，后到的看到自己那个未结束的批次 → DEVICE_BUSY。"""
    acc, devs = _commit_setup(conn, workspace, devices_n=1)
    results = _race(clean_db, 2, lambda c, i: fx.start_run(c, devs[0]))
    assert sum(isinstance(r, dict) for r in results) == 1, results
    win = next(r for r in results if isinstance(r, dict))
    loss = next(r for r in results if not isinstance(r, dict))
    assert isinstance(loss, DomainError) and loss.code == "DEVICE_BUSY" and loss.http_status == 409
    assert loss.detail["run_id"] == win["run_id"] and loss.detail["account_name"] == acc["name"]
    assert _active_runs(conn, acc) == 1


def test_many_concurrent_manual_starts_single_execution_right(clean_db, conn, workspace):
    acc, devs = _commit_setup(conn, workspace, devices_n=4)
    results = _race(clean_db, 4, lambda c, i: fx.start_run(c, devs[i]))
    assert sum(isinstance(r, dict) for r in results) == 1, results
    assert all(r.code == "ACCOUNT_BUSY" for r in results if not isinstance(r, dict)), results
    assert _active_runs(conn, acc) == 1


def test_concurrent_scheduled_starts_for_same_slot_one_runs_rest_slot_done(clean_db, conn, workspace):
    """同一个买家号登在多个浏览器上，到点同时触发同一个时间点：只执行一次，其余一律 SLOT_DONE
    （不随并发时序在 SLOT_DONE / ACCOUNT_BUSY 之间摇摆，插件据此把这个时间点记成已处理）。"""
    acc, devs = _commit_setup(conn, workspace, devices_n=4, purchase_mode="scheduled",
                              schedule_times=["09:30", "10:00"])
    for _ in range(3):                                           # 多跑几轮，冲突落在预查或唯一索引上都要是同一个码
        results = _race(clean_db, 4, lambda c, i: fx.start_run(c, devs[i], trigger="scheduled", slot="09:30"))
        wins = [r for r in results if isinstance(r, dict)]
        if not wins:                                             # 第 2、3 轮：这个时间点今天已经执行过
            assert all(isinstance(r, DomainError) and r.code == "SLOT_DONE" for r in results), results
            continue
        assert len(wins) == 1, results
        assert all(r.code == "SLOT_DONE" for r in results if not isinstance(r, dict)), results
        winner = next(devs[i] for i, r in enumerate(results) if isinstance(r, dict))
        execution.finish_run(conn, winner, wins[0]["run_id"])
        conn.commit()
    n = conn.execute("SELECT count(*) AS n FROM procure.execution_runs WHERE account_id = %s AND scheduled_slot = %s",
                     (acc["id"], "09:30")).fetchone()["n"]
    assert n == 1


def test_concurrent_scheduled_starts_for_different_slots_loser_gets_account_busy(clean_db, conn, workspace):
    acc, devs = _commit_setup(conn, workspace, devices_n=2, purchase_mode="scheduled",
                              schedule_times=["09:30", "10:00"])
    slots = ["09:30", "10:00"]
    results = _race(clean_db, 2, lambda c, i: fx.start_run(c, devs[i], trigger="scheduled", slot=slots[i]))
    assert sum(isinstance(r, dict) for r in results) == 1, results
    loss = next(r for r in results if not isinstance(r, dict))
    assert isinstance(loss, DomainError) and loss.code == "ACCOUNT_BUSY"        # 下一分钟再问，不记已处理
    assert _active_runs(conn, acc) == 1


def test_concurrent_next_on_same_run_creates_one_attempt(clean_db, conn, workspace):
    """同一批次被两个请求同时领取：批次行锁让它们排队，第二个看到在途尝试 → ATTEMPT_IN_FLIGHT。"""
    acc, devs = _commit_setup(conn, workspace, devices_n=1)
    fx.assigned_order(conn, workspace["id"], acc)
    fx.assigned_order(conn, workspace["id"], acc)
    run_id = fx.start(conn, devs[0])
    conn.commit()
    results = _race(clean_db, 2, lambda c, i: execution.next_attempt(c, devs[0], run_id))
    got = [r for r in results if isinstance(r, dict) and r["attempt"] is not None]
    assert len(got) == 1, results
    other = next(r for r in results if r is not got[0])
    assert isinstance(other, DomainError) and other.code == "ATTEMPT_IN_FLIGHT"
    n = conn.execute("SELECT count(*) AS n FROM procure.purchase_attempts WHERE run_id = %s", (run_id,)).fetchone()["n"]
    assert n == 1
