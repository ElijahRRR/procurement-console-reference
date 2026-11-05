"""`python cli.py serve`：HTTP 服务（uvicorn `server.app:app`）+ 后台调度（契约 §9）。

入口层文件（与 cli.py 同层），可以同时用 services 与 workflows；server 不导入本文件。

|任务|频率|线程|
|---|---|---|
|租约清扫 workflows.sweep.run|settings.sweep_interval_s（默认 30 秒）|核心线程|
|每日分配 workflows.allocate.run(trigger='daily')|北京时间每天 00:01 起，每个工作区每个北京日期一次|核心线程|
|飞书读取 workflows.feishu_intake.run（到期连接）|settings.intake_check_s（默认 30 秒）查一次到期连接（各连接按 poll_minutes）|飞书线程|
|飞书写回 workflows.feishu_writeback.run|settings.writeback_interval_s（默认 60 秒）|飞书线程|

每个任务都调用与 ``python cli.py <任务>`` 相同的 workflows.<任务>.run()（契约 §9：单独执行与后台执行是同一件事）。

- 清扫单独放在核心线程：飞书接口慢或卡住时，执行权租约照样按时清扫。
- 每个任务各自 try/except 并记日志，一次失败不会让循环退出；连续失败只在第一次打印堆栈，恢复时记一条。
- 每个任务自己开事务（registry.db.pg_conn，在 workflows 里）；飞书读取每个连接一个事务，读飞书失败时失败记录随该事务提交。
- 每日分配：进程内记下已处理的北京日期（省得每拍查库）；真正的去重在库里（allocation.daily_done 查
  allocation_runs），所以 00:01 之后重启不会重做当天的每日分配；服务停过、当天还没做的工作区启动后补做一次。
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

from registry import settings
from services import clock

log = logging.getLogger("scheduler")

#: 每日分配的北京时间
DAILY_ALLOCATION_AT = time(0, 1)
#: 循环的节拍（秒）：每拍检查一次哪些任务到期（只是轮询粒度，不是业务参数）
TICK_S = 1.0


# ── 单个任务（每个都自己开事务）────────────────────────────────────────────────


def run_sweep(now: datetime) -> dict[str, Any]:
    """输入：当前时刻 → 输出：清扫摘要 {expired_runs, revoked_runs, orphan_attempts, aborted, unknown}（workflows.sweep.run）。"""
    from workflows import sweep

    return sweep.run()


def run_feishu_intake(now: datetime) -> dict[str, Any]:
    """输入：当前时刻 → 输出：{due, connections: {连接 id: 读取统计 | {error, code}}}（飞书未配置时另有 skipped）。

    即 workflows.feishu_intake.run(now=now)：先查到期连接，没有到期的就不建飞书客户端；每个连接一个事务。
    """
    from workflows import feishu_intake

    return feishu_intake.run(now=now)


def run_feishu_writeback(now: datetime) -> dict[str, Any]:
    """输入：当前时刻 → 输出：{due, sent, done, retrying, failed, superseded, waiting_verification[, error]}。"""
    from workflows import feishu_writeback

    return feishu_writeback.run(now=now)


def daily_due(now: datetime, last_done: date | None) -> bool:
    """输入：当前时刻、上次每日分配的北京日期 → 输出：现在是否该跑当天的每日分配（北京时间 ≥ 00:01 且今天还没跑过）。"""
    local = now.astimezone(clock.BJ_TZ)
    return local.time() >= DAILY_ALLOCATION_AT and last_done != local.date()


def run_daily_allocation(now: datetime) -> dict[str, Any]:
    """输入：当前时刻 → 输出：{bj_date, workspaces: {工作区 id: 分配摘要 | {skipped} | {error}}}（每个工作区一个事务；
    当天已做过每日分配的工作区跳过，见 allocation.allocate_daily）。"""
    from workflows import allocate

    out = allocate.run(trigger="daily")
    done = sum(1 for v in out.get("workspaces", {}).values() if "id" in v)
    log.info("每日分配（北京 %s）：%d 个工作区分配，%d 个已做过或失败", clock.bj_date(now), done,
             len(out.get("workspaces", {})) - done)
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
                 intake_check_s: float | None = None, tick_s: float = TICK_S) -> None:
        s = settings.get()
        self.state = JobState()
        self.tick_s = tick_s
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._core = [_Periodic("sweep", float(sweep_interval_s or s.sweep_interval_s), run_sweep)]
        self._feishu = [
            _Periodic("feishu_intake", float(intake_check_s or s.intake_check_s), run_feishu_intake),
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

    for name in ("httpx", "httpcore"):          # 它们的 INFO 请求日志带多维表格 token
        logging.getLogger(name).setLevel(logging.WARNING)
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
