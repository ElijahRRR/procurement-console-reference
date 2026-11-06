"""工作区全局设置（契约 §10「设置」）：默认配送上限、模拟模式、物流同步时间、文件导入的准入规则。

文件导入的准入规则（file_admission）：保存的只是工作区改过的键；读取时与 normalize.FILE_ADMISSION_DEFAULTS、
normalize.DEFAULT_ADMISSION 合并成完整规则（文件预览不另传规则时就用它）。

本阶段 `real_purchase_available` 恒为 false，`simulation_only` 不允许改成 false（方案 v2 §2）。
"""

from __future__ import annotations

import re
from typing import Any

from psycopg.types.json import Jsonb

from services import clock, events, normalize
from services.errors import validation

#: 只读键：前端可能整份回传，忽略即可
_READONLY_KEYS = frozenset({"real_purchase_available"})
_EDITABLE_KEYS = ("default_max_delivery_days", "simulation_only", "logistics_sync_time", "file_admission")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def get_row(conn, workspace_id: int) -> dict[str, Any]:
    """输入：工作区 → 输出：workspace_settings 原始行（缺失时先建默认行）。"""
    conn.execute(
        "INSERT INTO procure.workspace_settings (workspace_id, updated_at) VALUES (%s, %s) "
        "ON CONFLICT (workspace_id) DO NOTHING",
        (workspace_id, clock.now()),
    )
    return conn.execute(
        "SELECT * FROM procure.workspace_settings WHERE workspace_id = %s", (workspace_id,)
    ).fetchone()


def get(conn, workspace_id: int) -> dict[str, Any]:
    """输入：工作区 → 输出：{default_max_delivery_days, simulation_only, logistics_sync_time, real_purchase_available,
    file_admission}。"""
    row = get_row(conn, workspace_id)
    return _out(row)


def simulation_only(conn, workspace_id: int) -> bool:
    """输入：工作区 → 输出：当前是否只允许模拟执行（额度统计按这个标记区分模拟 / 真实尝试）。"""
    return bool(get_row(conn, workspace_id)["simulation_only"])


def file_admission(conn, workspace_id: int) -> dict[str, Any]:
    """输入：工作区 → 输出：文件导入用的完整准入规则（缺省值 + 工作区改过的键）。"""
    return _file_admission(get_row(conn, workspace_id))


def _file_admission(row: dict[str, Any]) -> dict[str, Any]:
    saved = row.get("file_admission") or {}
    return normalize.effective_admission({**normalize.DEFAULT_ADMISSION, **normalize.FILE_ADMISSION_DEFAULTS,
                                          **{k: v for k, v in saved.items() if v is not None}})


def default_max_delivery_days(conn, workspace_id: int) -> int:
    """输入：工作区 → 输出：全局默认配送上限（天）。"""
    return int(get_row(conn, workspace_id)["default_max_delivery_days"])


def update(conn, workspace_id: int, patch: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、要改的键值、操作人 → 输出：改后的设置。

    校验：default_max_delivery_days 为 1–60 的整数；logistics_sync_time 为北京时间 'HH:MM'；
    simulation_only 本阶段只能为 true；file_admission 为准入规则（键同契约 §6，给出的键覆盖缺省，null = 恢复缺省）。
    未知键 → VALIDATION。有变化时写 'settings.updated' 事件。
    """
    if not isinstance(patch, dict):
        raise validation("设置内容格式不对")
    row = get_row(conn, workspace_id)
    new: dict[str, Any] = {}
    for key, value in patch.items():
        if key in _READONLY_KEYS:
            continue
        if key not in _EDITABLE_KEYS:
            raise validation(f"未知的设置项：{key}", field=key)
        if key == "default_max_delivery_days":
            if isinstance(value, bool) or not isinstance(value, int):
                raise validation("默认配送上限必须是整数天数", field=key)
            if not 1 <= value <= 60:
                raise validation("默认配送上限必须在 1 到 60 天之间", field=key)
            new[key] = value
        elif key == "simulation_only":
            if not isinstance(value, bool):
                raise validation("模拟模式开关必须是 true 或 false", field=key)
            if value is False:
                raise validation("本阶段只允许模拟执行，不能关闭模拟模式", field=key)
            new[key] = value
        elif key == "logistics_sync_time":
            if not isinstance(value, str) or not _HHMM_RE.match(value.strip()):
                raise validation("物流同步时间应为 HH:MM（北京时间，如 08:30）", field=key)
            new[key] = value.strip()
        elif key == "file_admission":
            if value is not None and not isinstance(value, dict):
                raise validation("文件导入的准入规则格式不对", field=key)
            merged = {**(row.get("file_admission") or {}), **normalize.check_admission(value or {})}
            new[key] = {k: v for k, v in merged.items() if v is not None} if value is not None else {}

    changes = {k: [row[k], v] for k, v in new.items() if row[k] != v}
    if changes:
        sets = ", ".join(f"{k} = %s" for k in changes)
        conn.execute(
            f"UPDATE procure.workspace_settings SET {sets}, updated_at = %s, updated_by = %s "
            "WHERE workspace_id = %s",
            [*(Jsonb(v[1]) if isinstance(v[1], dict) else v[1] for v in changes.values()), clock.now(),
             actor_user_id, workspace_id],
        )
        events.record(conn, workspace_id, "settings", workspace_id, "settings.updated",
                      actor_kind="user" if actor_user_id else "system", actor_id=actor_user_id,
                      detail={"changes": changes})
    return get(conn, workspace_id)


def _out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "default_max_delivery_days": row["default_max_delivery_days"],
        "simulation_only": row["simulation_only"],
        "logistics_sync_time": row["logistics_sync_time"],
        "real_purchase_available": False,
        "file_admission": _file_admission(row),
    }
