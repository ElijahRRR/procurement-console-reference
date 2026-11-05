"""任务状态机与人工动作：迁移表、人审、改派、覆盖值、恢复、取消、结果核实、重采、冲突处理、跨工作区。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from services import allocation, clock, orders, quota
from services.errors import DomainError
from tests import factories as f

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _kinds(conn, order_id):
    return [r["kind"] for r in conn.execute("SELECT kind FROM procure.events WHERE order_id = %s ORDER BY id",
                                            (order_id,)).fetchall()]


# ── 迁移表 ────────────────────────────────────────────────────────────────────


def test_transition_table_rejects_invalid_transitions(conn, workspace):
    ws = workspace["id"]
    order = f.ingest_ready_order(conn, ws)
    for frm, to in (("purchased", "queued"), ("closed", "queued"), ("queued", "executing"),
                    ("result_unknown", "exception"), ("cancelled", "executing"), ("executing", "closed")):
        row = f.set_status(conn, order["id"], frm)
        with pytest.raises(DomainError) as e:
            orders.transition(conn, row, to, actor_kind="system")
        assert e.value.code == "INVALID_TRANSITION" and e.value.http_status == 409


def test_transition_table_matches_contract():
    t = orders.ALLOWED_TRANSITIONS
    assert t["executing"] == {"assigned", "exception", "result_unknown", "purchased", "cancelled"}
    assert t["result_unknown"] == {"purchased", "assigned", "queued"}
    assert t["purchased"] == {"repurchase_pending"}
    assert t["closed"] == frozenset()
    assert "executing" in t["assigned"] and "executing" not in t["queued"]
    assert {"cancelled", "closed"} <= t["exception"] and "closed" in t["repurchase_pending"]
    for s in orders.PRE:
        assert "cancelled" in t[s]


def test_transition_writes_event_sets_changed_at_and_clears_assignment(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    order = f.ingest_ready_order(conn, ws)
    row = orders.transition(conn, order, "assigned", actor_kind="system",
                            updates={"assigned_account_id": acc["id"], "assignment_kind": "auto"})
    assert row["status"] == "assigned" and row["status_changed_at"] == NOW
    row = orders.transition(conn, row, "cancelled", reason="DEMO", actor_kind="user", actor_id=workspace["user_id"])
    assert row["assigned_account_id"] is None and row["status_reason"] == "DEMO"
    ev = conn.execute("SELECT * FROM procure.events WHERE order_id = %s AND kind = 'order.status_changed' "
                      "ORDER BY id DESC LIMIT 1", (order["id"],)).fetchone()
    assert ev["detail"]["from"] == "assigned" and ev["detail"]["to"] == "cancelled"
    assert ev["actor_kind"] == "user" and ev["actor_id"] == workspace["user_id"]


def test_transition_to_assigned_needs_account_and_detects_stale_rows(conn, workspace):
    ws = workspace["id"]
    order = f.ingest_ready_order(conn, ws)
    with pytest.raises(DomainError) as e:
        orders.transition(conn, order, "assigned", actor_kind="system")
    assert e.value.code == "INVALID_TRANSITION"
    orders.transition(conn, order, "cancelled", actor_kind="system")
    with pytest.raises(DomainError) as e:
        orders.transition(conn, order, "needs_info", actor_kind="system")   # order 仍是旧的 queued 行
    assert e.value.code == "CONFLICT"


def test_derive_status_priority():
    base = dict(review_status="approved", conflict=None, upstream_cancelled=False, external_amz_order_no=None,
                assigned_account_id=None, asin="B0DEMO0001", quantity=1, unit_price=Decimal("10"),
                price_cap=Decimal("20"), suggested_purchase_date=NOW.date(), recipient_name="Demo Buyer",
                address1="100 Demo Street", city="Demo City", state="FL", postal_code="32960", phone="555-0100")
    assert orders.derive_status(base) == "queued"
    assert orders.derive_status({**base, "assigned_account_id": 1}) == "assigned"
    assert orders.derive_status({**base, "phone": " "}) == "needs_info"
    assert orders.derive_status({**base, "conflict": {"x": 1}, "phone": None}) == "conflict"
    assert orders.derive_status({**base, "review_status": "unknown", "conflict": {"x": 1}}) == "pending_review"
    assert orders.derive_status({**base, "external_amz_order_no": "111", "review_status": "unknown"}) == "purchased"
    assert orders.derive_status({**base, "upstream_cancelled": True, "external_amz_order_no": "111"}) == "cancelled"
    assert orders.derive_status({**base, "asin": "bad", "override_asin": "B0DEMO0002"}) == "queued"
    assert orders.subtotal_eff({**base, "quantity": 2, "override_quantity": 3}) == Decimal("30.00")
    assert orders.subtotal_eff({**base, "unit_price": None}) is None


# ── 人审 ──────────────────────────────────────────────────────────────────────


def test_console_review_approve_and_reject(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws, review_value=None, review="unknown")
    assert o["status"] == "pending_review"
    d = orders.review(conn, ws, o["id"], "approve", "DEMO 核对通过", uid)
    assert d["status"] == "queued" and d["review_status"] == "approved"
    assert "order.reviewed" in _kinds(conn, o["id"])
    o2 = f.ingest_ready_order(conn, ws, review_value=None, review="unknown")
    d2 = orders.review(conn, ws, o2["id"], "reject", None, uid)
    assert d2["status"] == "pending_review" and d2["review_status"] == "rejected"
    with pytest.raises(DomainError):
        orders.review(conn, ws, o["id"], "maybe", None, uid)


def test_bulk_review_skips_missing_and_wrong_status(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.ingest_ready_order(conn, ws, review_value=None, review="unknown")
    b = f.ingest_ready_order(conn, ws, review_value=None, review="unknown")
    done = f.ingest_ready_order(conn, ws)
    f.set_status(conn, done["id"], "purchased")
    out = orders.bulk_review(conn, ws, [a["id"], b["id"], done["id"], 999999], "approve", None, uid)
    assert out["updated"] == [a["id"], b["id"]]
    assert [s["id"] for s in out["skipped"]] == [done["id"], 999999]


def test_console_approval_survives_file_reimport_without_review(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-CR", review_value=None, review="unknown")
    orders.review(conn, ws, o["id"], "approve", None, uid)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-CR", review_value="待审", review="unknown"),
                  f.file_source(conn, ws, 2))
    assert orders.get_row(conn, ws, o["id"])["status"] == "queued"


# ── 改派 ──────────────────────────────────────────────────────────────────────


def test_assign_manual_validates_range_capacity_and_enabled(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    narrow = f.make_account(conn, ws, name="N-DEMO", min_amount="20", max_amount="30", daily_limit=1)
    off = f.make_account(conn, ws, name="OFF-DEMO", purchase_enabled=False)
    big = f.ingest_ready_order(conn, ws, unit_price=Decimal("45.00"), price_cap=Decimal("60.00"))
    with pytest.raises(DomainError) as e:
        orders.assign_manual(conn, ws, big["id"], narrow["id"], uid)
    assert e.value.code == "VALIDATION" and "区间" in e.value.message
    with pytest.raises(DomainError) as e:
        orders.assign_manual(conn, ws, big["id"], off["id"], uid)
    assert "采购开关" in e.value.message
    ok = f.ingest_ready_order(conn, ws)
    d = orders.assign_manual(conn, ws, ok["id"], narrow["id"], uid)
    assert d["status"] == "assigned" and d["assignment_kind"] == "manual"
    assert d["assigned_account"] == {"id": narrow["id"], "name": "N-DEMO"}
    other = f.ingest_ready_order(conn, ws)
    with pytest.raises(DomainError) as e:
        orders.assign_manual(conn, ws, other["id"], narrow["id"], uid)
    assert "额度已满" in e.value.message
    pending = f.ingest_ready_order(conn, ws, review_value=None, review="unknown")
    with pytest.raises(DomainError) as e:
        orders.assign_manual(conn, ws, pending["id"], narrow["id"], uid)
    assert e.value.code == "INVALID_TRANSITION"


def test_reassign_between_accounts_and_unassign(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="A-DEMO")
    b = f.make_account(conn, ws, name="B-DEMO")
    o = f.ingest_ready_order(conn, ws)
    orders.assign_manual(conn, ws, o["id"], a["id"], uid)
    d = orders.assign_manual(conn, ws, o["id"], b["id"], uid)
    assert d["assigned_account"]["id"] == b["id"]
    d = orders.unassign(conn, ws, o["id"], uid)
    assert d["status"] == "queued" and d["assigned_account"] is None and d["status_reason"] == "人工解除分配"
    with pytest.raises(DomainError):
        orders.unassign(conn, ws, o["id"], uid)


# ── 覆盖值 ────────────────────────────────────────────────────────────────────


def test_set_override_unset_vs_none_and_rederive(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws, asin="bad")
    assert o["status"] == "needs_info" and o["missing_fields"] == ["ASIN"]
    d = orders.set_override(conn, ws, o["id"], asin="b0demo0007", note="换同款", actor_user_id=uid)
    assert d["status"] == "queued" and d["asin_eff"] == "B0DEMO0007" and d["has_override"]
    assert d["override"]["by_name"] == "测试所有者" and d["override"]["note"] == "换同款"
    d = orders.set_override(conn, ws, o["id"], price_cap="55", actor_user_id=uid)   # asin 未传 → 保留
    assert d["asin_eff"] == "B0DEMO0007" and d["price_cap_eff"] == "55.00" and d["price_cap"] == "40.00"
    d = orders.set_override(conn, ws, o["id"], asin=None, price_cap=None, actor_user_id=uid)
    assert d["status"] == "needs_info" and d["override"] is None and not d["has_override"]
    assert "order.override_set" in _kinds(conn, o["id"])
    with pytest.raises(DomainError):
        orders.set_override(conn, ws, o["id"], asin="TOO-SHORT", actor_user_id=uid)
    with pytest.raises(DomainError):
        orders.set_override(conn, ws, o["id"], quantity=0, actor_user_id=uid)
    with pytest.raises(DomainError):
        orders.set_override(conn, ws, o["id"], actor_user_id=uid)


def test_override_quantity_out_of_range_unassigns(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, min_amount="20", max_amount="30")
    o = f.ingest_ready_order(conn, ws)
    orders.assign_manual(conn, ws, o["id"], a["id"], uid)
    d = orders.set_override(conn, ws, o["id"], quantity=2, actor_user_id=uid)
    assert d["status"] == "queued" and d["subtotal_eff"] == "50.00" and "不在账号" in d["status_reason"]


def test_override_only_before_execution_or_in_exception(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws)
    f.set_status(conn, o["id"], "executing")
    with pytest.raises(DomainError) as e:
        orders.set_override(conn, ws, o["id"], price_cap="50", actor_user_id=uid)
    assert e.value.code == "INVALID_TRANSITION"
    f.set_status(conn, o["id"], "exception", status_reason="结算总额超过限价")
    d = orders.set_override(conn, ws, o["id"], price_cap="50", actor_user_id=uid)
    assert d["status"] == "exception" and d["price_cap_eff"] == "50.00"


# ── 恢复、取消 ────────────────────────────────────────────────────────────────


def test_resume_exception_rederives_and_resets_page_retries(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws)
    f.set_status(conn, o["id"], "exception", status_reason="提交前页面加载失败", page_load_retries=1,
                 assigned_account_id=acc["id"], assignment_kind="auto")
    d = orders.resume(conn, ws, o["id"], "已处理", uid)
    assert d["status"] == "assigned" and d["assigned_account"]["id"] == acc["id"]
    assert orders.get_row(conn, ws, o["id"])["page_load_retries"] == 0
    with pytest.raises(DomainError):
        orders.resume(conn, ws, o["id"], None, uid)


def test_cancel_and_uncancel(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws)
    allocation.allocate(conn, ws, trigger="manual")
    d = orders.cancel(conn, ws, o["id"], "客户撤单", uid)
    assert d["status"] == "cancelled" and d["assigned_account"] is None and d["status_reason"] == "人工取消：客户撤单"
    assert "uncancel" in d["allowed_actions"]
    d = orders.uncancel(conn, ws, o["id"], None, uid)
    assert d["status"] == "queued"
    f.set_status(conn, o["id"], "purchased")
    with pytest.raises(DomainError) as e:
        orders.cancel(conn, ws, o["id"], None, uid)
    assert e.value.code == "INVALID_TRANSITION"
    assert acc["id"]


def test_uncancel_refused_while_upstream_cancelled(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws, upstream_cancelled=True, after_sale_value="已取消")
    assert o["status"] == "cancelled"
    with pytest.raises(DomainError) as e:
        orders.uncancel(conn, ws, o["id"], None, uid)
    assert e.value.code == "VALIDATION"


# ── 结果待核实 ────────────────────────────────────────────────────────────────


def _unknown_order(conn, ws, acc, simulated=True):
    o = f.ingest_ready_order(conn, ws)
    att = f.make_attempt(conn, ws, o["id"], acc["id"], status="unknown", submitted=True, simulated=simulated)
    f.set_status(conn, o["id"], "result_unknown", assigned_account_id=acc["id"], assignment_kind="auto",
                 current_attempt_id=att["id"])
    conn.execute("UPDATE procure.buyer_accounts SET execution_paused = true, pause_reason = '结果待核实' "
                 "WHERE id = %s", (acc["id"],))
    return o, att


def test_resolve_unknown_ordered_marks_purchased_and_enqueues_writeback(conn, workspace, monkeypatch):
    ws, uid = workspace["id"], workspace["user_id"]
    calls = []
    monkeypatch.setattr(orders.writeback, "enqueue_for_order", lambda c, w, oid: calls.append((w, oid)))
    acc = f.make_account(conn, ws)
    o, att = _unknown_order(conn, ws, acc, simulated=False)
    with pytest.raises(DomainError):
        orders.resolve_unknown(conn, ws, o["id"], "ordered", actor_user_id=uid)          # 缺单号
    d = orders.resolve_unknown(conn, ws, o["id"], "ordered", amazon_order_no="112-0000000-0000001",
                               amounts={"total": "44.49", "tax": "2.52"}, note="已查到订单", actor_user_id=uid)
    assert d["status"] == "purchased"
    row = orders.get_row(conn, ws, o["id"])
    assert row["result_version"] == 1 and row["purchased_attempt_id"] == att["id"] and row["current_attempt_id"] is None
    a = conn.execute("SELECT * FROM procure.purchase_attempts WHERE id = %s", (att["id"],)).fetchone()
    assert a["status"] == "succeeded" and a["resolution"] == "ordered" and a["total"] == Decimal("44.49")
    assert a["resolved_by"] == uid
    assert calls == [(ws, o["id"])]
    assert conn.execute("SELECT execution_paused FROM procure.buyer_accounts WHERE id = %s",
                        (acc["id"],)).fetchone()["execution_paused"] is True
    # 同一真实单号不能再登记到另一笔任务
    acc2 = f.make_account(conn, ws)
    o2, _ = _unknown_order(conn, ws, acc2, simulated=False)
    with pytest.raises(DomainError) as e:
        orders.resolve_unknown(conn, ws, o2["id"], "ordered", amazon_order_no="112-0000000-0000001",
                               actor_user_id=uid)
    assert e.value.code == "VALIDATION"


def test_resolve_unknown_not_ordered_frees_quota_keeps_assignment_and_pause(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    o, att = _unknown_order(conn, ws, acc)
    assert quota.used(conn, acc["id"], NOW.date(), True) == 1
    d = orders.resolve_unknown(conn, ws, o["id"], "not_ordered", note="亚马逊没有该订单", actor_user_id=uid)
    assert d["status"] == "assigned" and d["assigned_account"]["id"] == acc["id"]
    a = conn.execute("SELECT * FROM procure.purchase_attempts WHERE id = %s", (att["id"],)).fetchone()
    assert a["status"] == "failed" and a["resolution"] == "not_ordered"
    assert quota.used(conn, acc["id"], NOW.date(), True) == 0
    assert conn.execute("SELECT execution_paused FROM procure.buyer_accounts WHERE id = %s",
                        (acc["id"],)).fetchone()["execution_paused"] is True


def test_resolve_unknown_not_ordered_with_upstream_cancel_goes_cancelled(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    o, _ = _unknown_order(conn, ws, acc)
    conn.execute("UPDATE procure.purchase_orders SET upstream_cancelled = true WHERE id = %s", (o["id"],))
    d = orders.resolve_unknown(conn, ws, o["id"], "not_ordered", actor_user_id=uid)
    assert d["status"] == "cancelled"


def test_resolve_unknown_refused_while_attempt_in_flight(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws)
    f.make_attempt(conn, ws, o["id"], acc["id"], status="submitting", submitted=True)
    f.set_status(conn, o["id"], "result_unknown", assigned_account_id=acc["id"], assignment_kind="auto")
    with pytest.raises(DomainError) as e:
        orders.resolve_unknown(conn, ws, o["id"], "not_ordered", actor_user_id=uid)
    assert e.value.code == "ATTEMPT_IN_FLIGHT"


# ── 亚马逊取消与重采 ──────────────────────────────────────────────────────────


def test_amazon_cancel_then_repurchase_approve_and_decline(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws)
    att = f.make_attempt(conn, ws, o["id"], acc["id"], status="succeeded", amazon_order_no="SIM-20261031-1")
    f.set_status(conn, o["id"], "purchased", assigned_account_id=acc["id"], assignment_kind="auto",
                 purchased_attempt_id=att["id"])
    d = orders.mark_amazon_cancelled(conn, ws, o["id"], "亚马逊缺货取消", uid)
    assert d["status"] == "repurchase_pending" and d["allowed_actions"] == ["repurchase"]
    d = orders.decide_repurchase(conn, ws, o["id"], "approve", "同意重采", uid)
    assert d["status"] == "queued" and d["assigned_account"] is None
    assert orders.next_attempt_kind(conn, orders.get_row(conn, ws, o["id"])) == "repurchase"
    o2 = f.ingest_ready_order(conn, ws)
    f.set_status(conn, o2["id"], "repurchase_pending")
    assert orders.decide_repurchase(conn, ws, o2["id"], "decline", None, uid)["status"] == "closed"


def test_repurchase_of_external_purchase_is_not_reverted_by_reimport(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    rec = f.make_record(upstream_order_no="PO-DEMO-RP", external_amz_order_no="111-0000000-0000077")
    o = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2))
    orders.mark_amazon_cancelled(conn, ws, o.order_id, None, uid)
    orders.decide_repurchase(conn, ws, o.order_id, "approve", None, uid)
    again = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2))
    assert again.verdict == "unchanged" and orders.get_row(conn, ws, o.order_id)["status"] == "queued"


def test_next_attempt_no_and_kind(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws)
    assert orders.next_attempt_no(conn, o["id"]) == 1 and orders.next_attempt_kind(conn, o) == "auto"
    f.make_attempt(conn, ws, o["id"], acc["id"], status="failed")
    assert orders.next_attempt_no(conn, o["id"], workspace_id=ws) == 2
    assert orders.next_attempt_kind(conn, o) == "retry"


# ── 冲突处理 ──────────────────────────────────────────────────────────────────


def _conflicted(conn, ws):
    rec = f.make_record(upstream_order_no="PO-DEMO-CF")
    orders.ingest(conn, ws, rec, f.feishu_source(conn, ws, "recCF"))
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-CF", price_cap=Decimal("55.00")),
                        f.file_source(conn, ws, 2))
    assert out.status == "conflict"
    return out.order_id


def test_resolve_conflict_keep(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    oid = _conflicted(conn, ws)
    assert "resolve_conflict" in orders.get_detail(conn, ws, oid)["allowed_actions"]
    d = orders.resolve_conflict(conn, ws, oid, "keep", "以飞书为准", uid)
    assert d["status"] == "queued" and d["price_cap"] == "40.00" and d["conflict"] is None
    with pytest.raises(DomainError):
        orders.resolve_conflict(conn, ws, oid, "keep", None, uid)


def test_resolve_conflict_accept_applies_incoming_values(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    oid = _conflicted(conn, ws)
    d = orders.resolve_conflict(conn, ws, oid, "accept", None, uid)
    assert d["status"] == "queued" and d["price_cap"] == "55.00"
    # 飞书记录没有变化 → 不会把接受的值改回去
    again = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-CF"), f.feishu_source(conn, ws, "recCF"))
    assert again.verdict == "unchanged" and orders.get_row(conn, ws, oid)["price_cap"] == Decimal("55.00")


# ── 模拟回退 ──────────────────────────────────────────────────────────────────


def test_revert_simulation_returns_to_derived_state(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws)
    att = f.make_attempt(conn, ws, o["id"], acc["id"], status="aborted", simulated=True)
    f.set_status(conn, o["id"], "purchased", assigned_account_id=acc["id"], assignment_kind="auto",
                 purchased_attempt_id=att["id"])
    row = orders.revert_simulation(conn, ws, o["id"], actor_kind="system")
    assert row["status"] == "assigned" and row["purchased_attempt_id"] is None
    o2 = f.ingest_ready_order(conn, ws)
    f.make_attempt(conn, ws, o2["id"], acc["id"], status="succeeded", simulated=False, amazon_order_no="113-1")
    f.set_status(conn, o2["id"], "purchased")
    with pytest.raises(DomainError):
        orders.revert_simulation(conn, ws, o2["id"], actor_kind="system")


# ── 跨工作区 ──────────────────────────────────────────────────────────────────


def test_cross_workspace_order_access_is_404(conn, workspace):
    """v1 §10：甲访问乙的任务 → 服务端拒绝（404，不泄露存在性）。"""
    ws, uid = workspace["id"], workspace["user_id"]
    other = f.make_workspace(conn)
    theirs = f.ingest_ready_order(conn, other["id"])
    their_acc = f.make_account(conn, other["id"])
    mine = f.ingest_ready_order(conn, ws)
    calls = [
        lambda: orders.get_row(conn, ws, theirs["id"]),
        lambda: orders.get_detail(conn, ws, theirs["id"]),
        lambda: orders.review(conn, ws, theirs["id"], "approve", None, uid),
        lambda: orders.cancel(conn, ws, theirs["id"], None, uid),
        lambda: orders.set_override(conn, ws, theirs["id"], price_cap="10", actor_user_id=uid),
        lambda: orders.assign_manual(conn, ws, mine["id"], their_acc["id"], uid),
    ]
    for call in calls:
        with pytest.raises(DomainError) as e:
            call()
        assert e.value.code == "NOT_FOUND" and e.value.http_status == 404
    assert orders.list_orders(conn, ws)["total"] == 1
