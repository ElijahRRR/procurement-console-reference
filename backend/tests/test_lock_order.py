"""加锁顺序（契约 §3.2）：账号行（FOR NO KEY UPDATE）→ 工作区分配锁 → 任务行。

两条真实连接 + 线程：在入口函数里用 threading.Event 停住先动手的一方（只是把本来就有的时间窗撑开），等另一方确实
在等锁了再放行，确认两边都做完、没有一方因 deadlock detected 被回滚，两边的效果都提交了。评审列出的四种交错：

(a) 文件提交（分配锁 → ingest 锁任务行 → 分配把新任务分给 A）对改账号 A；
(b) 领取（锁任务行 → 插入尝试，外键检查要 A 行的 KEY SHARE）对改账号 A（A 行 → 分配锁 → 任务行）；
(c) 分配（分配锁 → 把任务分给 A，外键 KEY SHARE）对改账号 A；
(d) 归档账号（A 行 → 分配锁 → A 的任务行 → 分配）对单独的一次分配。

另有改名的变体：名称在唯一约束里，改名的 UPDATE 拿到的账号行锁会挡外键检查的 KEY SHARE，所以改名要先取分配锁、
再锁齐任务行，最后才改账号行——(b)(c) 各有一个改名用例，另加 (e) 人工改派（锁任务行 → 写分配，外键 KEY SHARE）
对给目标账号改名。

清除模拟结果（锁任务行 → 最后恢复只因模拟结果暂停的账号、重新分配）也按这个顺序：先锁要恢复的账号行、再取分配锁，
然后才锁任务行——(f) 清除对单独的一次分配、对改这个账号（优先级 / 改名）。

执行模块的结果上报是另一条链（批次 → 任务 → 尝试 → 暂停账号，不取分配锁）：(g) 上报在途模拟尝试的结果对清除模拟
结果——清除要等上报提交，不得先锁住这个人工暂停、不会被它恢复的账号。

每个用例都有硬超时：工作线程的连接带 statement_timeout，停住的一方最多等 PAUSE_S 秒，线程 join 最多 JOIN_S 秒；
真的卡住会很快失败，不会挂住整个测试。全部数据虚构。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import pytest

from registry import db
from services import accounts, allocation, clock, designation, execution, imports, simulation
from tests import factories as f
from tests import factories_exec as fx
from tests.api_helpers import IMPORT_HEADERS, import_row, xlsx_bytes

NOW = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)        # 北京 2026-11-18 10:00
#: 工作线程连接上的语句超时（秒）：真的互相等住也会在这之内报错退出
STMT_TIMEOUT_S = 20
#: 停住的一方最多等多久（秒）
PAUSE_S = 15
#: 等另一方进入「等锁」状态的上限（秒）
BLOCK_WAIT_S = 10
#: 线程收尾的上限（秒）
JOIN_S = 40


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


class Gate:
    """停住一方：reached 表示已经停在钩子里，go 放行（最多等 PAUSE_S 秒）。"""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.go = threading.Event()

    def hold(self) -> None:
        self.reached.set()
        self.go.wait(PAUSE_S)


class Race:
    """两个工作线程，各用自己的一条连接、一个事务（正常结束即提交，出错回滚并记下错误）。"""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.result: dict[str, str] = {}
        self.pids: dict[str, int] = {}
        self.threads: dict[str, threading.Thread] = {}

    def start(self, name: str, fn: Callable[[Any], Any]) -> None:
        def work() -> None:
            try:
                with db.pg_conn(self.dsn, statement_timeout_s=STMT_TIMEOUT_S) as c:
                    self.pids[name] = c.info.backend_pid
                    fn(c)
                self.result[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - 结果交给断言
                self.result[name] = f"{type(exc).__name__}: " + " | ".join(str(exc).splitlines()[:3])

        t = threading.Thread(target=work, name=f"lock-order-{name}", daemon=True)
        self.threads[name] = t
        t.start()

    def wait_blocked(self, name: str) -> bool:
        """输入：线程名 → 输出：它的连接是否进入了「等锁」（pg_stat_activity.wait_event_type = 'Lock'）；
        它已经做完、或超时 → False。"""
        end = time.monotonic() + BLOCK_WAIT_S
        with db.pg_conn(self.dsn, autocommit=True) as c:
            while time.monotonic() < end:
                if not self.threads[name].is_alive():
                    return False
                pid = self.pids.get(name)
                if pid is not None:
                    row = c.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
                    if row is not None and row["wait_event_type"] == "Lock":
                        return True
                time.sleep(0.02)
        return False

    def finish(self) -> dict[str, str]:
        for t in self.threads.values():
            t.join(JOIN_S)
        stuck = [n for n, t in self.threads.items() if t.is_alive()]
        assert not stuck, f"线程没有在 {JOIN_S} 秒内结束：{stuck}"
        return dict(self.result)


def _play(dsn: str, gate: Gate, first: tuple[str, Callable[[Any], Any]],
          second: tuple[str, Callable[[Any], Any]]) -> dict[str, str]:
    """先跑 first，等它停在钩子里；再跑 second，等它确实在等锁；放行，收尾 → 两边的结果。"""
    race = Race(dsn)
    blocked = False
    try:
        race.start(*first)
        assert gate.reached.wait(BLOCK_WAIT_S), f"{first[0]} 没有走到停住的位置：{race.result}"
        race.start(*second)
        blocked = race.wait_blocked(second[0])
    finally:
        gate.go.set()
        result = race.finish()
    assert blocked, f"{second[0]} 没有在等 {first[0]} 的锁（交错没有发生）：{result}"
    return result


def _account(conn, account_id: int) -> dict[str, Any]:
    return conn.execute("SELECT * FROM procure.buyer_accounts WHERE id = %s", (account_id,)).fetchone()


def _order(conn, ws: int, no: str) -> dict[str, Any]:
    return conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = %s",
                        (ws, no)).fetchone()


def _previewed_batch(conn, ws: int, uid: int, rows: list[dict[str, Any]], filename: str) -> int:
    up = imports.upload(conn, ws, filename=filename, data=xlsx_bytes(rows, IMPORT_HEADERS), actor_user_id=uid)
    imports.preview(conn, ws, up["batch_id"])
    return up["batch_id"]


def test_a_file_commit_vs_account_edit(clean_db, conn, workspace, monkeypatch):
    """(a) 文件提交在 ingest 之后、分配之前停住（任务行已锁）；这时改账号 A 的优先级。提交的分配把新任务分给 A。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id="A1DEMO0001")
    first = _previewed_batch(conn, ws, uid, [import_row("PO-DEMO-LO-A1")], "demo-lock-1.xlsx")
    imports.commit(conn, ws, first, uid)
    assert _order(conn, ws, "PO-DEMO-LO-A1")["assigned_account_id"] == a["id"]
    second = _previewed_batch(conn, ws, uid, [import_row("PO-DEMO-LO-A1", 限价=45), import_row("PO-DEMO-LO-A2")],
                              "demo-lock-2.xlsx")
    conn.commit()

    gate = Gate()
    real = allocation.allocate

    def paused(c, w, *args, **kw):
        if kw.get("trigger") == "import" and not gate.reached.is_set():
            gate.hold()
        return real(c, w, *args, **kw)

    monkeypatch.setattr(allocation, "allocate", paused)
    result = _play(clean_db, gate,
                   ("import_commit", lambda c: imports.commit(c, ws, second, uid)),
                   ("account_edit", lambda c: accounts.update(c, ws, a["id"], {"priority": 7}, uid)))
    assert result == {"import_commit": "ok", "account_edit": "ok"}
    assert _account(conn, a["id"])["priority"] == 7
    assert conn.execute("SELECT status FROM procure.import_batches WHERE id = %s", (second,)).fetchone()["status"] == \
        "committed"
    new = _order(conn, ws, "PO-DEMO-LO-A2")
    assert (new["status"], new["assigned_account_id"]) == ("assigned", a["id"])


@pytest.mark.parametrize("patch", [{"priority": 7}, {"name": "环境-DEMO-A-改名"}], ids=["priority", "rename"])
def test_b_claim_vs_account_edit(clean_db, conn, workspace, monkeypatch, patch):
    """(b) 领取锁住任务行后、插入尝试之前停住；这时改账号 A（它的重新分配要锁同一行任务）。改名的变体：改名的
    UPDATE 拿的账号行锁会挡领取插入尝试时的外键检查，所以改名要先锁齐任务行再改账号行。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = fx.exec_account(conn, ws, name="环境-DEMO-A")
    dev = fx.logged_in_device(conn, workspace, account=a)
    o = fx.assigned_order(conn, ws, a)
    run_id = fx.start(conn, dev)
    conn.commit()

    gate = Gate()
    real = execution._claim

    def paused(*args, **kw):
        gate.hold()
        return real(*args, **kw)

    monkeypatch.setattr(execution, "_claim", paused)
    claimed: dict[str, Any] = {}

    def claimer(c):
        claimed.update(execution.next_attempt(c, dev, run_id))

    result = _play(clean_db, gate, ("claim", claimer),
                   ("account_edit", lambda c: accounts.update(c, ws, a["id"], patch, uid)))
    assert result == {"claim": "ok", "account_edit": "ok"}
    assert claimed["attempt"] is not None and claimed["attempt"]["order_id"] == o["id"]
    assert f.count(conn, "purchase_attempts", "purchase_order_id = %s", (o["id"],)) == 1
    row = _account(conn, a["id"])
    assert {k: row[k] for k in patch} == patch


@pytest.mark.parametrize("patch", [{"priority": 7}, {"name": "环境-DEMO-A-改名"}], ids=["priority", "rename"])
def test_c_allocation_vs_account_edit(clean_db, conn, workspace, monkeypatch, patch):
    """(c) 一次分配取到分配锁后停住，随后会把任务分给 A（外键检查要 A 行的 KEY SHARE）；这时改账号 A。
    改名的 UPDATE 拿的是与 KEY SHARE 冲突的行锁：改账号必须先取分配锁再改行。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id="A1DEMO0001")
    o = f.ingest_ready_order(conn, ws)                       # 待分配，还没分配
    conn.commit()

    gate = Gate()
    real = designation.load_context

    def paused(c, w):
        if threading.current_thread().name == "lock-order-allocation" and not gate.reached.is_set():
            gate.hold()                                     # 分配锁已经拿到
        return real(c, w)

    monkeypatch.setattr(designation, "load_context", paused)
    result = _play(clean_db, gate,
                   ("allocation", lambda c: allocation.allocate(c, ws, trigger="manual")),
                   ("account_edit", lambda c: accounts.update(c, ws, a["id"], patch, uid)))
    assert result == {"allocation": "ok", "account_edit": "ok"}
    row = _account(conn, a["id"])
    assert {k: row[k] for k in patch} == patch
    got = conn.execute("SELECT status, assigned_account_id FROM procure.purchase_orders WHERE id = %s",
                       (o["id"],)).fetchone()
    assert (got["status"], got["assigned_account_id"]) == ("assigned", a["id"])


def test_d_archive_vs_allocation(clean_db, conn, workspace, monkeypatch):
    """(d) 归档账号 A 锁住它已分配的任务、在重新分配之前停住；这时单独跑一次分配。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id="A1DEMO0001")
    b = f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id="A1DEMO0002", priority=9)
    o = f.ingest_ready_order(conn, ws, external_buyer_customer_id="A1DEMO0001")
    allocation.allocate(conn, ws, trigger="manual")
    assert _order(conn, ws, o["upstream_order_no"])["assigned_account_id"] == a["id"]
    conn.commit()

    gate = Gate()
    real = accounts._reallocate

    def paused(c, w, ak, aid):
        if threading.current_thread().name == "lock-order-archive" and not gate.reached.is_set():
            gate.hold()                                     # A 的任务行已锁
        return real(c, w, ak, aid)

    monkeypatch.setattr(accounts, "_reallocate", paused)
    result = _play(clean_db, gate,
                   ("archive", lambda c: accounts.archive(c, ws, a["id"], uid)),
                   ("allocation", lambda c: allocation.allocate(c, ws, trigger="manual")))
    assert result == {"archive": "ok", "allocation": "ok"}
    assert _account(conn, a["id"])["archived_at"] is not None
    got = _order(conn, ws, o["upstream_order_no"])
    assert got["status"] == "queued" and got["assigned_account_id"] is None      # 指定的账号已归档：不落到 B
    assert got["assigned_account_id"] != b["id"]
    assert f.count(conn, "allocation_runs") == 3                                  # 准备时一次 + 归档一次 + 单独一次


def test_e_manual_assignment_vs_account_rename(clean_db, conn, workspace, monkeypatch):
    """(e) 人工改派锁住任务行、校验完、写分配之前停住（写分配时外键检查要目标账号行的 KEY SHARE）；这时给目标账号改名
    （它的重新分配要锁同一行任务）。"""
    from services import orders

    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id="A1DEMO0001")
    o = f.ingest_ready_order(conn, ws)                       # 待分配，还没分配
    conn.commit()

    gate = Gate()
    real = allocation.check_manual_assignment

    def paused(*args, **kw):
        out = real(*args, **kw)
        if threading.current_thread().name == "lock-order-manual_assign" and not gate.reached.is_set():
            gate.hold()                                     # 任务行已锁、校验已过
        return out

    monkeypatch.setattr(allocation, "check_manual_assignment", paused)
    result = _play(clean_db, gate,
                   ("manual_assign", lambda c: orders.assign_manual(c, ws, o["id"], a["id"], uid)),
                   ("account_edit", lambda c: accounts.update(c, ws, a["id"], {"name": "环境-DEMO-A-改名"}, uid)))
    assert result == {"manual_assign": "ok", "account_edit": "ok"}
    assert _account(conn, a["id"])["name"] == "环境-DEMO-A-改名"
    got = conn.execute("SELECT status, assigned_account_id, assignment_kind FROM procure.purchase_orders "
                       "WHERE id = %s", (o["id"],)).fetchone()
    assert (got["status"], got["assigned_account_id"], got["assignment_kind"]) == ("assigned", a["id"], "manual")


def _captcha_paused_source_task(conn, workspace) -> tuple[dict[str, Any], dict[str, Any]]:
    """来源指定的任务 T 分给账号 A（kind source）；A 的模拟尝试遇到验证码 → A 只因模拟结果暂停，T 回到已分配
    （仍在分配池里）。输出 (A, T)。"""
    ws = workspace["id"]
    a = fx.exec_account(conn, ws, name="环境-DEMO-A")
    dev = fx.logged_in_device(conn, workspace, account=a)
    o = f.ingest_ready_order(conn, ws, external_buyer_customer_id=a["amazon_customer_id"])
    allocation.allocate(conn, ws, trigger="manual")
    att = fx.claim(conn, dev, fx.start(conn, dev))
    execution.report_result(conn, dev, att["attempt_id"], fx.failure_payload("CAPTCHA_ENCOUNTERED"))
    assert simulation.paused_only_by_simulation(conn, ws, a["id"]) is True
    got = fx.order_row(conn, o["id"])
    assert (got["status"], got["assigned_account_id"], got["assignment_kind"]) == ("assigned", a["id"], "source")
    return a, o


@pytest.mark.parametrize("other", ["allocation", "priority", "rename"])
def test_f_simulation_reset_vs_allocation_or_account_edit(clean_db, conn, workspace, monkeypatch, other):
    """(f) 清除模拟结果锁住要回退的任务、在恢复账号 A（改 A 行并重新分配）之前停住；这时单独跑一次分配，或改账号 A
    （优先级 / 改名）。清除要先锁 A 行、再取分配锁，然后才锁任务行。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, o = _captcha_paused_source_task(conn, workspace)
    conn.commit()

    gate = Gate()
    real = accounts.resume

    def paused(c, w, account_id, **kw):
        if threading.current_thread().name == "lock-order-reset" and not gate.reached.is_set():
            gate.hold()                                     # 任务行已锁，A 还没恢复
        return real(c, w, account_id, **kw)

    monkeypatch.setattr(accounts, "resume", paused)
    patch = {"allocation": None, "priority": {"priority": 7}, "rename": {"name": "环境-DEMO-A-改名"}}[other]
    second = ((lambda c: allocation.allocate(c, ws, trigger="manual")) if patch is None
              else (lambda c: accounts.update(c, ws, a["id"], patch, uid)))
    summary: dict[str, Any] = {}
    result = _play(clean_db, gate, ("reset", lambda c: summary.update(simulation.reset(c, ws, uid))), (other, second))
    assert result == {"reset": "ok", other: "ok"}
    assert summary["accounts_resumed"] == [a["id"]] and summary["orders_restored"] == [o["id"]]
    row = _account(conn, a["id"])
    assert row["execution_paused"] is False
    assert {k: row[k] for k in (patch or {})} == (patch or {})
    got = fx.order_row(conn, o["id"])
    assert (got["status"], got["assigned_account_id"], got["assignment_kind"]) == ("assigned", a["id"], "source")


def test_g_result_report_vs_simulation_reset(clean_db, conn, workspace, monkeypatch):
    """(g) 账号 A 有在途的模拟尝试时被人工暂停；插件上报这笔「遇到验证码」（锁住批次、任务、尝试，随后要再暂停 A），
    在暂停 A 之前停住；这时清除模拟结果。A 是人工暂停、不会被恢复，清除不预先锁它，等上报提交后照常清除。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = fx.exec_account(conn, ws, name="环境-DEMO-A")
    dev = fx.logged_in_device(conn, workspace, account=a)
    o = fx.assigned_order(conn, ws, a)
    att = fx.claim(conn, dev, fx.start(conn, dev))
    accounts.pause(conn, ws, a["id"], "DEMO 人工暂停", actor_kind="user", actor_id=uid)
    conn.commit()

    gate = Gate()
    real = accounts.pause

    def paused(c, w, account_id, reason, **kw):
        if threading.current_thread().name == "lock-order-report" and not gate.reached.is_set():
            gate.hold()                                     # 批次、任务、尝试已锁，A 还没暂停
        return real(c, w, account_id, reason, **kw)

    monkeypatch.setattr(accounts, "pause", paused)
    summary: dict[str, Any] = {}
    result = _play(clean_db, gate,
                   ("report", lambda c: execution.report_result(c, dev, att["attempt_id"],
                                                                fx.failure_payload("CAPTCHA_ENCOUNTERED"))),
                   ("reset", lambda c: summary.update(simulation.reset(c, ws, uid))))
    assert result == {"report": "ok", "reset": "ok"}
    assert summary["accounts_resumed"] == [] and summary["orders_restored"] == [o["id"]]
    assert _account(conn, a["id"])["execution_paused"] is True                    # 人工暂停不被自动恢复
    got = fx.attempt_row(conn, att["attempt_id"])
    assert (got["status"], got["resolution"]) == ("aborted", "simulation_reset")


@pytest.mark.parametrize("third", ["priority", "resume"])
def test_h_result_report_vs_simulation_reset_vs_account_edit(clean_db, conn, workspace, monkeypatch, third):
    """(h) 三方：插件上报这笔「遇到验证码」（锁住批次、任务、尝试），在暂停账号 A 之前停住；这时清除模拟结果，
    再改 A 的优先级或恢复 A（锁 A 行、随后取分配锁重新分配）。清除先在批次行上排队、不先拿分配锁，改账号能做完，
    上报随后暂停 A、提交，清除最后把只因模拟结果暂停的 A 恢复。清除如果先取了分配锁再等批次行，就是
    「上报等 A 行 → 改账号等分配锁 → 清除等批次行」的环。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = fx.exec_account(conn, ws, name="环境-DEMO-A")
    dev = fx.logged_in_device(conn, workspace, account=a)
    o = fx.assigned_order(conn, ws, a)
    att = fx.claim(conn, dev, fx.start(conn, dev))
    if third == "resume":
        accounts.pause(conn, ws, a["id"], "DEMO 人工暂停", actor_kind="user", actor_id=uid)
    conn.commit()

    gate = Gate()
    real = accounts.pause

    def paused(c, w, account_id, reason, **kw):
        if threading.current_thread().name == "lock-order-report" and not gate.reached.is_set():
            gate.hold()                                     # 批次、任务、尝试已锁，A 还没暂停
        return real(c, w, account_id, reason, **kw)

    monkeypatch.setattr(accounts, "pause", paused)
    edit = ((lambda c: accounts.update(c, ws, a["id"], {"priority": 7}, uid)) if third == "priority"
            else (lambda c: accounts.resume(c, ws, a["id"], actor_kind="user", actor_id=uid)))
    summary: dict[str, Any] = {}
    race = Race(clean_db)
    try:
        race.start("report", lambda c: execution.report_result(c, dev, att["attempt_id"],
                                                                 fx.failure_payload("CAPTCHA_ENCOUNTERED")))
        assert gate.reached.wait(BLOCK_WAIT_S), f"report 没有走到停住的位置：{race.result}"
        race.start("reset", lambda c: summary.update(simulation.reset(c, ws, uid)))
        assert race.wait_blocked("reset"), f"reset 没有在等 report 的锁：{race.result}"
        race.start(third, edit)
        end = time.monotonic() + BLOCK_WAIT_S               # 改账号要么做完，要么停在锁上（修复前停在分配锁上）
        while race.threads[third].is_alive() and time.monotonic() < end:
            if race.wait_blocked(third):
                break
    finally:
        gate.go.set()
        result = race.finish()
    assert result == {"report": "ok", "reset": "ok", third: "ok"}
    assert summary["orders_restored"] == [o["id"]] and summary["accounts_resumed"] == [a["id"]]
    row = _account(conn, a["id"])
    assert row["execution_paused"] is False                 # 上报的暂停起因是模拟结果，清除时恢复
    if third == "priority":
        assert row["priority"] == 7
    got = fx.attempt_row(conn, att["attempt_id"])
    assert (got["status"], got["resolution"]) == ("aborted", "simulation_reset")
