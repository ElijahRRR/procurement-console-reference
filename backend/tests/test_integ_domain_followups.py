"""集成：执行之后回到开始前的人工出口也要落下被挡住的来源变化、账号的暂停起因、概览的「需要留意」、
清除模拟结果的状态事件、Feishu 取消不被文件快照撤销（D1-02）。

- 结果待核实 → 人工核实「未下单」：待核实期间来源撤回人审 → 待确认（不留分配）；出现外部 AMZ 订单号 → 待核实；
- 已采购 → 亚马逊取消 → 同意重采：已采购期间被挡住的来源变化同样落到任务上；
- 账号 pause_cause / open_result_unknown（结果待核实、账号级错误、人工）；
- revert_simulation 每次改状态都写 order.status_changed {simulation_reset: true}；
- 飞书说取消后，再导入的文件行售后状态不是取消值（含执行中）→ 取消保留，插件中止后任务已取消。

全部数据虚构（DEMO 名称、假地址、假 AMZ 订单号、B0DEMO ASIN、555 电话）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services import accounts, clock, execution, imports, orders, overview, simulation
from tests import factories as f
from tests import factories_exec as fx
from tests.api_helpers import import_row, xlsx_bytes

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
EXT_NO = "111-0000000-0000042"


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _claimed(conn, workspace, no, *, source=None):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    src = source or f.file_source(conn, ws, 2)
    order = fx.assigned_order(conn, ws, acc, source=src, upstream_order_no=no)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    return {"ws": ws, "uid": workspace["user_id"], "acc": acc, "dev": dev, "src": src, "order": order,
            "run_id": run_id, "a": a}


def _unknown(conn, workspace, no):
    """领取 → 过护栏 → 登记提交 → 插件说结果不明：任务结果待核实、账号暂停。"""
    e = _claimed(conn, workspace, no)
    fx.to_submitted(conn, e["dev"], e["a"])
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                                  fx.failure_payload(None, "DEMO 断线", outcome="unknown"))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    return e


def _purchased(conn, workspace, no):
    e = _claimed(conn, workspace, no)
    res = fx.complete_success(conn, e["dev"], e["a"])
    assert res["order_status"] == "purchased"
    return e


def _reingest(conn, e, **kw):
    """同一批次同一行再送来该任务的记录（文件为主的任务，同种来源）。"""
    src = f.file_source(conn, e["ws"], 2, batch_id=e["src"].batch_id)
    return orders.ingest(conn, e["ws"], f.make_record(upstream_order_no=e["order"]["upstream_order_no"], **kw), src)


def _status_events(conn, oid):
    return [r["detail"] for r in conn.execute(
        "SELECT detail FROM procure.events WHERE order_id = %s AND kind = 'order.status_changed' ORDER BY id",
        (oid,)).fetchall()]


# ── M1：结果待核实 → 未下单 ──────────────────────────────────────────────────


def test_not_ordered_after_review_revoked_while_unknown_lands_in_pending_review(conn, workspace):
    e = _unknown(conn, workspace, "PO-DEMO-RU1")
    _reingest(conn, e, review="rejected", review_value="DEMO 拒绝")
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "result_unknown" and o["review_status"] == "approved"      # 待核实期间不改原值

    d = orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "not_ordered", note="DEMO 未下单",
                               actor_user_id=e["uid"])
    assert d["status"] == "pending_review" and d["assigned_account"] is None
    o = fx.order_row(conn, e["order"]["id"])
    assert o["review_status"] == "rejected" and o["review_value"] == "DEMO 拒绝" and o["assigned_account_id"] is None
    assert o["needs_attention"] is True and "人审结论" in o["attention_note"]
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM procure.events WHERE order_id = %s ORDER BY id",
                                             (o["id"],)).fetchall()]
    assert kinds.count("order.source_changed_during_attempt") == 1
    # 账号仍暂停（核实不自动恢复），恢复后也领不到这一笔
    assert fx.account_row(conn, e["acc"]["id"])["execution_paused"] is True
    accounts.resume(conn, e["ws"], e["acc"]["id"], actor_kind="user", actor_id=e["uid"])
    assert execution.next_attempt(conn, e["dev"], e["run_id"])["attempt"] is None


def test_not_ordered_after_external_order_no_while_unknown_goes_conflict(conn, workspace):
    e = _unknown(conn, workspace, "PO-DEMO-RU2")
    _reingest(conn, e, external_amz_order_no=EXT_NO, external_buyer_account="buyer-demo-elsewhere")
    assert fx.order_row(conn, e["order"]["id"])["status"] == "result_unknown"

    d = orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "not_ordered", actor_user_id=e["uid"])
    assert d["status"] == "conflict" and d["assigned_account"] is None
    o = fx.order_row(conn, e["order"]["id"])
    assert o["conflict"]["kind"] == "external_order_no"
    assert o["conflict"]["incoming"]["external_amz_order_no"] == EXT_NO and EXT_NO in o["status_reason"]
    assert "resolve_conflict" in d["allowed_actions"]
    # 接受 → 按外部单号登记为已采购，不重新下单
    d = orders.resolve_conflict(conn, e["ws"], o["id"], "accept", actor_user_id=e["uid"])
    assert d["status"] == "purchased"


def test_not_ordered_after_field_change_while_unknown_keeps_old_values_in_conflict(conn, workspace):
    e = _unknown(conn, workspace, "PO-DEMO-RU3")
    _reingest(conn, e, asin="B0DEMO0X02")
    d = orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "not_ordered", actor_user_id=e["uid"])
    assert d["status"] == "conflict"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["asin"] == "B0DEMO0001" and o["conflict"]["kind"] == "fields"
    assert o["conflict"]["incoming"]["asin"] == "B0DEMO0X02"


def test_not_ordered_without_source_change_keeps_assignment(conn, workspace):
    e = _unknown(conn, workspace, "PO-DEMO-RU4")
    _reingest(conn, e)                                       # 同样内容重传：不算变化
    d = orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "not_ordered", actor_user_id=e["uid"])
    assert d["status"] == "assigned" and d["assigned_account"]["id"] == e["acc"]["id"]
    o = fx.order_row(conn, e["order"]["id"])
    assert o["conflict"] is None and o["needs_attention"] is False


# ── M2：已采购 → 亚马逊取消 → 同意重采 ──────────────────────────────────────


def test_repurchase_approve_applies_review_revoked_while_purchased(conn, workspace):
    e = _purchased(conn, workspace, "PO-DEMO-RP1")
    _reingest(conn, e, review="rejected", review_value="DEMO 拒绝")
    assert fx.order_row(conn, e["order"]["id"])["review_status"] == "approved"
    orders.mark_amazon_cancelled(conn, e["ws"], e["order"]["id"], "DEMO 缺货", e["uid"])
    d = orders.decide_repurchase(conn, e["ws"], e["order"]["id"], "approve", None, e["uid"])
    assert d["status"] == "pending_review"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["review_status"] == "rejected" and o["assigned_account_id"] is None and o["needs_attention"] is True


def test_repurchase_approve_applies_field_change_while_purchased_as_conflict(conn, workspace):
    e = _purchased(conn, workspace, "PO-DEMO-RP2")
    _reingest(conn, e, quantity=3)
    orders.mark_amazon_cancelled(conn, e["ws"], e["order"]["id"], None, e["uid"])
    d = orders.decide_repurchase(conn, e["ws"], e["order"]["id"], "approve", None, e["uid"])
    assert d["status"] == "conflict"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["quantity"] == 1 and o["conflict"]["kind"] == "fields" and o["conflict"]["incoming"]["quantity"] == 3
    assert o["external_amz_order_no"] is None


def test_repurchase_approve_ignores_own_order_no_and_unchanged_source(conn, workspace):
    """来源读回本系统写回的 AMZ 订单号（同一笔任务的尝试单号）不算外部单号；没有变化 → 正常回待分配。"""
    e = _purchased(conn, workspace, "PO-DEMO-RP3")
    own_no = fx.attempt_row(conn, e["a"]["attempt_id"])["amazon_order_no"]
    _reingest(conn, e, external_amz_order_no=own_no)
    orders.mark_amazon_cancelled(conn, e["ws"], e["order"]["id"], None, e["uid"])
    d = orders.decide_repurchase(conn, e["ws"], e["order"]["id"], "approve", None, e["uid"])
    assert d["status"] in ("queued", "assigned")
    assert fx.order_row(conn, e["order"]["id"])["conflict"] is None


# ── M3：账号的暂停起因与待核实数 ──────────────────────────────────────────────


def test_account_pause_cause_and_open_result_unknown(conn, workspace):
    e = _unknown(conn, workspace, "PO-DEMO-PC1")
    acc = accounts.get(conn, e["ws"], e["acc"]["id"])
    assert acc["pause_cause"] == "result_unknown" and acc["open_result_unknown"] == 1
    # 人工再暂停一次：起因跟着最近一次暂停（pause_reason 也是那一次写的）
    acc = accounts.pause(conn, e["ws"], e["acc"]["id"], "DEMO 人工检查", actor_kind="user", actor_id=e["uid"])
    assert acc["pause_cause"] == "manual" and acc["open_result_unknown"] == 1
    orders.resolve_unknown(conn, e["ws"], e["order"]["id"], "not_ordered", actor_user_id=e["uid"])
    acc = accounts.resume(conn, e["ws"], e["acc"]["id"], actor_kind="user", actor_id=e["uid"])
    assert acc["pause_cause"] is None and acc["open_result_unknown"] == 0
    listed = {a["id"]: a for a in accounts.list_accounts(conn, e["ws"])}
    assert listed[e["acc"]["id"]]["pause_cause"] is None


def test_account_error_pause_cause(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-PC2")
    execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.failure_payload("CAPTCHA_ENCOUNTERED", "DEMO"))
    acc = accounts.get(conn, e["ws"], e["acc"]["id"])
    assert acc["execution_paused"] is True and acc["pause_cause"] == "account_error"
    assert acc["open_result_unknown"] == 0


def test_pause_cause_for_legacy_rows_without_events(conn, workspace):
    ws = workspace["id"]
    a = f.make_account(conn, ws, execution_paused=True, pause_reason="结果待核实：PO-DEMO-OLD")
    b = f.make_account(conn, ws, execution_paused=True, pause_reason="DEMO 停用")
    assert accounts.get(conn, ws, a["id"])["pause_cause"] == "result_unknown"
    assert accounts.get(conn, ws, b["id"])["pause_cause"] == "manual"
    # 只读键整份回传不报错
    got = accounts.get(conn, ws, b["id"])
    assert accounts.update(conn, ws, b["id"], {**got, "notes": "DEMO"}, workspace["user_id"])["notes"] == "DEMO"


# ── M4：概览的「需要留意」 ───────────────────────────────────────────────────


def test_overview_needs_attention_counts_any_status(conn, workspace):
    ws = workspace["id"]
    rows = [f.ingest_ready_order(conn, ws) for _ in range(3)]
    assert overview.overview(conn, ws)["needs_attention"] == 0
    orders._update(conn, ws, rows[0]["id"], {"needs_attention": True, "attention_note": "DEMO"})
    f.set_status(conn, rows[1]["id"], "purchased", needs_attention=True)
    other = f.make_workspace(conn)
    o = f.ingest_ready_order(conn, other["id"])
    orders._update(conn, other["id"], o["id"], {"needs_attention": True})
    assert overview.overview(conn, ws)["needs_attention"] == 2
    assert overview.overview(conn, other["id"])["needs_attention"] == 1


# ── M5：批次详情带工作表与表头 ────────────────────────────────────────────────


def test_batch_detail_has_sheets_and_headers_from_stored_file(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    data = xlsx_bytes([import_row("PO-DEMO-SH1"), import_row("PO-DEMO-SH2")])
    up = imports.upload(conn, ws, filename="demo-orders.xlsx", data=data, actor_user_id=uid)
    d = imports.get_batch(conn, ws, up["batch_id"], with_sheets=True)
    assert d["sheets"] == up["sheets"] and d["headers"] == up["headers"]
    assert d["sheets"][0]["row_count"] == 2 and d["headers"][0] == "平台订单号"
    assert "sheets" not in imports.get_batch(conn, ws, up["batch_id"])
    imports.discard(conn, ws, up["batch_id"], uid)
    d = imports.get_batch(conn, ws, up["batch_id"], with_sheets=True)
    assert d["sheets"] == [] and d["headers"] == []


# ── M7：清除模拟结果的状态事件 ───────────────────────────────────────────────


def test_revert_simulation_records_status_changed_with_simulation_reset(conn, workspace):
    e = _purchased(conn, workspace, "PO-DEMO-SR1")
    out = simulation.reset(conn, e["ws"], e["uid"])
    assert e["order"]["id"] in out["orders_restored"]
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "assigned"
    last = _status_events(conn, o["id"])[-1]
    assert last["from"] == "purchased" and last["to"] == "assigned" and last["simulation_reset"] is True
    # 开始前的任务再清一次：状态没变，不再写状态事件
    n = len(_status_events(conn, o["id"]))
    orders.revert_simulation(conn, e["ws"], o["id"], actor_kind="system")
    assert len(_status_events(conn, o["id"])) == n


@pytest.mark.parametrize("status", ["exception", "result_unknown", "repurchase_pending", "closed"])
def test_revert_simulation_event_for_each_started_status(conn, workspace, status):
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    o = f.ingest_ready_order(conn, ws)
    f.make_attempt(conn, ws, o["id"], acc["id"], status="aborted", simulated=True)
    f.set_status(conn, o["id"], status, assigned_account_id=acc["id"], assignment_kind="auto")
    row = orders.revert_simulation(conn, ws, o["id"], actor_kind="system")
    assert row["status"] == "assigned"
    ev = _status_events(conn, o["id"])[-1]
    assert ev == {"from": status, "to": "assigned", "reason": "已清除模拟结果", "simulation_reset": True}


# ── M9 / D1-02：飞书取消后，文件快照的「没取消」不撤销取消 ──────────────────────


def _feishu_cancel(conn, ws, no, src):
    orders.ingest(conn, ws, f.make_record(upstream_order_no=no, after_sale_value="已取消", upstream_cancelled=True), src)


def _import_file(conn, ws, uid, no, after_sale):
    headers = ["平台订单号", "ASIN", "数量", "人工审核", "限价", "采购单价", "收货人姓名", "收货人地址", "收货人城市",
               "收货人州/省", "邮编", "收货人电话", "收货人国家", "建议采购日期", "售后状态"]
    row = import_row(no, 售后状态=after_sale, 建议采购日期="2026-10-31")
    up = imports.upload(conn, ws, filename="demo-after-sale.xlsx", data=xlsx_bytes([row], headers=headers),
                        actor_user_id=uid)
    assert up["suggested_mapping"]["售后状态"] == "after_sale_value"
    pv = imports.preview(conn, ws, up["batch_id"], sheet=up["sheet"], mapping=up["suggested_mapping"])
    imports.commit(conn, ws, up["batch_id"], uid)
    return pv["rows"]["rows"][0]


def test_file_import_with_normal_after_sale_never_lifts_feishu_cancel_while_executing(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    no = "PO-DEMO-D102A"
    e = _claimed(conn, workspace, no, source=f.feishu_source(conn, ws, "recD102A"))
    _feishu_cancel(conn, ws, no, e["src"])
    row = _import_file(conn, ws, uid, no, "正常")
    assert any("未撤销" in i for i in row["issues"])
    o = fx.order_row(conn, e["order"]["id"])
    assert o["upstream_cancelled"] is True and o["status"] == "executing"

    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["continue"] is False
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                                  fx.failure_payload("ABORTED_UPSTREAM_CANCELLED", p["cancel_reason"]))
    assert res["order_status"] == "cancelled"
    # 再来一份「没取消」的文件也不撤销，人工也不能撤销（来源仍说取消）
    _import_file(conn, ws, uid, no, "正常")
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "cancelled" and o["upstream_cancelled"] is True
    assert "allowed_actions" in orders.get_detail(conn, ws, o["id"])
    assert "uncancel" not in orders.get_detail(conn, ws, o["id"])["allowed_actions"]


def test_file_import_with_normal_after_sale_never_lifts_feishu_cancel_before_start(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    no = "PO-DEMO-D102B"
    src = f.feishu_source(conn, ws, "recD102B")
    o = f.ingest_ready_order(conn, ws, source=src, upstream_order_no=no)
    _feishu_cancel(conn, ws, no, src)
    assert orders.get_row(conn, ws, o["id"])["status"] == "cancelled"
    _import_file(conn, ws, uid, no, "正常")
    row = orders.get_row(conn, ws, o["id"])
    assert row["status"] == "cancelled" and row["upstream_cancelled"] is True
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM procure.events WHERE order_id = %s",
                                             (o["id"],)).fetchall()]
    assert "order.upstream_uncancelled" not in kinds
    # 发出取消的飞书记录自己撤销 → 才撤销取消标记（任务仍需人工撤销取消）
    orders.ingest(conn, ws, f.make_record(upstream_order_no=no, after_sale_value="正常"), src)
    assert orders.get_row(conn, ws, o["id"])["upstream_cancelled"] is False
