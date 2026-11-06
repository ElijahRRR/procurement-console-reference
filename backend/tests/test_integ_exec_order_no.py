"""真实尝试的亚马逊订单号重复（评审 D2-12）：库里的唯一索引是（工作区, 单号）限非模拟的成功尝试；
report_result 必须在写库前就按同一口径判重（含本任务之前的成功尝试，例如亚马逊取消后同意重采、新尝试却报回
旧单号），按结果待核实处理并写明原因，而不是抛出 UniqueViolation（500）；并发漏网的由 SAVEPOINT 兜底。

本阶段开不出真实批次，这里用 tests/factories.make_run(simulated=False) 直接造。全部数据虚构
（DEMO 名称、假买家号 ID、假地址、111-0000000-00000xx 形式的假单号）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services import clock, execution, orders, writeback
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
AMZ_NO = "111-0000000-0000021"


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


@pytest.fixture()
def real(conn, workspace, monkeypatch):
    calls: list = []
    monkeypatch.setattr(writeback, "enqueue_for_order", lambda c, ws, oid: calls.append((ws, oid)))
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    run = f.make_run(conn, ws, acc["id"], dev["id"], simulated=False)
    return {"ws": ws, "uid": workspace["user_id"], "acc": acc, "dev": dev, "run_id": run["id"], "calls": calls}


def _buy(conn, e, order_no: str, **order_kw) -> tuple[dict, dict, dict]:
    order = fx.assigned_order(conn, e["ws"], e["acc"], **order_kw)
    a = fx.claim(conn, e["dev"], e["run_id"])
    assert a["simulated"] is False and a["order_id"] == order["id"]
    fx.to_submitted(conn, e["dev"], a)
    res = execution.report_result(conn, e["dev"], a["attempt_id"], fx.success_payload(a, order_no))
    return order, a, res


def test_repurchase_reporting_the_old_order_number_goes_result_unknown_not_500(conn, real):
    order, a, res = _buy(conn, real, AMZ_NO, upstream_order_no="PO-DEMO-RP01")
    assert res == {"attempt_status": "succeeded", "order_status": "purchased"}
    orders.mark_amazon_cancelled(conn, real["ws"], order["id"], "DEMO 亚马逊取消", actor_user_id=real["uid"])
    orders.decide_repurchase(conn, real["ws"], order["id"], "approve", actor_user_id=real["uid"])
    if fx.order_row(conn, order["id"])["status"] != "assigned":
        orders.assign_manual(conn, real["ws"], order["id"], real["acc"]["id"], actor_user_id=real["uid"])
    b = fx.claim(conn, real["dev"], real["run_id"])
    assert b["order_id"] == order["id"] and b["kind"] == "repurchase"
    fx.to_submitted(conn, real["dev"], b)

    res = execution.report_result(conn, real["dev"], b["attempt_id"], fx.success_payload(b, AMZ_NO))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    att = fx.attempt_row(conn, b["attempt_id"])
    assert att["status"] == "unknown" and att["amazon_order_no"] == AMZ_NO
    assert AMZ_NO in att["error_detail"] and "本任务之前的第 1 次成功尝试" in att["error_detail"]
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "result_unknown" and o["current_attempt_id"] == b["attempt_id"]
    assert o["purchased_attempt_id"] == a["attempt_id"]                     # 旧的成功尝试不动
    assert AMZ_NO in o["status_reason"]
    assert fx.account_row(conn, real["acc"]["id"])["execution_paused"] is True
    assert real["calls"] == [(real["ws"], order["id"])]                     # 只有第一次成功入队写回
    # 插件补发同一份回执：原样返回，不报 500
    again = execution.report_result(conn, real["dev"], b["attempt_id"], fx.success_payload(b, AMZ_NO))
    assert again["duplicate"] is True and again["attempt_status"] == "unknown"


def test_order_number_owned_by_another_order_goes_result_unknown(conn, real):
    first, _, _ = _buy(conn, real, AMZ_NO, upstream_order_no="PO-DEMO-OWN1")
    _, b, res = _buy(conn, real, AMZ_NO, upstream_order_no="PO-DEMO-OWN2")
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    assert "已登记在任务 PO-DEMO-OWN1" in fx.attempt_row(conn, b["attempt_id"])["error_detail"]
    assert fx.order_row(conn, first["id"])["status"] == "purchased"


def test_same_attempt_resending_its_success_is_a_duplicate_not_a_clash(conn, real):
    _, a, _ = _buy(conn, real, AMZ_NO, upstream_order_no="PO-DEMO-SELF")
    again = execution.report_result(conn, real["dev"], a["attempt_id"], fx.success_payload(a, AMZ_NO))
    assert again == {"attempt_status": "succeeded", "order_status": "purchased", "duplicate": True}


def test_order_number_of_an_accepted_external_attempt_is_also_a_clash(conn, real):
    """人工接受的外部单号（kind external、成功、非模拟）同样占着这个单号。"""
    ext = f.ingest_ready_order(conn, real["ws"], upstream_order_no="PO-DEMO-EXTN")
    f.make_attempt(conn, real["ws"], ext["id"], None, status="succeeded", simulated=False, kind="external",
                   amazon_order_no=AMZ_NO)
    _, b, res = _buy(conn, real, AMZ_NO, upstream_order_no="PO-DEMO-EXT2")
    assert res["attempt_status"] == "unknown"
    assert "已登记在任务 PO-DEMO-EXTN" in fx.attempt_row(conn, b["attempt_id"])["error_detail"]


def test_unique_violation_in_the_write_is_caught_by_savepoint(conn, real, monkeypatch):
    """预检漏网（例如并发的另一笔刚提交）：唯一索引在 SAVEPOINT 里报错 → 按结果待核实处理，事务仍可用。"""
    first, _, _ = _buy(conn, real, AMZ_NO, upstream_order_no="PO-DEMO-SP01")
    monkeypatch.setattr(execution, "_order_no_problem", lambda c, att, no: None)
    second = fx.assigned_order(conn, real["ws"], real["acc"], upstream_order_no="PO-DEMO-SP02")
    b = fx.claim(conn, real["dev"], real["run_id"])
    fx.to_submitted(conn, real["dev"], b)
    res = execution.report_result(conn, real["dev"], b["attempt_id"], fx.success_payload(b, AMZ_NO))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    att = fx.attempt_row(conn, b["attempt_id"])
    assert att["status"] == "unknown" and att["amazon_order_no"] == AMZ_NO
    assert "已登记在别的成功尝试" in att["error_detail"]
    o = fx.order_row(conn, second["id"])
    assert o["status"] == "result_unknown" and o["purchased_attempt_id"] is None
    assert fx.order_row(conn, first["id"])["status"] == "purchased"
    assert fx.account_row(conn, real["acc"]["id"])["execution_paused"] is True
    assert real["calls"] == [(real["ws"], first["id"])]
    assert f.count(conn, "events", "entity = 'attempt' AND entity_id = %s AND kind = 'attempt.unknown'",
                   (b["attempt_id"],)) == 1
