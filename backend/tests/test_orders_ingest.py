"""来源接收（orders.ingest）：v1 §10 / v2 §4 验收条目与契约 §2「来源更新」各规则。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from services import allocation, clock, orders
from tests import factories as f

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _order(conn, ws, no):
    return conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = %s",
                        (ws, no)).fetchone()


def _events(conn, order_id, kind=None):
    rows = conn.execute("SELECT * FROM procure.events WHERE order_id = %s ORDER BY id", (order_id,)).fetchall()
    return [r for r in rows if kind is None or r["kind"] == kind]


# ── 去重与来源 ────────────────────────────────────────────────────────────────


def test_duplicate_ingest_of_same_approved_feishu_record_gives_one_task(conn, workspace):
    """v1 §10：同一审核通过记录重复导入 → 始终只有一笔采购任务。"""
    ws = workspace["id"]
    rec = f.make_record(upstream_order_no="PO-DEMO-DUP-1")
    src = f.feishu_source(conn, ws, "recDEMO001")
    first = orders.ingest(conn, ws, rec, src)
    second = orders.ingest(conn, ws, rec, src)
    assert first.verdict == "new" and first.status == "queued"
    assert second.verdict == "unchanged" and second.order_id == first.order_id
    assert f.count(conn, "purchase_orders", "workspace_id = %s", (ws,)) == 1
    assert f.count(conn, "order_sources", "purchase_order_id = %s", (first.order_id,)) == 1


def test_duplicate_file_uploads_in_new_batches_give_one_task(conn, workspace):
    """v1 §10：同一记录在两个上传批次里 → 一笔任务；第二批 unchanged，只追加来源关联。"""
    ws = workspace["id"]
    rec = f.make_record(upstream_order_no="PO-DEMO-DUP-2")
    a = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2))
    b = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 5))
    assert a.verdict == "new" and b.verdict == "unchanged" and a.order_id == b.order_id
    assert f.count(conn, "purchase_orders") == 1
    assert f.count(conn, "order_sources", "purchase_order_id = %s", (a.order_id,)) == 2


def test_file_only_workspace_works_without_feishu(conn, workspace):
    """v1 §10：没有飞书连接只上传文件 → 可导入、查看；没有写回目标。"""
    ws = workspace["id"]
    assert f.count(conn, "feishu_connections") == 0
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-FILE-1"), f.file_source(conn, ws, 2))
    assert out.verdict == "new"
    row = orders.get_row(conn, ws, out.order_id)
    assert row["primary_source_kind"] == "file" and row["status"] == "queued"
    src = conn.execute("SELECT * FROM procure.order_sources WHERE purchase_order_id = %s", (row["id"],)).fetchone()
    assert src["is_writeback_target"] is False
    listed = orders.list_orders(conn, ws, view="unassigned")
    assert listed["total"] == 1 and listed["rows"][0]["source_kinds"] == ["file"]


def test_first_feishu_source_becomes_writeback_target(conn, workspace):
    ws = workspace["id"]
    rec = f.make_record(upstream_order_no="PO-DEMO-WB-1")
    out = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2))
    orders.ingest(conn, ws, rec, f.feishu_source(conn, ws, "recDEMOWB1"))
    srcs = conn.execute("SELECT kind, is_writeback_target FROM procure.order_sources WHERE purchase_order_id = %s "
                        "ORDER BY id", (out.order_id,)).fetchall()
    assert [(s["kind"], s["is_writeback_target"]) for s in srcs] == [("file", False), ("feishu", True)]


def test_feishu_then_file_same_key_is_one_task(conn, workspace):
    """v1 §10：先飞书后文件同键 → 同一任务，内容相同只追加来源关联。"""
    ws = workspace["id"]
    rec = f.make_record(upstream_order_no="PO-DEMO-X-1")
    a = orders.ingest(conn, ws, rec, f.feishu_source(conn, ws, "recDEMOX1"))
    b = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 3))
    assert b.verdict == "unchanged" and b.order_id == a.order_id
    row = orders.get_row(conn, ws, a.order_id)
    assert row["primary_source_kind"] == "feishu" and row["status"] == "queued"
    assert orders.list_orders(conn, ws)["rows"][0]["source_kinds"] == ["feishu", "file"]


def test_feishu_then_file_with_conflicting_content_goes_conflict_without_overwrite(conn, workspace):
    """v1 §10：内容冲突提示核实，不改写执行商品 / 限价。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws, name="环境-DEMO-A")
    a = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-X-2"), f.feishu_source(conn, ws, "recX2"))
    allocation.allocate(conn, ws, trigger="manual")
    assert orders.get_row(conn, ws, a.order_id)["assigned_account_id"] == acc["id"]
    clash = f.make_record(upstream_order_no="PO-DEMO-X-2", asin="B0DEMO0099", price_cap=Decimal("99.00"))
    preview = orders.ingest(conn, ws, clash, f.file_source(conn, ws, 2), dry_run=True)
    assert preview.verdict == "conflict" and preview.status == "conflict"
    out = orders.ingest(conn, ws, clash, f.file_source(conn, ws, 2))
    assert out.verdict == "conflict" and out.order_id == a.order_id
    assert set(out.changes) == {"asin", "price_cap"}
    row = orders.get_row(conn, ws, a.order_id)
    assert row["status"] == "conflict" and row["assigned_account_id"] is None
    assert row["asin"] == "B0DEMO0001" and row["price_cap"] == Decimal("40.00")
    assert row["conflict"]["incoming"] == {"asin": "B0DEMO0099", "price_cap": "99.00"}
    assert _events(conn, a.order_id, "order.source_conflict")
    # 同一文件行再次到来（指纹不变）→ unchanged，不重复提示
    again = orders.ingest(conn, ws, clash, f.file_source(conn, ws, 2, batch_id=conn.execute(
        "SELECT import_batch_id FROM procure.order_sources WHERE kind = 'file'").fetchone()["import_batch_id"]))
    assert again.verdict == "unchanged"


def test_record_key_change_for_same_feishu_record_is_conflict(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recKEY")
    a = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-K-1"), src)
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-K-2"), src)
    assert out.verdict == "conflict" and out.order_id == a.order_id
    assert f.count(conn, "purchase_orders") == 1
    assert orders.get_row(conn, ws, a.order_id)["needs_attention"] is True


def test_missing_order_no_is_invalid(conn, workspace):
    ws = workspace["id"]
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="   "), f.file_source(conn, ws, 2))
    assert out.verdict == "invalid" and out.order_id is None
    assert f.count(conn, "purchase_orders") == 0


# ── 人审、资料、外部单号 ──────────────────────────────────────────────────────


def test_missing_review_stays_pending_review_and_is_never_assigned(conn, workspace):
    """v1 §10：文件缺少人审依据 → 不下发（待确认，不分配）。"""
    ws = workspace["id"]
    f.make_account(conn, ws)
    out = orders.ingest(conn, ws, f.make_record(review_value=None, review="unknown"), f.file_source(conn, ws, 2))
    assert out.verdict == "new" and out.status == "pending_review"
    assert any("待确认" in i for i in out.issues)
    summary = allocation.allocate(conn, ws, trigger="import")
    assert summary["stats"]["considered"] == 0
    assert orders.get_row(conn, ws, out.order_id)["assigned_account_id"] is None


def test_rejected_review_is_pending_review_with_reason(conn, workspace):
    ws = workspace["id"]
    out = orders.ingest(conn, ws, f.make_record(review_value="拒绝", review="rejected"), f.file_source(conn, ws, 2))
    row = orders.get_row(conn, ws, out.order_id)
    assert row["status"] == "pending_review" and "拒绝" in row["status_reason"]


def test_missing_fields_go_to_needs_info_and_null_is_not_zero(conn, workspace):
    ws = workspace["id"]
    out = orders.ingest(conn, ws, f.make_record(price_cap=None, phone=None, unit_price=None),
                        f.file_source(conn, ws, 2))
    row = orders.get_row(conn, ws, out.order_id)
    assert row["status"] == "needs_info"
    assert row["missing_fields"] == ["采购单价", "限价", "收货人电话"]
    assert row["price_cap"] is None and row["unit_price"] is None


def test_existing_amz_order_no_becomes_purchased_external_not_queued(conn, workspace):
    """v1 §10：已存在亚马逊订单号 → 历史采购，不重新下单。"""
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-HIST")
    rec = f.make_record(upstream_order_no="PO-DEMO-H-1", external_amz_order_no="111-0000000-0000001",
                        external_buyer_account="环境-DEMO-HIST")
    preview = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2), dry_run=True)
    assert preview.verdict == "historical" and preview.status == "purchased"
    out = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2))
    assert out.verdict == "historical"
    row = orders.get_row(conn, ws, out.order_id)
    assert row["status"] == "purchased" and row["assigned_account_id"] is None
    att = conn.execute("SELECT * FROM procure.purchase_attempts WHERE purchase_order_id = %s", (row["id"],)).fetchone()
    assert att["kind"] == "external" and att["status"] == "succeeded" and not att["simulated"]
    assert att["amazon_order_no"] == "111-0000000-0000001" and att["buyer_account_name"] == "环境-DEMO-HIST"
    assert row["purchased_attempt_id"] == att["id"]
    assert allocation.allocate(conn, ws, trigger="import")["stats"]["considered"] == 0


def test_same_amz_order_no_on_another_task_is_conflict(conn, workspace):
    ws = workspace["id"]
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-H-2", external_amz_order_no="111-0000000-0000002"),
                  f.file_source(conn, ws, 2))
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-H-3",
                                                external_amz_order_no="111-0000000-0000002"),
                        f.file_source(conn, ws, 3))
    assert out.verdict == "conflict" and out.status == "conflict"
    row = orders.get_row(conn, ws, out.order_id)
    assert row["external_amz_order_no"] is None and row["conflict"]["kind"] == "external_order_no"


def test_amz_order_no_arriving_for_queued_task_marks_purchased(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recH4")
    a = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-H-4"), src)
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-H-4",
                                                external_amz_order_no="111-0000000-0000004"), src)
    assert out.verdict == "historical" and out.status == "purchased"
    assert orders.get_row(conn, ws, a.order_id)["external_amz_order_no"] == "111-0000000-0000004"


def test_amz_order_no_while_executing_is_conflict_and_attention(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recH5")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-H-5")
    f.set_status(conn, order["id"], "executing", assigned_account_id=acc["id"], assignment_kind="auto")
    f.make_attempt(conn, ws, order["id"], acc["id"], status="checking")
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-H-5",
                                                external_amz_order_no="111-0000000-0000005"), src)
    assert out.verdict == "conflict" and out.status == "executing"
    row = orders.get_row(conn, ws, order["id"])
    assert row["needs_attention"] and "AMZ 订单号" in row["attention_note"]


def test_different_amz_order_no_on_purchased_task_is_conflict(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recH6")
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-H-6", external_amz_order_no="111-0000000-0000006"),
                  src)
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-H-6",
                                                external_amz_order_no="111-0000000-0000066"), src)
    assert out.verdict == "conflict" and out.status == "purchased"


# ── 建议采购日期 ──────────────────────────────────────────────────────────────


def test_computed_suggested_date_uses_beijing_order_date(conn, workspace):
    """方案 v2 Q5：2026-10-23 17:00 UTC 是北京 10-24；配送 4 天 → 建议 10-26（依据 computed）。"""
    ws = workspace["id"]
    rec = f.make_record(order_time=datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc), ship_days=4,
                        suggested_purchase_date=None)
    out = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2))
    row = orders.get_row(conn, ws, out.order_id)
    assert row["order_date_bj"] == date(2026, 10, 24)
    assert row["suggested_purchase_date"] == date(2026, 10, 26)
    assert row["suggested_date_basis"] == "computed"


def test_source_suggested_date_wins_over_computed(conn, workspace):
    ws = workspace["id"]
    rec = f.make_record(order_time=datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc), ship_days=4,
                        suggested_purchase_date=date(2026, 10, 29))
    row = orders.get_row(conn, ws, orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2)).order_id)
    assert row["suggested_purchase_date"] == date(2026, 10, 29) and row["suggested_date_basis"] == "source"


def test_missing_order_time_or_ship_days_means_needs_info(conn, workspace):
    ws = workspace["id"]
    rec = f.make_record(order_time=None, ship_days=4, suggested_purchase_date=None)
    row = orders.get_row(conn, ws, orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2)).order_id)
    assert row["status"] == "needs_info" and "建议采购日期" in row["missing_fields"]


def test_ship_days_change_recomputes_suggested_date(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recSD")
    base = dict(upstream_order_no="PO-DEMO-SD", order_time=datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc),
                suggested_purchase_date=None)
    a = orders.ingest(conn, ws, f.make_record(ship_days=4, **base), src)
    out = orders.ingest(conn, ws, f.make_record(ship_days=2, **base), src)
    assert out.verdict == "update"
    assert orders.get_row(conn, ws, a.order_id)["suggested_purchase_date"] == date(2026, 10, 28)


# ── 开始前的来源更新 ──────────────────────────────────────────────────────────


def test_override_kept_when_source_changes_original_updated_and_event_written(conn, workspace):
    """v1 §10：控制台修改限价后再次导入 → 覆盖值保留，原值更新并记事件。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recOV")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-OV")
    orders.set_override(conn, ws, order["id"], price_cap="60.00", note="DEMO 调价", actor_user_id=workspace["user_id"])
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-OV", price_cap=Decimal("45.00")), src)
    assert out.verdict == "update" and out.changes == {"price_cap": ["40.00", "45.00"]}
    row = orders.get_row(conn, ws, order["id"])
    assert row["price_cap"] == Decimal("45.00") and row["override_price_cap"] == Decimal("60.00")
    assert orders.price_cap_eff(row) == Decimal("60.00")
    ev = _events(conn, order["id"], "order.source_updated")[-1]
    assert ev["detail"]["changes"] == {"price_cap": ["40.00", "45.00"]}
    assert ev["detail"]["overrides_kept"] == ["price_cap"]


def test_review_revoked_before_execution_returns_to_pending_review_and_unassigns(conn, workspace):
    ws = workspace["id"]
    f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recRV")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-RV")
    allocation.allocate(conn, ws, trigger="intake")
    assert orders.get_row(conn, ws, order["id"])["status"] == "assigned"
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-RV", review_value="拒绝",
                                                review="rejected"), src)
    assert out.verdict == "update" and out.status == "pending_review"
    row = orders.get_row(conn, ws, order["id"])
    assert row["assigned_account_id"] is None and row["assignment_kind"] is None
    assert _events(conn, order["id"], "order.status_changed")[-1]["detail"]["to"] == "pending_review"


def test_file_reupload_without_review_column_does_not_revoke_review(conn, workspace):
    """None 表示没读到：新批次文件没有人审列，不撤销已通过的人审。"""
    ws = workspace["id"]
    order = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-RV2")
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-RV2", review_value=None, review="unknown"),
                        f.file_source(conn, ws, 2))
    assert out.verdict == "unchanged"
    assert orders.get_row(conn, ws, order["id"])["status"] == "queued"


def test_amount_change_out_of_account_range_unassigns(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws, min_amount="20", max_amount="30")
    src = f.feishu_source(conn, ws, "recAMT")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-AMT")
    allocation.allocate(conn, ws, trigger="intake")
    assert orders.get_row(conn, ws, order["id"])["assigned_account_id"] == acc["id"]
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-AMT", unit_price=Decimal("35.00")), src)
    assert out.status == "queued"
    row = orders.get_row(conn, ws, order["id"])
    assert row["assigned_account_id"] is None and "不在账号" in row["status_reason"]


def test_address_group_replaced_as_a_whole(conn, workspace):
    ws = workspace["id"]
    order = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-ADR", address2="Apt 5")
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-ADR", address1="200 Demo Avenue", address2=None),
                  f.file_source(conn, ws, 2))
    row = orders.get_row(conn, ws, order["id"])
    assert row["address1"] == "200 Demo Avenue" and row["address2"] is None


# ── 开始后的来源更新 ──────────────────────────────────────────────────────────


def test_execution_fields_changed_after_start_only_set_attention(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recAFT")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-AFT")
    f.set_status(conn, order["id"], "purchased", assigned_account_id=acc["id"], assignment_kind="auto")
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-AFT", asin="B0DEMO0002",
                                                store_name="STORE-DEMO-2"), src)
    assert out.verdict == "conflict" and out.status == "purchased"
    row = orders.get_row(conn, ws, order["id"])
    assert row["asin"] == "B0DEMO0001" and row["store_name"] == "STORE-DEMO-2"
    assert row["needs_attention"] and "ASIN" in row["attention_note"]
    assert _events(conn, order["id"], "order.upstream_changed_after_start")


# ── 上游取消（各状态）─────────────────────────────────────────────────────────


def _cancelled(no):
    return f.make_record(upstream_order_no=no, after_sale_value="已取消", upstream_cancelled=True)


def test_upstream_cancel_before_execution_cancels_and_unassigns(conn, workspace):
    """v1 §5 / §10：采购前沃尔玛取消 → 移出队列并解除分配。"""
    ws = workspace["id"]
    f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recC1")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-C1")
    allocation.allocate(conn, ws, trigger="intake")
    out = orders.ingest(conn, ws, _cancelled("PO-DEMO-C1"), src)
    assert out.verdict == "update" and out.status == "cancelled"
    row = orders.get_row(conn, ws, order["id"])
    assert row["upstream_cancelled"] and row["assigned_account_id"] is None
    assert row["upstream_cancel_note"] == "已取消"


def test_upstream_cancel_in_exception_cancels(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recC2")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-C2")
    f.set_status(conn, order["id"], "exception", status_reason="商品无库存")
    assert orders.ingest(conn, ws, _cancelled("PO-DEMO-C2"), src).status == "cancelled"


def test_upstream_cancel_while_executing_not_submitted_sets_flag(conn, workspace):
    """执行中且未登记提交 → 置取消标记（插件下次进度上报得到 continue=false），任务仍在执行中。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recC3")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-C3")
    f.set_status(conn, order["id"], "executing", assigned_account_id=acc["id"], assignment_kind="auto")
    f.make_attempt(conn, ws, order["id"], acc["id"], status="checking")
    out = orders.ingest(conn, ws, _cancelled("PO-DEMO-C3"), src)
    assert out.status == "executing"
    row = orders.get_row(conn, ws, order["id"])
    assert row["upstream_cancelled"] is True and row["needs_attention"]


def test_upstream_cancel_while_executing_after_submit_goes_result_unknown(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recC4")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-C4")
    f.set_status(conn, order["id"], "executing", assigned_account_id=acc["id"], assignment_kind="auto")
    f.make_attempt(conn, ws, order["id"], acc["id"], status="submitting", submitted=True)
    out = orders.ingest(conn, ws, _cancelled("PO-DEMO-C4"), src)
    assert out.status == "result_unknown"
    row = orders.get_row(conn, ws, order["id"])
    assert "执行中收到取消" in row["status_reason"] and row["upstream_cancelled"]
    assert row["assigned_account_id"] == acc["id"]


def test_upstream_cancel_after_purchase_keeps_purchased_and_flags_attention(conn, workspace):
    """v1 §5：采购成功后沃尔玛取消 → 保持已采购，提醒人工处理亚马逊订单。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recC5")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-C5")
    f.set_status(conn, order["id"], "purchased")
    out = orders.ingest(conn, ws, _cancelled("PO-DEMO-C5"), src)
    assert out.status == "purchased"
    row = orders.get_row(conn, ws, order["id"])
    assert row["upstream_cancelled"] and row["needs_attention"] and "亚马逊订单" in row["attention_note"]


def test_upstream_cancel_in_result_unknown_flags_only(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recC6")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-C6")
    f.set_status(conn, order["id"], "result_unknown")
    assert orders.ingest(conn, ws, _cancelled("PO-DEMO-C6"), src).status == "result_unknown"
    assert orders.get_row(conn, ws, order["id"])["upstream_cancelled"] is True


def test_upstream_uncancel_does_not_revive_but_allows_manual_uncancel(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recC7")
    order = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-C7")
    orders.ingest(conn, ws, _cancelled("PO-DEMO-C7"), src)
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-C7", after_sale_value="无"), src)
    assert out.status == "cancelled"
    row = orders.get_row(conn, ws, order["id"])
    assert row["upstream_cancelled"] is False and row["needs_attention"]
    detail = orders.uncancel(conn, ws, order["id"], actor_user_id=workspace["user_id"])
    assert detail["status"] == "queued"


# ── 预演 ──────────────────────────────────────────────────────────────────────


def test_dry_run_gives_same_verdicts_without_writing(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recDRY")
    rec = f.make_record(upstream_order_no="PO-DEMO-DRY")
    tables = ("purchase_orders", "order_sources", "events", "purchase_attempts")
    before = {t: f.count(conn, t) for t in tables}
    dry = orders.ingest(conn, ws, rec, src, dry_run=True)
    assert dry.verdict == "new" and dry.order_id is None and dry.status == "queued"
    assert {t: f.count(conn, t) for t in tables} == before
    real = orders.ingest(conn, ws, rec, src)
    assert real.verdict == dry.verdict and real.status == dry.status
    changed = f.make_record(upstream_order_no="PO-DEMO-DRY", quantity=2)
    snap = {t: f.count(conn, t) for t in tables}
    dry2 = orders.ingest(conn, ws, changed, src, dry_run=True)
    assert dry2.verdict == "update" and dry2.changes == {"quantity": [1, 2]}
    assert {t: f.count(conn, t) for t in tables} == snap
    assert orders.get_row(conn, ws, real.order_id)["quantity"] == 1


def test_dry_run_for_unsaved_feishu_connection_does_not_touch_db(conn, workspace):
    """飞书向导预览时连接尚未保存：dry_run 不写库，所以不存在的 connection_id 也不会出错。"""
    ws = workspace["id"]
    from services.models import SourceRef

    src = SourceRef(kind="feishu", snapshot={}, connection_id=999999, record_id="recPREVIEW")
    out = orders.ingest(conn, ws, f.make_record(), src, dry_run=True)
    assert out.verdict == "new"


def test_cross_workspace_same_order_no_are_independent(conn, workspace):
    ws = workspace["id"]
    other = f.make_workspace(conn)
    rec = f.make_record(upstream_order_no="PO-DEMO-SHARED")
    a = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2))
    b = orders.ingest(conn, other["id"], rec, f.file_source(conn, other["id"], 2))
    assert a.verdict == b.verdict == "new" and a.order_id != b.order_id


def test_new_file_batch_with_changed_price_is_a_normal_update(conn, workspace):
    """同种来源（新批次文件）视为正常更新。"""
    ws = workspace["id"]
    order = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-NB")
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-NB", price_cap=Decimal("44.00")),
                        f.file_source(conn, ws, 9))
    assert out.verdict == "update" and out.changes == {"price_cap": ["40.00", "44.00"]}
    assert orders.get_row(conn, ws, order["id"])["price_cap"] == Decimal("44.00")


def test_secondary_source_can_fill_unknown_review(conn, workspace):
    """文件先到但没有人审结论；飞书带「通过」且内容相同 → 补上人审，进入待分配。"""
    ws = workspace["id"]
    order = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-FILL", review_value=None, review="unknown")
    assert order["status"] == "pending_review"
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-FILL"), f.feishu_source(conn, ws, "rFILL"))
    assert out.verdict == "update" and out.status == "queued"


def test_key_changed_record_reminds_once(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recKEY2")
    a = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-K-3"), src)
    for _ in range(3):
        orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-K-4"), src)
    assert len(_events(conn, a.order_id, "order.source_key_changed")) == 1
