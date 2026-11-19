"""领取时复核来源指定（契约 §3.3、方案 v2 修订二 §E.6）：指定变了而分配还没重算时，原账号领不到这笔任务。
与 orders._assignment_problem 共用 designation.off_target；人工改派不受约束。全部数据虚构。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services import allocation, clock, designation, execution, orders
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)   # 北京 2026-11-18 10:00


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _env(conn, workspace):
    """两个可执行的账号 A、B，各有一台登录着它的浏览器。"""
    ws = workspace["id"]
    a = fx.exec_account(conn, ws, name="环境-DEMO-A")
    b = fx.exec_account(conn, ws, name="环境-DEMO-B")
    return a, b, fx.logged_in_device(conn, workspace, account=a), fx.logged_in_device(conn, workspace, account=b)


def _designate(conn, oid, cid=None, name=None):
    conn.execute("UPDATE procure.purchase_orders SET designated_customer_id = %s, designated_buyer_name = %s "
                 "WHERE id = %s", (cid, name, oid))


def _claim_events(conn, oid):
    return [r["detail"] for r in conn.execute(
        "SELECT detail FROM procure.events WHERE order_id = %s AND kind = 'order.status_changed' ORDER BY id",
        (oid,)).fetchall() if (r["detail"] or {}).get("action") == "claim_recheck"]


def test_old_account_cannot_claim_after_the_designation_moved(conn, workspace):
    """验收 修订二 第 6 条：指定变了而分配还没重算（例如旧库回填之后、还没点「重新分配」）→ 原账号领取时这笔被解除
    分配并跳过，别的任务照常领取；下一次分配把它交给现在指定的账号，由那个账号领走。"""
    ws = workspace["id"]
    a, b, dev_a, dev_b = _env(conn, workspace)
    moved = fx.assigned_order(conn, ws, a, suggested_purchase_date=clock.bj_today() - timedelta(days=1))
    plain = fx.assigned_order(conn, ws, a)
    _designate(conn, moved["id"], cid=b["amazon_customer_id"])                # 分配还停在 A 上，来源已经指定 B
    run_a = fx.start(conn, dev_a)
    got = fx.claim(conn, dev_a, run_a)
    assert got["order_id"] == plain["id"]                                     # 排在前面的那笔被跳过
    row = fx.order_row(conn, moved["id"])
    assert (row["status"], row["assigned_account_id"], row["assignment_kind"]) == ("queued", None, None)
    assert row["status_reason"] == "来源指定的买家号已不是账号 环境-DEMO-A，领取时解除分配"
    assert row["designated_customer_id"] == b["amazon_customer_id"]            # 指定本身不动
    ev = _claim_events(conn, moved["id"])
    assert len(ev) == 1 and ev[0]["to"] == "queued"
    allocation.allocate(conn, ws, trigger="manual")
    row = fx.order_row(conn, moved["id"])
    assert (row["status"], row["assigned_account_id"], row["assignment_kind"]) == ("assigned", b["id"], "source")
    assert fx.claim(conn, dev_b, fx.start(conn, dev_b))["order_id"] == moved["id"]


def test_nothing_else_to_claim_reports_how_many_were_released(conn, workspace):
    ws = workspace["id"]
    a, b, dev_a, _ = _env(conn, workspace)
    gone = fx.assigned_order(conn, ws, a)
    unresolvable = fx.assigned_order(conn, ws, a)
    _designate(conn, gone["id"], name="环境-DEMO-B")
    _designate(conn, unresolvable["id"], cid="A1DEMO0777")                    # 指定解析不出账号也不能留在 A 上
    out = execution.next_attempt(conn, dev_a, fx.start(conn, dev_a))
    assert out["attempt"] is None
    assert out["reason"] == "今天没有可执行的任务（2 笔来源指定的买家号已不是本账号、已解除分配）"
    for o in (gone, unresolvable):
        row = fx.order_row(conn, o["id"])
        assert row["status"] == "queued" and row["status_reason"].endswith("领取时解除分配")
    allocation.allocate(conn, ws, trigger="manual")
    assert fx.order_row(conn, gone["id"])["assigned_account_id"] == b["id"]
    assert "还没有登记为账号" in fx.order_row(conn, unresolvable["id"])["status_reason"]


def test_designation_that_still_points_here_and_manual_assignments_are_claimable(conn, workspace):
    """指定解析到的就是本账号 → 照常领取（按 ID、按名称宽松匹配都算）；人工改派不受来源约束。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b, dev_a, _ = _env(conn, workspace)
    by_id = fx.assigned_order(conn, ws, a, suggested_purchase_date=clock.bj_today() - timedelta(days=3))
    by_name = fx.assigned_order(conn, ws, a, suggested_purchase_date=clock.bj_today() - timedelta(days=2))
    manual = f.ingest_ready_order(conn, ws, suggested_purchase_date=clock.bj_today() - timedelta(days=1))
    _designate(conn, by_id["id"], cid=a["amazon_customer_id"])
    _designate(conn, by_name["id"], name="环境-demo-a")
    _designate(conn, manual["id"], cid=b["amazon_customer_id"])
    orders.assign_manual(conn, ws, manual["id"], a["id"], uid)
    run = fx.start(conn, dev_a)
    claimed = []
    for _ in range(3):
        att = fx.claim(conn, dev_a, run)
        claimed.append(att["order_id"])
        fx.complete_success(conn, dev_a, att)
    assert claimed == [by_id["id"], by_name["id"], manual["id"]]
    assert _claim_events(conn, manual["id"]) == []


def test_aborted_attempt_does_not_go_back_to_the_old_account(conn, workspace):
    """采购中来源把买家号改成了 B（在途的这一次不受影响）；这次未提交就中止、任务回到已分配后，A 的同一批次再领取
    拿不到它。"""
    ws = workspace["id"]
    a, b, dev_a, dev_b = _env(conn, workspace)
    src = f.feishu_source(conn, ws, "recC01")
    kw = dict(upstream_order_no="PO-DEMO-C01", unit_price=Decimal("25.00"), price_cap=Decimal("45.00"))
    o = f.ingest_ready_order(conn, ws, source=src, external_buyer_customer_id=a["amazon_customer_id"], **kw)
    allocation.allocate(conn, ws, trigger="intake")
    run_a = fx.start(conn, dev_a)
    att = fx.claim(conn, dev_a, run_a)
    assert att["order_id"] == o["id"]
    out = orders.ingest(conn, ws, f.make_record(external_buyer_customer_id=b["amazon_customer_id"], **kw), src)
    assert out.verdict == "update" and out.status == "executing"
    assert out.reason.endswith("；任务已开始采购，不影响这一次")
    allocation.allocate(conn, ws, trigger="intake")                           # 在途的任务不在池里
    assert fx.order_row(conn, o["id"])["assigned_account_id"] == a["id"]
    assert execution.progress(conn, dev_a, att["attempt_id"], stage="checkout")["continue"] is True
    execution.report_result(conn, dev_a, att["attempt_id"], fx.failure_payload("ABORTED_BY_USER", "DEMO"))
    again = execution.next_attempt(conn, dev_a, run_a)
    assert again["attempt"] is None
    row = fx.order_row(conn, o["id"])
    assert row["status"] == "queued" and row["assigned_account_id"] is None
    assert "来源指定的买家号已不是账号 环境-DEMO-A" in row["status_reason"]
    allocation.allocate(conn, ws, trigger="manual")
    assert fx.claim(conn, dev_b, fx.start(conn, dev_b))["order_id"] == o["id"]


def test_claim_reads_the_account_table_once_and_only_when_needed(conn, workspace, monkeypatch):
    ws = workspace["id"]
    a, b, dev_a, _ = _env(conn, workspace)
    calls = []
    real = designation.load_context
    monkeypatch.setattr(designation, "load_context", lambda *x, **k: calls.append(x) or real(*x, **k))
    plain = fx.assigned_order(conn, ws, a)
    run = fx.start(conn, dev_a)
    att = fx.claim(conn, dev_a, run)
    assert att["order_id"] == plain["id"] and calls == []                     # 没有带指定的候选任务：不读账号表
    fx.complete_success(conn, dev_a, att)
    for _ in range(3):
        o = fx.assigned_order(conn, ws, a)
        _designate(conn, o["id"], cid=b["amazon_customer_id"])
    out = execution.next_attempt(conn, dev_a, run)
    assert out["attempt"] is None and len(calls) == 1


def test_buyer_change_after_claim_does_not_stop_the_attempt_in_flight(conn, workspace):
    """领取后来源只改了买家号 / 买家号 ID：不算执行相关的来源变化——进度照常继续、提交登记不被拒绝，这一次照常完成。"""
    ws = workspace["id"]
    a, b, dev_a, _ = _env(conn, workspace)
    src = f.feishu_source(conn, ws, "recC02")
    kw = dict(upstream_order_no="PO-DEMO-C02", unit_price=Decimal("25.00"), price_cap=Decimal("45.00"))
    o = f.ingest_ready_order(conn, ws, source=src, external_buyer_customer_id=a["amazon_customer_id"], **kw)
    allocation.allocate(conn, ws, trigger="intake")
    att = fx.claim(conn, dev_a, fx.start(conn, dev_a))
    out = orders.ingest(conn, ws, f.make_record(external_buyer_customer_id=b["amazon_customer_id"],
                                                external_buyer_account="环境-DEMO-B", **kw), src)
    assert out.verdict == "update"
    row = fx.order_row(conn, o["id"])
    assert row["needs_attention"] is False and row["conflict"] is None
    assert execution.progress(conn, dev_a, att["attempt_id"], stage="checkout")["continue"] is True
    res = fx.complete_success(conn, dev_a, att)                               # 过护栏、登记提交、上报成功都不受影响
    assert res["order_status"] == "purchased"
    row = fx.order_row(conn, o["id"])
    assert row["designated_customer_id"] == b["amazon_customer_id"]            # 指定已按来源更新，留给之后（如重采）


def test_amount_recheck_of_a_source_task_leaves_it_queued_not_on_another_account(conn, workspace):
    """来源指定的任务在领取时因金额已不在账号区间被解除分配：下一次分配仍不会把它给别的账号，而是写明指定账号接不了。"""
    ws = workspace["id"]
    a, b, dev_a, _ = _env(conn, workspace)
    o = f.ingest_ready_order(conn, ws, unit_price=Decimal("30.00"), price_cap=Decimal("50.00"),
                             external_buyer_customer_id=a["amazon_customer_id"])
    allocation.allocate(conn, ws, trigger="intake")
    assert fx.order_row(conn, o["id"])["assignment_kind"] == "source"
    conn.execute("UPDATE procure.buyer_accounts SET max_amount = 29 WHERE id = %s", (a["id"],))    # 没经过账号服务：不重新分配
    out = execution.next_attempt(conn, dev_a, fx.start(conn, dev_a))
    assert out["attempt"] is None and "1 笔金额已不在区间、已解除分配" in out["reason"]
    allocation.allocate(conn, ws, trigger="manual")
    row = fx.order_row(conn, o["id"])
    assert row["status"] == "queued" and row["assigned_account_id"] is None
    assert row["status_reason"] == ("金额 $30.00 不在来源指定的账号 环境-DEMO-A 的区间 $0.00–$29.00 内；"
                                    "调整账号的金额区间，或改派到其他账号")
