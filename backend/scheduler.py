"""`python cli.py serve`：HTTP 服务（uvicorn `server.app:app`）+ 后台调度（契约 §9）。

入口层文件（与 cli.py 同层），可以同时用 services 与 workflows；server 不导入本文件。

|任务|频率|线程|
|---|---|---|
|租约清扫 execution.sweep|settings.sweep_interval_s（默认 30 秒）|核心线程|
|每日分配（全部工作区，trigger='daily'）|北京时间每天 00:01 起，每个北京日期一次|核心线程|
|飞书读取：到期连接逐个 feishu_intake.run_connection|每 INTAKE_CHECK_S 秒查一次到期连接（各连接按 poll_minutes）|飞书线程|
|飞书写回 writeback.flush|settings.writeback_interval_s（默认 60 秒）|飞书线程|

- 清扫单独放在核心线程：飞书接口慢或卡住时，执行权租约照样按时清扫。
- 每个任务各自 try/except 并记日志，一次失败不会让循环退出；连续失败只在第一次打印堆栈，恢复时记一条。
- 每个任务自己开事务（registry.db.pg_conn）；飞书读取每个连接一个事务，读飞书失败时失败记录随该事务提交。
- 每日分配按进程内记下的北京日期去重；进程在 00:01 之后才启动时，启动后补跑一次当天的分配（等同人工「重新分配」）。
- 停止：uvicorn 退出（Ctrl+C / SIGTERM）后置停止事件并等待线程结束；线程是守护线程，不会挂住进程退出。
"""

from __future__ import annotations

import logging
import threading
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from registry import db, settings
from services import clock, execution, feishu_connections, feishu_intake, writeback
from services.errors import DomainError

log = logging.getLogger("scheduler")

#: 每日分配的北京时间
DAILY_ALLOCATION_AT = time(0, 1)
#: 多久查一次「到期」的飞书连接（各连接自己的 poll_minutes 决定是否到期）
INTAKE_CHECK_S = 30
#: 循环的节拍（秒）：每拍检查一次哪些任务到期
TICK_S = 1.0

JOB_NAMES = ("sweep", "daily_allocation", "feishu_intake", "feishu_writeback")


# ── 单个任务（每个都自己开事务）────────────────────────────────────────────────


def run_sweep(now: datetime) -> dict[str, Any]:
    """输入：当前时刻 → 输出：清扫摘要 {expired_runs, revoked_runs, orphan_attempts, aborted, unknown}（一个事务）。"""
    with db.pg_conn() as conn:
        summary = execution.sweep(conn, now)
    if summary["expired_runs"] or summary["revoked_runs"] or summary["orphan_attempts"]:
        log.info("租约清扫：%s", summary)
    return summary


def run_feishu_intake(now: datetime) -> dict[str, Any]:
    """输入：当前时刻 → 输出：{due, connections: {连接 id: 读取统计 | {error, code}}}（飞书未配置时另有 skipped）。

    先查到期连接（跨工作区）；没有到期的就不建飞书客户端。每个连接一个事务：读飞书失败时服务层已把失败记录写进
    该事务，这里捕获 DomainError 后正常提交，失败历史得以保留；一个连接出错不影响其他连接。
    """
    with db.pg_conn() as conn:
        due = feishu_intake.due_connections(conn, now)
    out: dict[str, Any] = {"due": len(due), "connections": {}}
    if not due:
        return out
    try:
        client = feishu_connections.make_client()
    except DomainError as exc:
        out["skipped"] = exc.message
        return out
    try:
        for ws, cid in due:
            try:
                with db.pg_conn() as conn:
                    try:
                        res = feishu_intake.run_connection(conn, ws, cid, trigger="schedule", client=client)
                        out["connections"][str(cid)] = {k: res[k] for k in (*feishu_intake.STAT_KEYS, "run_id")}
                    except DomainError as exc:
                        log.warning("飞书连接 %s 读取失败：%s", cid, exc.message)
                        out["connections"][str(cid)] = {"error": exc.message, "code": exc.code}
            except Exception as exc:  # noqa: BLE001 - 后台任务：记录并继续下一个连接
                log.exception("飞书连接 %s 读取异常", cid)
                out["connections"][str(cid)] = {"error": str(exc), "code": "INTERNAL"}
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()
    return out


def run_feishu_writeback(now: datetime) -> dict[str, Any]:
    """输入：当前时刻 → 输出：{due, sent, done, retrying, failed, superseded, waiting_verification[, error]}（一个事务）。"""
    with db.pg_conn() as conn:
        stats = writeback.flush(conn, now=now)
    if stats.get("sent") or stats.get("failed"):
        log.info("飞书写回：%s", stats)
    return stats


def daily_due(now: datetime, last_done: date | None) -> bool:
    """输入：当前时刻、上次每日分配的北京日期 → 输出：现在是否该跑当天的每日分配（北京时间 ≥ 00:01 且今天还没跑过）。"""
    local = now.astimezone(clock.BJ_TZ)
    return local.time() >= DAILY_ALLOCATION_AT and last_done != local.date()


def run_daily_allocation(now: datetime) -> dict[str, Any]:
    """输入：当前时刻 → 输出：{bj_date, workspaces: {工作区 id: 分配摘要 | {error}}}（每个工作区一个事务）。"""
    from workflows import allocate

    out = allocate.run(trigger="daily")
    log.info("每日分配（北京 %s）：%d 个工作区", clock.bj_date(now), len(out.get("workspaces", {})))
    return {"bj_date": clock.bj_date(now).isoformat(), **out}


# ── 调度状态与一次全部执行 ───────────────────────────────────────────────────


@dataclass
class JobState:
    """调度的进程内状态：每日分配已跑到的北京日期、各任务的连续失败次数。"""

    last_daily: date | None = None
    failures: dict[str, int] = field(default_factory=dict)


def _guarded(name: str, fn: Callable[[], dict[str, Any]], state: JobState) -> dict[str, Any]:
    """输入：任务名、任务函数、调度状态 → 输出：任务摘要；抛异常时为 {error}（连续失败只在第一次打印堆栈）。"""
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - 后台任务：一次失败不能让调度退出
        n = state.failures.get(name, 0) + 1
        state.failures[name] = n
        if n == 1:
            log.exception("后台任务 %s 失败", name)
        else:
            log.warning("后台任务 %s 连续第 %d 次失败：%s", name, n, exc)
        return {"error": str(exc) or exc.__class__.__name__}
    if state.failures.get(name):
        log.info("后台任务 %s 已恢复（此前连续失败 %d 次）", name, state.failures[name])
        state.failures[name] = 0
    return result


def _daily(now: datetime, state: JobState) -> dict[str, Any]:
    if not daily_due(now, state.last_daily):
        return {"skipped": "未到北京时间 00:01 或今天已分配", "last_daily": state.last_daily}
    out = run_daily_allocation(now)
    state.last_daily = clock.bj_date(now)
    return out


def run_jobs_once(now: datetime | None = None, state: JobState | None = None) -> dict[str, Any]:
    """输入：当前时刻（缺省 clock.now()）、调度状态（缺省新建）→ 输出：{sweep, daily_allocation, feishu_intake,
    feishu_writeback}：每个任务的摘要，失败的为 {error}。全部任务各跑一次（每日分配仍按北京日期去重），从不抛异常。"""
    now = now or clock.now()
    st = state if state is not None else JobState()
    return {
        "sweep": _guarded("sweep", lambda: run_sweep(now), st),
        "daily_allocation": _guarded("daily_allocation", lambda: _daily(now, st), st),
        "feishu_intake": _guarded("feishu_intake", lambda: run_feishu_intake(now), st),
        "feishu_writeback": _guarded("feishu_writeback", lambda: run_feishu_writeback(now), st),
    }


# ── 后台线程 ──────────────────────────────────────────────────────────────────


@dataclass
class _Periodic:
    """按单调时钟计间隔的周期任务（首次立即执行）。"""

    name: str
    interval_s: float
    fn: Callable[[datetime], dict[str, Any]]
    next_at: float = 0.0


class Scheduler:
    """后台调度：核心线程（清扫、每日分配）+ 飞书线程（读取、写回）。start() 启动，stop() 置停止事件并等待结束。"""

    def __init__(self, *, sweep_interval_s: float | None = None, writeback_interval_s: float | None = None,
                 intake_check_s: float = INTAKE_CHECK_S, tick_s: float = TICK_S) -> None:
        s = settings.get()
        self.state = JobState()
        self.tick_s = tick_s
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._core = [_Periodic("sweep", float(sweep_interval_s or s.sweep_interval_s), run_sweep)]
        self._feishu = [
            _Periodic("feishu_intake", float(intake_check_s), run_feishu_intake),
            _Periodic("feishu_writeback", float(writeback_interval_s or s.writeback_interval_s), run_feishu_writeback),
        ]

    @property
    def running(self) -> bool:
        """输入：无 → 输出：是否还有调度线程在运行。"""
        return any(t.is_alive() for t in self._threads)

    def start(self) -> None:
        """输入：无 → 输出：无（启动两个守护线程；重复调用无效）。"""
        if self.running:
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._loop, args=(self._core, True), name="pc-scheduler-core", daemon=True),
            threading.Thread(target=self._loop, args=(self._feishu, False), name="pc-scheduler-feishu", daemon=True),
        ]
        for t in self._threads:
            t.start()
        log.info("后台调度已启动：清扫每 %.0f 秒，飞书读取每 %.0f 秒检查到期连接，写回每 %.0f 秒，每日分配北京时间 %s",
                 self._core[0].interval_s, self._feishu[0].interval_s, self._feishu[1].interval_s,
                 DAILY_ALLOCATION_AT.strftime("%H:%M"))

    def stop(self, timeout: float = 30.0) -> None:
        """输入：等待秒数 → 输出：无（置停止事件；正在执行的任务做完这一轮后线程退出）。"""
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        if self.running:
            log.warning("后台调度线程未能在 %.0f 秒内结束（守护线程，进程退出时一并结束）", timeout)

    def _loop(self, jobs: list[_Periodic], with_daily: bool) -> None:
        while not self._stop.is_set():
            for job in jobs:
                if self._stop.is_set():
                    break
                mono = _time.monotonic()
                if mono >= job.next_at:
                    job.next_at = mono + job.interval_s
                    _guarded(job.name, lambda j=job: j.fn(clock.now()), self.state)
            if with_daily and not self._stop.is_set():
                now = clock.now()
                if daily_due(now, self.state.last_daily):
                    _guarded("daily_allocation", lambda n=now: _daily(n, self.state), self.state)
            self._stop.wait(self.tick_s)


# ── serve ─────────────────────────────────────────────────────────────────────


def _flag(v: Any) -> bool:
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def serve(host: str | None = None, port: int | str | None = None, jobs: Any = "1", log_level: str = "info",
          **unknown: Any) -> None:
    """输入：监听地址、端口、是否启动后台调度、uvicorn 日志级别 → 输出：无（阻塞运行 uvicorn，退出后停止后台调度）。

    地址与端口缺省取 settings（127.0.0.1:8790）；jobs=0 不启动后台调度（只跑 HTTP）。应用为 'server.app:app'。
    """
    if unknown:
        raise SystemExit(f"serve 不认识的参数：{'、'.join(sorted(unknown))}（可用 host、port、jobs、log_level）")
    import uvicorn

    s = settings.get()
    bind_host = host or s.host
    bind_port = int(port) if port not in (None, "") else int(s.port)
    sched = Scheduler() if _flag(jobs) else None
    if sched is not None:
        sched.start()
    else:
        log.info("后台调度未启动（jobs=%s）", jobs)
    try:
        log.info("采购控制台监听 http://%s:%d（前端目录 %s）", bind_host, bind_port, s.web_dist)
        uvicorn.run("server.app:app", host=bind_host, port=bind_port, log_level=log_level, proxy_headers=False,
                    server_header=False)
    finally:
        if sched is not None:
            sched.stop()
