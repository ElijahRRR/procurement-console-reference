"""时间唯一入口：当前时刻、北京日期、站点日期。

业务日期一律按北京时间（Asia/Shanghai）计算；配送天数按站点时区（美国站 America/Los_Angeles）。
测试用 `freeze(dt)` 固定当前时刻，`unfreeze()` 恢复。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

BJ_TZ = ZoneInfo("Asia/Shanghai")
SITE_TZ = {"US": ZoneInfo("America/Los_Angeles")}

_frozen: datetime | None = None


def now() -> datetime:
    """输入：无 → 输出：当前 UTC 时刻（带时区）。"""
    if _frozen is not None:
        return _frozen
    return datetime.now(timezone.utc)


def freeze(dt: datetime) -> None:
    """输入：带时区的时刻 → 输出：无（此后 now() 返回它）。只给测试用。"""
    global _frozen
    if dt.tzinfo is None:
        raise ValueError("freeze() 需要带时区的 datetime")
    _frozen = dt.astimezone(timezone.utc)


def unfreeze() -> None:
    global _frozen
    _frozen = None


def bj_date(ts: datetime) -> date:
    """输入：带时区的时刻 → 输出：它在北京时间的日期。"""
    if ts.tzinfo is None:
        raise ValueError("bj_date() 需要带时区的 datetime")
    return ts.astimezone(BJ_TZ).date()


def bj_today() -> date:
    """输入：无 → 输出：北京时间的今天。"""
    return bj_date(now())


def site_today(site: str = "US") -> date:
    """输入：站点 → 输出：站点时区的今天（算配送天数用）。"""
    return now().astimezone(SITE_TZ[site]).date()


def bj_datetime(d: date, hhmm: str = "00:00") -> datetime:
    """输入：北京日期 + 'HH:MM' → 输出：对应的 UTC 时刻。"""
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=BJ_TZ).astimezone(timezone.utc)
