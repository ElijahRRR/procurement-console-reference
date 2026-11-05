"""全局设置：默认行、取值校验、本阶段不能关闭模拟模式、事件。"""

from __future__ import annotations

import pytest

from services import settings_svc
from services.errors import DomainError
from tests import factories as f


def test_get_creates_default_row(conn):
    ws = conn.execute("INSERT INTO procure.workspaces (name) VALUES ('DEMO 无设置') RETURNING id").fetchone()["id"]
    assert settings_svc.get(conn, ws) == {"default_max_delivery_days": 10, "simulation_only": True,
                                          "logistics_sync_time": "08:30", "real_purchase_available": False}
    assert f.count(conn, "workspace_settings", "workspace_id = %s", (ws,)) == 1


def test_update_valid_values_writes_event(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    out = settings_svc.update(conn, ws, {"default_max_delivery_days": 14, "logistics_sync_time": "09:15",
                                         "simulation_only": True, "real_purchase_available": False}, uid)
    assert out["default_max_delivery_days"] == 14 and out["logistics_sync_time"] == "09:15"
    ev = conn.execute("SELECT * FROM procure.events WHERE entity = 'settings'").fetchall()
    assert len(ev) == 1 and ev[0]["detail"]["changes"] == {"default_max_delivery_days": [10, 14],
                                                           "logistics_sync_time": ["08:30", "09:15"]}
    assert ev[0]["actor_id"] == uid
    row = conn.execute("SELECT updated_by FROM procure.workspace_settings WHERE workspace_id = %s", (ws,)).fetchone()
    assert row["updated_by"] == uid
    settings_svc.update(conn, ws, {"default_max_delivery_days": 14}, uid)       # 无变化不记事件
    assert f.count(conn, "events", "entity = 'settings'") == 1


@pytest.mark.parametrize("patch", [
    {"default_max_delivery_days": 0},
    {"default_max_delivery_days": 61},
    {"default_max_delivery_days": "10"},
    {"default_max_delivery_days": True},
    {"logistics_sync_time": "24:00"},
    {"logistics_sync_time": "8:30"},
    {"logistics_sync_time": 830},
    {"simulation_only": False},
    {"simulation_only": "true"},
    {"unknown_key": 1},
])
def test_update_rejects_invalid_values(conn, workspace, patch):
    with pytest.raises(DomainError) as e:
        settings_svc.update(conn, workspace["id"], patch, workspace["user_id"])
    assert e.value.code == "VALIDATION" and e.value.http_status == 422
    assert settings_svc.get(conn, workspace["id"])["simulation_only"] is True


def test_simulation_only_cannot_be_disabled_message(conn, workspace):
    with pytest.raises(DomainError) as e:
        settings_svc.update(conn, workspace["id"], {"simulation_only": False}, workspace["user_id"])
    assert "模拟" in e.value.message
