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


def _commit_setup(conn, workspace, *, devices_n):
    acc = fx.exec_account(conn, workspace["id"])
    devs = [fx.paired_device(conn, workspace, account=acc, name=f"DEMO-并发-{i}") for i in range(devices_n)]
    conn.commit()
    return acc, devs


def test_two_devices_start_concurrently_exactly_one_wins(clean_db, conn, workspace):
    acc, devs = _commit_setup(conn, workspace, devices_n=2)
    results = _race(clean_db, 2, lambda c, i: execution.start_run(c, devs[i], trigger="manual", simulated=True))
    wins = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, DomainError)]
    assert len(wins) == 1 and len(losses) == 1, results
    assert losses[0].code == "ACCOUNT_BUSY"
    winner = next(devs[i] for i, r in enumerate(results) if isinstance(r, dict))
    assert losses[0].detail["holder_device_id"] == winner["id"]
    assert losses[0].detail["holder_device_name"] == winner["name"]
    n = conn.execute("SELECT count(*) AS n FROM procure.execution_runs WHERE account_id = %s AND ended_at IS NULL",
                     (acc["id"],)).fetchone()["n"]
    assert n == 1


def test_same_device_two_concurrent_starts_one_wins(clean_db, conn, workspace):
    """同一台设备的两个插件页面同时点开始。"""
    acc, devs = _commit_setup(conn, workspace, devices_n=1)
    results = _race(clean_db, 2, lambda c, i: execution.start_run(c, devs[0], trigger="manual", simulated=True))
    assert sum(isinstance(r, dict) for r in results) == 1, results
    loss = next(r for r in results if not isinstance(r, dict))
    assert isinstance(loss, DomainError) and loss.code == "ACCOUNT_BUSY"
    assert loss.detail["holder_device_id"] == devs[0]["id"]


def test_many_concurrent_starts_single_execution_right(clean_db, conn, workspace):
    acc, devs = _commit_setup(conn, workspace, devices_n=4)
    results = _race(clean_db, 4, lambda c, i: execution.start_run(c, devs[i], trigger="scheduled", simulated=True))
    assert sum(isinstance(r, dict) for r in results) == 1, results
    assert all(r.code == "ACCOUNT_BUSY" for r in results if not isinstance(r, dict))


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
