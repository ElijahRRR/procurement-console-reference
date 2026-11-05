"""来源接收的跨来源规则（orders.ingest）：人审拒绝不丢、取消只由发出者撤销、准入变化暂停已有任务、平台订单号改了、
不可信的 AMZ 订单号、邮编、建议采购日期溢出、写回目标归属。全部数据虚构。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from services import allocation, clock, orders
from services.errors import DomainError
from tests import factories as f

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
FEISHU_ADM = {"purchaser_values": ["自采购DEMO"]}


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _row(conn, ws, oid):
    return orders.get_row(conn, ws, oid)


def _kinds(conn, oid):
    return [r["kind"] for r in conn.execute("SELECT kind FROM procure.events WHERE order_id = %s ORDER BY id",
                                            (oid,)).fetchall()]


def _executing(conn, ws, oid, acc, *, submitted=False):
    att = f.make_attempt(conn, ws, oid, acc["id"], status="submitting" if submitted else "checking",
                         submitted=submitted)
    f.set_status(conn, oid, "executing", assigned_account_id=acc["id"], assignment_kind="auto",
                 current_attempt_id=att["id"])
    return att


# ── 人审拒绝从不悄悄丢掉（D1-01）─────────────────────────────────────────────


def test_feishu_rejection_of_file_first_assigned_task_goes_conflict_and_unassigns(conn, workspace):
    """文件先到并已分配；同键飞书记录说「钓鱼」→ 待核实并解除分配（不是「无变化」）。"""
    ws = workspace["id"]
    f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-RJ1")
    allocation.allocate(conn, ws, trigger="import")
    assert _row(conn, ws, o["id"])["status"] == "assigned"
    rec = f.make_record(upstream_order_no="PO-DEMO-RJ1", review_value="钓鱼", review="rejected")
    src = f.feishu_source(conn, ws, "recRJ1")
    dry = orders.ingest(conn, ws, rec, src, dry_run=True)
    assert dry.verdict == "conflict" and dry.status == "conflict"
    out = orders.ingest(conn, ws, rec, src)
    assert out.verdict == "conflict" and "钓鱼" in out.reason
    row = _row(conn, ws, o["id"])
    assert row["status"] == "conflict" and row["assigned_account_id"] is None
    assert row["conflict"]["kind"] == "review" and row["conflict"]["incoming"]["review_status"] == "rejected"
    assert "order.source_review_conflict" in _kinds(conn, o["id"])
    # 同一条飞书记录再次读到（没变）→ unchanged，不重复提醒
    assert orders.ingest(conn, ws, rec, src).verdict == "unchanged"


def test_feishu_flip_to_rejection_after_agreeing_goes_conflict(conn, workspace):
    """最常见的情形：文件为主，飞书先同意「通过」，后来人审改成「钓鱼」→ 待核实。"""
    ws = workspace["id"]
    o = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-RJ2")
    src = f.feishu_source(conn, ws, "recRJ2")
    assert orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-RJ2"), src).verdict == "unchanged"
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-RJ2", review_value="钓鱼",
                                                review="rejected"), src)
    assert out.verdict == "conflict" and _row(conn, ws, o["id"])["status"] == "conflict"


def test_stale_file_rejection_after_console_keep_is_not_reapplied(conn, workspace):
    """飞书通过、文件说「拒绝」→ 待核实；人工「保留当前」= 控制台确认通过；同一份旧文件再上传不再转冲突，只说明。"""
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws, source=f.feishu_source(conn, ws, "recRJ3"), upstream_order_no="PO-DEMO-RJ3")
    rej = f.make_record(upstream_order_no="PO-DEMO-RJ3", review_value="拒绝", review="rejected")
    out = orders.ingest(conn, ws, rej, f.file_source(conn, ws, 2))
    assert out.verdict == "conflict" and _row(conn, ws, o["id"])["status"] == "conflict"
    d = orders.resolve_conflict(conn, ws, o["id"], "keep", "核对过，确实通过", uid)
    assert d["status"] == "queued" and d["review_source"] == "console" and d["conflict"] is None
    clock.freeze(datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc))
    again = orders.ingest(conn, ws, rej, f.file_source(conn, ws, 2))
    assert again.verdict == "unchanged" and _row(conn, ws, o["id"])["status"] == "queued"
    assert any("控制台已确认通过" in i for i in again.issues)


def test_new_file_batch_rejection_after_console_approval_goes_conflict(conn, workspace):
    """文件行没有人审 → 控制台通过；新批次文件说「钓鱼」→ 待核实（以前被当成「无变化」）。"""
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-RJ4", review_value=None, review="unknown")
    orders.review(conn, ws, o["id"], "approve", None, uid)
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-RJ4", review_value="钓鱼",
                                                review="rejected"), f.file_source(conn, ws, 2))
    assert out.verdict == "conflict" and _row(conn, ws, o["id"])["status"] == "conflict"
    d = orders.resolve_conflict(conn, ws, o["id"], "accept", None, uid)
    assert d["status"] == "pending_review" and d["review_status"] == "rejected" and d["review_source"] == "file"


def test_rejection_while_executing_holds_task_for_plugin_or_flags_after_submit(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    a = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-RJ5")
    _executing(conn, ws, a["id"], acc)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-RJ5", review_value="钓鱼", review="rejected"),
                  f.feishu_source(conn, ws, "recRJ5"))
    row = _row(conn, ws, a["id"])
    assert row["status"] == "executing" and row["conflict"]["kind"] == "review" and row["needs_attention"]
    assert "插件将中止" in row["attention_note"]
    b = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-RJ6")
    _executing(conn, ws, b["id"], f.make_account(conn, ws), submitted=True)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-RJ6", review_value="钓鱼", review="rejected"),
                  f.feishu_source(conn, ws, "recRJ6"))
    row = _row(conn, ws, b["id"])
    assert row["status"] == "executing" and row["conflict"] is None and row["needs_attention"]


def test_secondary_approval_on_rejected_task_is_reported_not_applied(conn, workspace):
    ws = workspace["id"]
    o = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-RJ7", review_value="拒绝", review="rejected")
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-RJ7"), f.feishu_source(conn, ws, "rRJ7"))
    assert _row(conn, ws, o["id"])["review_status"] == "rejected"
    assert any("未应用" in i for i in out.issues)


# ── 上游取消只由发出取消的来源撤销（D1-02）────────────────────────────────────


def _cancelled(no):
    return f.make_record(upstream_order_no=no, after_sale_value="已取消", upstream_cancelled=True)


def test_file_snapshot_never_lifts_feishu_cancellation_while_executing(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recUC1")
    o = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-UC1")
    _executing(conn, ws, o["id"], acc)
    orders.ingest(conn, ws, _cancelled("PO-DEMO-UC1"), src)
    assert _row(conn, ws, o["id"])["upstream_cancelled"] is True
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-UC1", after_sale_value="正常"),
                        f.file_source(conn, ws, 2))
    row = _row(conn, ws, o["id"])
    assert row["upstream_cancelled"] is True and row["status"] == "executing"
    assert "order.upstream_uncancelled" not in _kinds(conn, o["id"])
    assert any("未撤销" in i for i in out.issues)


def test_file_snapshot_does_not_lift_cancellation_in_result_unknown_or_cancelled(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    src = f.feishu_source(conn, ws, "recUC2")
    o = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-UC2")
    f.set_status(conn, o["id"], "result_unknown")
    orders.ingest(conn, ws, _cancelled("PO-DEMO-UC2"), src)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-UC2", after_sale_value="正常"),
                  f.file_source(conn, ws, 2))
    assert _row(conn, ws, o["id"])["upstream_cancelled"] is True
    src3 = f.feishu_source(conn, ws, "recUC3")
    c = f.ingest_ready_order(conn, ws, source=src3, upstream_order_no="PO-DEMO-UC3")
    orders.ingest(conn, ws, _cancelled("PO-DEMO-UC3"), src3)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-UC3", after_sale_value="正常"),
                  f.file_source(conn, ws, 2))
    row = _row(conn, ws, c["id"])
    assert row["status"] == "cancelled" and row["upstream_cancelled"] is True
    assert "上游撤销了取消" not in (row["attention_note"] or "")
    with pytest.raises(DomainError):
        orders.uncancel(conn, ws, c["id"], actor_user_id=uid)


def test_uncancel_waits_until_no_source_still_says_cancelled(conn, workspace):
    """飞书与文件都说取消；飞书撤销、文件仍是取消快照 → 不撤销。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recUC4")
    o = f.ingest_ready_order(conn, ws, source=src, upstream_order_no="PO-DEMO-UC4")
    orders.ingest(conn, ws, _cancelled("PO-DEMO-UC4"), src)
    orders.ingest(conn, ws, _cancelled("PO-DEMO-UC4"), f.file_source(conn, ws, 2))
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-UC4", after_sale_value="正常"), src)
    assert _row(conn, ws, o["id"])["upstream_cancelled"] is True


# ── 准入变化暂停已有任务（D4-01 / D1-03）──────────────────────────────────────


def test_purchase_status_bought_elsewhere_pauses_assigned_task(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recAD1")
    rec = f.make_record(upstream_order_no="PO-DEMO-AD1", purchaser="自采购DEMO")
    o = orders.ingest(conn, ws, rec, src, admission=FEISHU_ADM)
    allocation.allocate(conn, ws, trigger="intake")
    assert _row(conn, ws, o.order_id)["status"] == "assigned"
    bought = f.make_record(upstream_order_no="PO-DEMO-AD1", purchaser="自采购DEMO", purchase_status_value="已拍单")
    out = orders.ingest(conn, ws, bought, src, admission=FEISHU_ADM)
    assert out.verdict == "conflict" and "已拍单" in out.reason
    row = _row(conn, ws, o.order_id)
    assert row["status"] == "conflict" and row["assigned_account_id"] is None and row["conflict"]["kind"] == "admission"
    # 同一条记录再来（值没变）→ 不再重复暂停；人工保留 → 回到可执行
    assert orders.ingest(conn, ws, bought, src, admission=FEISHU_ADM).verdict == "unchanged"
    d = orders.resolve_conflict(conn, ws, o.order_id, "keep", None, uid)
    assert d["status"] == "queued"
    assert orders.ingest(conn, ws, bought, src, admission=FEISHU_ADM).verdict == "unchanged"
    assert _row(conn, ws, o.order_id)["status"] == "queued"


def test_purchaser_moved_away_pauses_and_accept_cancels(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    src = f.feishu_source(conn, ws, "recAD2")
    o = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-AD2", purchaser="自采购DEMO"), src,
                      admission=FEISHU_ADM)
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-AD2", purchaser="外采购DEMO"), src,
                        admission=FEISHU_ADM)
    assert out.verdict == "conflict" and "外采购DEMO" in out.reason
    d = orders.resolve_conflict(conn, ws, o.order_id, "accept", None, uid)
    assert d["status"] == "cancelled" and "不再采购" in d["status_reason"] and d["upstream_cancelled"] is False


def test_own_writeback_bought_status_never_pauses(conn, workspace):
    """本系统采购成功后写回「已拍单」：已采购 / 同意重采回到队列的任务都不因此暂停。"""
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recAD3")
    o = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-AD3", purchaser="自采购DEMO"), src,
                      admission=FEISHU_ADM)
    att = f.make_attempt(conn, ws, o.order_id, acc["id"], status="succeeded", amazon_order_no="SIM-20261031-1")
    f.set_status(conn, o.order_id, "purchased", purchased_attempt_id=att["id"])
    bought = f.make_record(upstream_order_no="PO-DEMO-AD3", purchaser="自采购DEMO", purchase_status_value="已拍单")
    assert orders.ingest(conn, ws, bought, src, admission=FEISHU_ADM).status == "purchased"
    orders.mark_amazon_cancelled(conn, ws, o.order_id, None, uid)
    orders.decide_repurchase(conn, ws, o.order_id, "approve", None, uid)
    again = f.make_record(upstream_order_no="PO-DEMO-AD3", purchaser="自采购DEMO", purchase_status_value="拿回")
    out = orders.ingest(conn, ws, again, src, admission=FEISHU_ADM)
    assert out.verdict != "conflict" and _row(conn, ws, o.order_id)["status"] == "queued"


def test_purchase_status_while_executing_holds_and_after_start_ignored(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recAD4")
    o = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-AD4", purchaser="自采购DEMO"), src,
                      admission=FEISHU_ADM)
    _executing(conn, ws, o.order_id, acc)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-AD4", purchaser="自采购DEMO",
                                          purchase_status_value="已拍单"), src, admission=FEISHU_ADM)
    row = _row(conn, ws, o.order_id)
    assert row["status"] == "executing" and row["conflict"]["kind"] == "admission" and row["needs_attention"]


def test_admission_none_means_no_check(conn, workspace):
    """直接调用 ingest 不传准入规则时，不对已有任务做准入检查（只有来源接收流程会传）。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recAD5")
    o = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-AD5"), src)
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-AD5", purchase_status_value="已拍单"), src)
    assert out.verdict != "conflict" and _row(conn, ws, o.order_id)["status"] == "queued"


# ── 平台订单号改了（D4-06）──────────────────────────────────────────────────


def test_key_change_pauses_old_task_and_accept_relinks(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recKC1")
    o = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-KC1"), src)
    allocation.allocate(conn, ws, trigger="intake")
    assert _row(conn, ws, o.order_id)["status"] == "assigned"
    changed = f.make_record(upstream_order_no="PO-DEMO-KC2", recipient_name="Other Demo")
    assert orders.ingest(conn, ws, changed, src, dry_run=True).status == "conflict"
    out = orders.ingest(conn, ws, changed, src)
    row = _row(conn, ws, o.order_id)
    assert out.verdict == "conflict" and row["status"] == "conflict" and row["assigned_account_id"] is None
    assert row["conflict"]["kind"] == "source_key_changed"
    for _ in range(2):          # 定时读取反复送来：只提醒一次
        orders.ingest(conn, ws, changed, src)
    assert _kinds(conn, o.order_id).count("order.source_key_changed") == 1
    d = orders.resolve_conflict(conn, ws, o.order_id, "accept", None, uid)
    assert d["status"] == "cancelled" and "PO-DEMO-KC2" in d["status_reason"] and d["sources"] == []
    new = orders.ingest(conn, ws, changed, src)
    assert new.verdict == "new" and new.order_id != o.order_id


def test_key_change_keep_stays_executable_and_does_not_reflag(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    src = f.feishu_source(conn, ws, "recKC3")
    o = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-KC3"), src)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-KC4"), src)
    assert orders.resolve_conflict(conn, ws, o.order_id, "keep", None, uid)["status"] == "queued"
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-KC4"), src)
    assert _row(conn, ws, o.order_id)["status"] == "queued"


def test_key_change_on_started_task_only_flags(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    src = f.feishu_source(conn, ws, "recKC5")
    o = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-KC5"), src)
    _executing(conn, ws, o.order_id, acc)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-KC6"), src)
    row = _row(conn, ws, o.order_id)
    assert row["status"] == "executing" and row["needs_attention"] and row["conflict"] is None


# ── 不可信的 AMZ 订单号（D4-04）──────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["—", "无", "N/A", "0"])
def test_placeholder_amz_number_is_not_a_historical_purchase(conn, workspace, raw):
    from services.normalize import normalize

    ws = workspace["id"]
    rec = normalize({"upstream_order_no": f"PO-DEMO-PH-{raw}", "external_amz_order_no": raw, "review_value": "通过"})
    assert rec.external_amz_order_no is None and any("不是订单号" in i for i in rec.issues)
    out = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2))
    assert out.verdict == "new" and out.status != "purchased"
    assert f.count(conn, "purchase_attempts", "purchase_order_id = %s", (out.order_id,)) == 0


def test_malformed_amz_number_goes_conflict_and_cannot_be_accepted(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-MF1", external_amz_order_no="缺货待补"),
                        f.file_source(conn, ws, 2))
    assert out.verdict == "conflict" and out.status == "conflict" and "格式不对" in out.reason
    assert f.count(conn, "purchase_attempts", "purchase_order_id = %s", (out.order_id,)) == 0
    with pytest.raises(DomainError) as e:
        orders.resolve_conflict(conn, ws, out.order_id, "accept", None, uid)
    assert e.value.code == "VALIDATION"
    assert orders.resolve_conflict(conn, ws, out.order_id, "keep", None, uid)["status"] == "queued"
    # 已有任务后来出现格式不对的单号：同样待核实，不登记为已采购
    o = f.ingest_ready_order(conn, ws, upstream_order_no="PO-DEMO-MF2")
    out2 = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-MF2", external_amz_order_no="已下单"),
                         f.file_source(conn, ws, 3))
    assert out2.verdict == "conflict" and _row(conn, ws, o["id"])["status"] == "conflict"


def test_valid_amz_number_still_historical(conn, workspace):
    ws = workspace["id"]
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-MF3",
                                                external_amz_order_no="111-0000000-0000031"),
                        f.file_source(conn, ws, 2))
    assert out.verdict == "historical" and out.status == "purchased"


# ── 邮编、建议采购日期（D4-05 / D4-02）──────────────────────────────────────────


def test_short_us_zip_counts_as_missing_field(conn, workspace):
    ws = workspace["id"]
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-ZIP", postal_code="2134"),
                        f.file_source(conn, ws, 2))
    row = _row(conn, ws, out.order_id)
    assert row["status"] == "needs_info" and row["missing_fields"] == ["postal_code"]
    assert "邮编" in row["status_reason"]
    ok = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-ZIP2", postal_code="02134-1234"),
                       f.file_source(conn, ws, 3))
    assert ok.status == "queued"


def test_absurd_ship_days_do_not_overflow_date_math(conn, workspace):
    assert orders.compute_suggested_date(datetime(2026, 10, 27, tzinfo=timezone.utc), 20261031) is None
    assert orders.compute_suggested_date(datetime(9999, 12, 31, tzinfo=timezone.utc), -5) is None
    assert orders.compute_suggested_date(datetime(2026, 10, 27, 5, tzinfo=timezone.utc), 4) == date(2026, 10, 29)
    ws = workspace["id"]
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-OV", suggested_purchase_date=None,
                                                ship_days=20261031), f.file_source(conn, ws, 2))
    assert out.status == "needs_info" and "suggested_purchase_date" in _row(conn, ws, out.order_id)["missing_fields"]


# ── 限价向下取整（覆盖值）──────────────────────────────────────────────────────


def test_override_price_cap_rounds_down(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws)
    d = orders.set_override(conn, ws, o["id"], price_cap="78.685", actor_user_id=uid)
    assert d["price_cap_eff"] == "78.68"
    d = orders.set_override(conn, ws, o["id"], price_cap=78.685, actor_user_id=uid)
    assert d["price_cap_eff"] == "78.68"
    with pytest.raises(DomainError):
        orders.set_override(conn, ws, o["id"], price_cap="1e30", actor_user_id=uid)


def test_record_price_cap_rounds_down_when_ingested_directly(conn, workspace):
    ws = workspace["id"]
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-CAP", price_cap=Decimal("78.685")),
                        f.file_source(conn, ws, 2))
    assert _row(conn, ws, out.order_id)["price_cap"] == Decimal("78.68")


# ── 写回目标（K12）─────────────────────────────────────────────────────────────


def test_set_writeback_target_for_file_primary_task(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    rec = f.make_record(upstream_order_no="PO-DEMO-WT1")
    o = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2))
    orders.ingest(conn, ws, rec, f.feishu_source(conn, ws, "recWT1"))
    d = orders.get_detail(conn, ws, o.order_id)
    assert "set_writeback_target" in d["allowed_actions"]
    feishu_src = next(s for s in d["sources"] if s["kind"] == "feishu")
    file_src = next(s for s in d["sources"] if s["kind"] == "file")
    with pytest.raises(DomainError) as e:
        orders.set_writeback_target(conn, ws, o.order_id, file_src["id"], uid)
    assert e.value.code == "VALIDATION"
    d = orders.set_writeback_target(conn, ws, o.order_id, feishu_src["id"], uid)
    assert [s["is_writeback_target"] for s in d["sources"] if s["kind"] == "feishu"] == [True]
    assert "set_writeback_target" not in d["allowed_actions"]
    assert "order.writeback_target_set" in _kinds(conn, o.order_id)
    other = f.make_workspace(conn)
    with pytest.raises(DomainError) as e:
        orders.set_writeback_target(conn, other["id"], o.order_id, feishu_src["id"], uid)
    assert e.value.code == "NOT_FOUND"
    o2 = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-WT2"), f.file_source(conn, ws, 3))
    with pytest.raises(DomainError) as e:      # 别的任务的来源 → NOT_FOUND
        orders.set_writeback_target(conn, ws, o2.order_id, feishu_src["id"], uid)
    assert e.value.code == "NOT_FOUND"


def test_set_writeback_target_on_purchased_real_result_enqueues(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws)
    rec = f.make_record(upstream_order_no="PO-DEMO-WT3")
    o = orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2))
    src = f.feishu_source(conn, ws, "recWT3")
    orders.ingest(conn, ws, rec, src)
    conn.execute("UPDATE procure.feishu_connections SET writeback_map = %s, writeback_enabled = true "
                 "WHERE id = %s", ('{"amazon_order_no": "AMZ订单号"}', src.connection_id))
    att = f.make_attempt(conn, ws, o.order_id, acc["id"], status="succeeded", simulated=False,
                         amazon_order_no="111-0000000-0000303")
    f.set_status(conn, o.order_id, "purchased", purchased_attempt_id=att["id"], result_version=1)
    sid = next(s["id"] for s in orders.get_detail(conn, ws, o.order_id)["sources"] if s["kind"] == "feishu")
    orders.set_writeback_target(conn, ws, o.order_id, sid, uid)
    wb = conn.execute("SELECT * FROM procure.writebacks WHERE purchase_order_id = %s", (o.order_id,)).fetchall()
    assert len(wb) == 1 and wb[0]["fields"] == {"AMZ订单号": "111-0000000-0000303"}


# ── 其他：清除模拟、needs_attention 过滤、详情里的名字、allowed_actions ────────────────


def test_revert_simulation_resets_retries_for_pre_execution_task(conn, workspace):
    ws = workspace["id"]
    o = f.ingest_ready_order(conn, ws)
    f.set_status(conn, o["id"], "queued", page_load_retries=1)
    row = orders.revert_simulation(conn, ws, o["id"])
    assert row["page_load_retries"] == 0 and row["current_attempt_id"] is None and row["status"] == "queued"


def test_list_orders_needs_attention_filter_counts_total(conn, workspace):
    ws = workspace["id"]
    rows = [f.ingest_ready_order(conn, ws) for _ in range(3)]
    for r in rows[:2]:
        orders._update(conn, ws, r["id"], {"needs_attention": True, "attention_note": "DEMO 提醒"})
    out = orders.list_orders(conn, ws, needs_attention=True, page_size=1)
    assert out["total"] == 2 and len(out["rows"]) == 1 and out["rows"][0]["needs_attention"]
    assert orders.list_orders(conn, ws, needs_attention=False)["total"] == 1
    assert orders.list_orders(conn, ws, view="unassigned", needs_attention=True, q="PO-DEMO")["total"] == 2
    with pytest.raises(DomainError):
        orders.list_orders(conn, ws, needs_attention="yes")


def test_detail_attempts_carry_account_device_and_resolver_names(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws, name="环境-DEMO-N")
    dev = f.make_device(conn, ws, name="Mac-DEMO-N", bound_account_id=acc["id"])
    o = f.ingest_ready_order(conn, ws)
    att = f.make_attempt(conn, ws, o["id"], acc["id"], status="unknown", submitted=True)
    conn.execute("UPDATE procure.purchase_attempts SET device_id = %s WHERE id = %s", (dev["id"], att["id"]))
    f.set_status(conn, o["id"], "result_unknown", current_attempt_id=att["id"])
    d = orders.resolve_unknown(conn, ws, o["id"], "not_ordered", actor_user_id=uid)
    a = d["attempts"][0]
    assert a["account_name"] == "环境-DEMO-N" and a["device_name"] == "Mac-DEMO-N"
    assert a["resolved_by_name"] == "测试所有者"
    o2 = f.ingest_ready_order(conn, ws)
    f.make_attempt(conn, ws, o2["id"], None, status="failed")
    a2 = orders.get_detail(conn, ws, o2["id"])["attempts"][0]
    assert a2["account_name"] is None and a2["device_name"] is None and a2["resolved_by_name"] is None


def test_clear_attention_is_an_allowed_action_and_records_event(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws)
    assert "clear_attention" not in orders.get_detail(conn, ws, o["id"])["allowed_actions"]
    orders._update(conn, ws, o["id"], {"needs_attention": True, "attention_note": "DEMO 提醒"})
    assert "clear_attention" in orders.get_detail(conn, ws, o["id"])["allowed_actions"]
    d = orders.clear_attention(conn, ws, o["id"], "已处理", uid)
    assert d["needs_attention"] is False and "clear_attention" not in d["allowed_actions"]
    ev = conn.execute("SELECT detail FROM procure.events WHERE order_id = %s AND kind = 'order.attention_cleared'",
                      (o["id"],)).fetchone()
    assert ev["detail"]["previous"] == "DEMO 提醒"
    other = f.make_workspace(conn)
    with pytest.raises(DomainError) as e:
        orders.clear_attention(conn, other["id"], o["id"], None, uid)
    assert e.value.code == "NOT_FOUND"


def test_missing_fields_are_keys_even_for_legacy_label_rows(conn, workspace):
    ws = workspace["id"]
    out = orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-MK", asin=None, phone=None),
                        f.file_source(conn, ws, 2))
    assert orders.get_detail(conn, ws, out.order_id)["missing_fields"] == ["asin", "phone"]
    conn.execute("UPDATE procure.purchase_orders SET missing_fields = %s WHERE id = %s", (["ASIN", "收货人电话"],
                                                                                         out.order_id))
    assert orders.get_detail(conn, ws, out.order_id)["missing_fields"] == ["asin", "phone"]
