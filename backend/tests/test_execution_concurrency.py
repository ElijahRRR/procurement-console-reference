"""执行权互斥的并发验证：两条数据库连接（两个线程）同时开始批次 / 同时领取，只有一个取得执行权
（v1 §10「同账号同时打开两个插件页面」「同账号在 Mac 和另一台电脑同时触发」）；以及加锁顺序里「浏览器行可以先于
执行闸门」这一条为什么不会和清除模拟结果互相等待。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from registry import db
from services import clock, devices, execution, simulation
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


def test_concurrent_scheduled_starts_for_same_slot_one_runs_rest_account_busy_then_slot_done(clean_db, conn, workspace):
    """同一个买家号登在多个浏览器上，到点同时触发同一个时间点：只执行一次。赢家的批次还在跑时，其余一律
    ACCOUNT_BUSY（带持有者；不随并发时序、也不随数据库报的是哪个唯一索引在 ACCOUNT_BUSY / SLOT_DONE 之间摇摆），
    插件不记这个时间点、下一分钟再问；赢家正常结束之后，谁来问都是 SLOT_DONE，插件这才把它记成已处理。"""
    acc, devs = _commit_setup(conn, workspace, devices_n=4, purchase_mode="scheduled",
                              schedule_times=["09:30", "10:00"])
    start = lambda c, i: fx.start_run(c, devs[i], trigger="scheduled", slot="09:30")      # noqa: E731
    results = _race(clean_db, 4, start)
    wins = [i for i, r in enumerate(results) if isinstance(r, dict)]
    assert len(wins) == 1, results
    winner = devs[wins[0]]
    losses = [r for r in results if not isinstance(r, dict)]
    assert all(isinstance(r, DomainError) and r.code == "ACCOUNT_BUSY" for r in losses), results
    assert all(r.detail["holder_device_id"] == winner["id"] for r in losses), results
    # 赢家还在跑：再来一轮（这次冲突都落在预查上）仍然是 ACCOUNT_BUSY / 赢家自己是 DEVICE_BUSY
    again = _race(clean_db, 4, start)
    assert [r.code for r in again] == ["DEVICE_BUSY" if i == wins[0] else "ACCOUNT_BUSY" for i in range(4)], again
    execution.finish_run(conn, winner, results[wins[0]]["run_id"])
    conn.commit()
    for _ in range(2):                                           # 正常结束之后：这个时间点今天已经执行过
        done = _race(clean_db, 4, start)
        assert all(isinstance(r, DomainError) and r.code == "SLOT_DONE" for r in done), done
    n = conn.execute("SELECT count(*) AS n FROM procure.execution_runs WHERE account_id = %s AND scheduled_slot = %s",
                     (acc["id"], "09:30")).fetchone()["n"]
    assert n == 1


def test_concurrent_scheduled_starts_after_the_slots_run_died_exactly_one_takes_over(clean_db, conn, workspace):
    """时间点的批次没跑完就结束（这里是租约过期）：时间点空出来，几个浏览器同时来接手，只有一个开得成，其余
    ACCOUNT_BUSY。"""
    acc, devs = _commit_setup(conn, workspace, devices_n=3, purchase_mode="scheduled", schedule_times=["09:30"])
    first = fx.start(conn, devs[0], trigger="scheduled", slot="09:30")
    conn.execute("UPDATE procure.execution_runs SET lease_expires_at = %s WHERE id = %s", (NOW, first))
    assert execution.sweep(conn)["expired_runs"] == [first]
    conn.commit()
    results = _race(clean_db, 3, lambda c, i: fx.start_run(c, devs[i], trigger="scheduled", slot="09:30"))
    assert sum(isinstance(r, dict) for r in results) == 1, results
    assert all(r.code == "ACCOUNT_BUSY" for r in results if not isinstance(r, dict)), results
    assert _active_runs(conn, acc) == 1


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


def _waiting_on_gate(conn) -> int:
    """输出：本测试库里此刻有几个会话在等咨询锁（执行闸门）。pg_locks 是整个集群的，所以要按库过滤。"""
    return conn.execute(
        "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())").fetchone()["n"]


def test_start_run_waiting_for_the_gate_does_not_block_simulation_reset(clean_db, conn, workspace):
    """加锁顺序是「浏览器行 → 执行闸门 → 批次 → 任务 → 尝试 → 账号」：开始批次先 FOR UPDATE 锁住自己的浏览器行，
    再等闸门。它拿着浏览器行等的时候，持有独占闸门的清除模拟结果必须能做完——前提是清除从不锁浏览器行
    （execution_gate 的文档字符串写明的不变量）。哪天清除开始锁 devices 行，这里会死锁或超时。"""
    ws = workspace["id"]
    acc_a = fx.exec_account(conn, ws, name="环境-DEMO-A")
    acc_b = fx.exec_account(conn, ws, name="环境-DEMO-B")
    dev_a = fx.logged_in_device(conn, workspace, account=acc_a, name="DEMO-闸门-A")
    dev_b = fx.logged_in_device(conn, workspace, account=acc_b, name="DEMO-闸门-B")
    order = fx.assigned_order(conn, ws, acc_a)
    run_a = fx.start(conn, dev_a)
    fx.claim(conn, dev_a, run_a)                                   # 有一笔模拟的在途尝试等着被清除
    conn.commit()

    out: dict = {}
    gate_held, go_reset = threading.Event(), threading.Event()

    def resetter():
        try:
            with db.pg_conn(clean_db, statement_timeout_s=20) as c:
                execution.execution_gate(c, ws, exclusive=True)
                gate_held.set()
                go_reset.wait(15)
                out["reset"] = simulation.reset(c, ws, workspace["user_id"])
        except Exception as exc:  # noqa: BLE001 - 带回主线程断言
            out["reset"] = exc

    def starter():
        try:
            with db.pg_conn(clean_db, statement_timeout_s=20) as c:
                out["start"] = fx.start_run(c, dev_b)
        except Exception as exc:  # noqa: BLE001
            out["start"] = exc

    def reporter():
        try:
            with db.pg_conn(clean_db, statement_timeout_s=20) as c:
                out["report"] = devices.report_identity(c, dev_a, fx.identity(acc_a["amazon_customer_id"]))
        except Exception as exc:  # noqa: BLE001
            out["report"] = exc

    t_reset = threading.Thread(target=resetter)
    t_reset.start()
    assert gate_held.wait(10)
    t_start = threading.Thread(target=starter)
    t_start.start()
    deadline = time.monotonic() + 10
    while _waiting_on_gate(conn) < 1 and time.monotonic() < deadline:      # 开始批次已锁住 B 的浏览器行，在等闸门
        time.sleep(0.02)
    assert _waiting_on_gate(conn) == 1 and "start" not in out
    locked = conn.execute("SELECT id FROM procure.devices WHERE id = %s FOR UPDATE SKIP LOCKED",
                          (dev_b["id"],)).fetchone()
    conn.rollback()
    assert locked is None                                          # B 的浏览器行确实被它拿着
    t_report = threading.Thread(target=reporter)                   # A 的浏览器行没人拿：身份上报不用等清除
    t_report.start()
    t_report.join(10)
    assert not t_report.is_alive() and out["report"]["login"]["state"] == "ready"
    go_reset.set()
    for t in (t_reset, t_start):
        t.join(30)
        assert not t.is_alive()
    assert not any(isinstance(v, Exception) for v in out.values()), out
    assert out["reset"]["runs_ended"] == [run_a] and out["reset"]["orders_restored"] == [order["id"]]
    assert out["start"]["account"]["id"] == acc_b["id"]            # 清除提交之后，等着的开始批次照常开出来
    assert fx.run_row(conn, run_a)["end_reason"] == "simulation_reset"
    assert fx.run_row(conn, out["start"]["run_id"])["ended_at"] is None
