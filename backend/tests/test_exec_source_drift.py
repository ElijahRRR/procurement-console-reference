"""领取后来源变化（v1 §10「已有 AMZ 订单号不重新下单」「缺人审不下发」、契约 §2 来源更新、§5 模拟）：

- 提交登记前来源出现外部 AMZ 订单号 / 人审撤回 / 执行相关字段被改 → 进度 continue=false、提交登记 409；
- 采购没有完成就结束（插件中止、清扫、失败、清除模拟结果）→ 被挡住的变化落到任务上：人审撤回 → 待确认，
  外部单号 / 字段变化 → 待核实（incoming 为来源新值），任务不会带着旧值回到可领取；
- 来源改了又改回、重传同样内容、领取前已人工「保留」的冲突，都不算变化；每笔尝试只落一次。

全部数据虚构（DEMO 名称、假地址、假 AMZ 订单号）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import clock, execution, orders, simulation
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
EXT_NO = "111-0000000-0000001"


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _err(fn, *a, **kw) -> DomainError:
    with pytest.raises(DomainError) as e:
        fn(*a, **kw)
    return e.value


def _claimed(conn, workspace, no, *, guard=True):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    src = f.file_source(conn, ws, 2)
    order = fx.assigned_order(conn, ws, acc, source=src, upstream_order_no=no)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    if guard:
        fx.to_guard_passed(conn, dev, a)
    return {"ws": ws, "uid": workspace["user_id"], "acc": acc, "dev": dev, "src": src, "order": order,
            "run_id": run_id, "a": a}


def _reingest(conn, e, *, new_batch=False, **kw):
    """同一来源（缺省同一批次同一行）再送来该任务的记录，带上要改的字段。"""
    src = f.file_source(conn, e["ws"], 2) if new_batch else f.file_source(conn, e["ws"], 2, batch_id=e["src"].batch_id)
    rec = f.make_record(upstream_order_no=e["order"]["upstream_order_no"], **kw)
    return orders.ingest(conn, e["ws"], rec, src)


def _abort(conn, e, why="DEMO 中止"):
    return execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                                   fx.failure_payload("ABORTED_UPSTREAM_CANCELLED", why))


# ── 提交登记前：停止 ──────────────────────────────────────────────────────────


def test_external_order_no_mid_attempt_stops_before_submit_and_goes_conflict(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-EXT")
    out = _reingest(conn, e, external_amz_order_no=EXT_NO, external_buyer_account="buyer-elsewhere")
    assert out.verdict == "conflict" and fx.order_row(conn, e["order"]["id"])["status"] == "executing"

    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["continue"] is False and EXT_NO in p["cancel_reason"] and p["cancel_reason"].endswith("未提交前中止")
    err = _err(execution.mark_submitting, conn, e["dev"], e["a"]["attempt_id"])
    assert err.code == "SOURCE_CHANGED" and err.http_status == 409 and EXT_NO in err.message
    assert fx.attempt_row(conn, e["a"]["attempt_id"])["submitted_at"] is None

    res = _abort(conn, e, p["cancel_reason"])
    assert res == {"attempt_status": "aborted", "order_status": "conflict"}
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["assigned_account_id"] is None and o["current_attempt_id"] is None
    assert o["conflict"]["kind"] == "external_order_no"
    assert o["conflict"]["incoming"]["external_amz_order_no"] == EXT_NO
    assert EXT_NO in o["status_reason"] and o["needs_attention"] is True
    # 不会再被领取；同一记录再来一次也不会让它回到可领取
    assert execution.next_attempt(conn, e["dev"], e["run_id"])["attempt"] is None
    _reingest(conn, e, external_amz_order_no=EXT_NO, external_buyer_account="buyer-elsewhere")
    assert fx.order_row(conn, e["order"]["id"])["status"] == "conflict"
    # 人工接受 → 按外部单号登记为已采购，不重新下单
    detail = orders.resolve_conflict(conn, e["ws"], e["order"]["id"], "accept", actor_user_id=e["uid"])
    assert detail["status"] == "purchased"
    assert f.count(conn, "purchase_attempts", "purchase_order_id = %s AND kind = 'external'", (e["order"]["id"],)) == 1


def test_review_revoked_mid_attempt_stops_and_lands_in_pending_review(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-REV")
    _reingest(conn, e, review_value="钓鱼", review="rejected")
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "executing" and o["review_status"] == "approved"      # 执行中不改原值

    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["continue"] is False and "钓鱼" in p["cancel_reason"]
    assert _err(execution.mark_submitting, conn, e["dev"], e["a"]["attempt_id"]).code == "SOURCE_CHANGED"

    res = _abort(conn, e)
    assert res["order_status"] == "pending_review"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "pending_review" and o["review_status"] == "rejected" and o["review_value"] == "钓鱼"
    assert o["review_source"] == "file" and o["assigned_account_id"] is None
    assert execution.next_attempt(conn, e["dev"], e["run_id"])["attempt"] is None


def test_field_change_mid_attempt_stops_and_goes_conflict_with_incoming(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-ASIN")
    _reingest(conn, e, asin="B0DEMO0X01", quantity=2)
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["continue"] is False and "ASIN" in p["cancel_reason"] and "数量" in p["cancel_reason"]
    _abort(conn, e)
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["asin"] == "B0DEMO0001" and o["quantity"] == 1
    assert o["conflict"]["kind"] == "fields"
    assert o["conflict"]["incoming"]["asin"] == "B0DEMO0X01" and o["conflict"]["incoming"]["quantity"] == 2
    assert "ASIN" in o["status_reason"] and "需人工核实" in o["status_reason"]
    orders.resolve_conflict(conn, e["ws"], e["order"]["id"], "accept", actor_user_id=e["uid"])
    o = fx.order_row(conn, e["order"]["id"])
    assert o["asin"] == "B0DEMO0X01" and o["quantity"] == 2 and o["conflict"] is None
    assert o["status"] in ("queued", "assigned")


def test_upstream_cancel_still_wins_over_drift(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-CXL")
    _reingest(conn, e, asin="B0DEMO0X01", upstream_cancelled=True, after_sale_value="DEMO 退款")
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["continue"] is False and "上游已取消" in p["cancel_reason"]
    assert _abort(conn, e)["order_status"] == "cancelled"


# ── 不算变化 ──────────────────────────────────────────────────────────────────


def test_change_reverted_before_submit_is_not_drift(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-BACK")
    _reingest(conn, e, asin="B0DEMO0X01")
    _reingest(conn, e)                                   # 又改回领取时的内容
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p == {"continue": True, "cancel_reason": None, "cancel_code": None}
    assert execution.mark_submitting(conn, e["dev"], e["a"]["attempt_id"])["ok"] is True


def test_identical_reupload_in_new_batch_is_not_drift_but_changed_one_is(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-REUP")
    _reingest(conn, e, new_batch=True)                   # 新一批文件、内容相同
    assert execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="x")["continue"] is True
    _reingest(conn, e, new_batch=True, price_cap="30.00")
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="y")
    assert p["continue"] is False and "限价" in p["cancel_reason"]


def test_conflict_kept_before_claim_is_not_reported_again(conn, workspace):
    """另一种来源在领取前就与任务不同、人工选择「保留」：领取后它没再变，就不算变化。"""
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    order = f.ingest_ready_order(conn, ws, source=f.file_source(conn, ws, 2), upstream_order_no="PO-DEMO-KEEP")
    feishu = f.feishu_source(conn, ws, "recDEMO0001")
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-KEEP", asin="B0DEMO0X01"), feishu)
    assert fx.order_row(conn, order["id"])["status"] == "conflict"
    orders.resolve_conflict(conn, ws, order["id"], "keep", actor_user_id=workspace["user_id"])
    orders.assign_manual(conn, ws, order["id"], acc["id"], actor_user_id=workspace["user_id"])
    a = fx.claim(conn, dev, fx.start(conn, dev))
    assert execution.progress(conn, dev, a["attempt_id"], stage="checkout")["continue"] is True
    fx.complete_success(conn, dev, a)
    assert fx.order_row(conn, order["id"])["status"] == "purchased"


# ── 采购未完成就结束：落到任务上 ──────────────────────────────────────────────


def test_lease_sweep_applies_drift(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-SWEEP")
    _reingest(conn, e, external_amz_order_no=EXT_NO)
    clock.freeze(NOW + timedelta(seconds=181))
    out = execution.sweep(conn)
    assert out["aborted"] == 1
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["conflict"]["kind"] == "external_order_no"


def test_task_level_failure_with_drift_stays_exception_then_resume_derives(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-EXC", guard=False)
    _reingest(conn, e, review_value="拒绝", review="rejected")
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.failure_payload("OUT_OF_STOCK", "DEMO"))
    assert res["order_status"] == "exception"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "exception" and o["review_status"] == "rejected" and o["needs_attention"] is True
    detail = orders.resume(conn, e["ws"], e["order"]["id"], actor_user_id=e["uid"])
    assert detail["status"] == "pending_review"


def test_guard_block_with_drift_records_conflict_on_exception(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-GB", guard=False)
    _reingest(conn, e, asin="B0DEMO0X01")
    execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="checkout")
    verdict = execution.submit_guard(conn, e["dev"], e["a"]["attempt_id"],
                                     fx.good_facts(fx.order_row(conn, e["order"]["id"]), total="99.00"))
    assert verdict["code"] == "PRICE_OVER_CAP"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "exception" and o["conflict"]["incoming"] == {"asin": "B0DEMO0X01"}
    assert "resolve_conflict" in orders.allowed_actions(o)


def test_drift_is_applied_once_per_attempt(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-ONCE")
    _reingest(conn, e, asin="B0DEMO0X01")
    _abort(conn, e)
    orders.resolve_conflict(conn, e["ws"], e["order"]["id"], "keep", actor_user_id=e["uid"])
    orders.assign_manual(conn, e["ws"], e["order"]["id"], e["acc"]["id"], actor_user_id=e["uid"])
    assert fx.order_row(conn, e["order"]["id"])["status"] == "assigned"
    # 清除模拟结果：开始前的任务不再按旧尝试重复落地
    simulation.reset(conn, e["ws"], e["uid"])
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "assigned" and o["conflict"] is None and o["asin"] == "B0DEMO0001"
    # 重新领取：新基线里已有改过的来源，「保留」的决定不再被翻出来
    fx.refresh_identity(conn, e["dev"], e["acc"])
    b = fx.claim(conn, e["dev"], fx.start(conn, e["dev"]))
    assert execution.progress(conn, e["dev"], b["attempt_id"], stage="checkout")["continue"] is True


# ── 清除模拟结果（D5：模拟采购之后来源改了）────────────────────────────────────


def _sim_purchased(conn, workspace, no):
    e = _claimed(conn, workspace, no, guard=False)
    fx.complete_success(conn, e["dev"], e["a"])
    assert fx.order_row(conn, e["order"]["id"])["status"] == "purchased"
    return e


def test_reset_after_sim_purchase_and_upstream_asin_change_goes_conflict(conn, workspace):
    e = _sim_purchased(conn, workspace, "PO-DEMO-D5")
    out = _reingest(conn, e, new_batch=True, asin="B0DEMO0X01")          # 第二批文件改了 ASIN
    assert out.verdict == "conflict"
    summary = simulation.reset(conn, e["ws"], e["uid"])
    assert summary["orders_restored"] == [e["order"]["id"]]
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["asin"] == "B0DEMO0001"
    assert o["conflict"]["incoming"] == {"asin": "B0DEMO0X01"} and o["purchased_attempt_id"] is None
    fx.refresh_identity(conn, e["dev"], e["acc"])
    assert execution.next_attempt(conn, e["dev"], fx.start(conn, e["dev"]))["attempt"] is None
    # 同样内容的文件再导入一次（又一个新批次）：仍不会带着旧 ASIN 回到可领取（开始前同种来源的新值直接采用，
    # 冲突里过期的 ASIN 随之作废，或冲突保留）
    _reingest(conn, e, new_batch=True, asin="B0DEMO0X01")
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" or (o["asin"] == "B0DEMO0X01" and o["conflict"] is None)


def test_reset_after_sim_purchase_and_external_order_no_does_not_redispatch(conn, workspace):
    e = _sim_purchased(conn, workspace, "PO-DEMO-D2EXT")
    _reingest(conn, e, external_amz_order_no=EXT_NO)
    simulation.reset(conn, e["ws"], e["uid"])
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["conflict"]["kind"] == "external_order_no"
    fx.refresh_identity(conn, e["dev"], e["acc"])
    assert execution.next_attempt(conn, e["dev"], fx.start(conn, e["dev"]))["attempt"] is None


def test_reset_after_sim_purchase_and_review_revoked_goes_pending_review(conn, workspace):
    e = _sim_purchased(conn, workspace, "PO-DEMO-D2REV")
    _reingest(conn, e, review_value="钓鱼", review="rejected")
    simulation.reset(conn, e["ws"], e["uid"])
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "pending_review" and o["review_value"] == "钓鱼" and o["assigned_account_id"] is None


def test_reset_without_upstream_change_restores_assigned(conn, workspace):
    e = _sim_purchased(conn, workspace, "PO-DEMO-PLAIN")
    _reingest(conn, e)                                   # 同样的内容又同步了一次
    simulation.reset(conn, e["ws"], e["uid"])
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "assigned" and o["conflict"] is None and o["needs_attention"] is False


def test_reset_of_inflight_attempt_with_drift(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-INFL")
    _reingest(conn, e, asin="B0DEMO0X01")
    simulation.reset(conn, e["ws"], e["uid"])
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["conflict"]["incoming"] == {"asin": "B0DEMO0X01"}
    assert fx.attempt_row(conn, e["a"]["attempt_id"])["status"] == "aborted"


def test_payload_does_not_carry_source_baseline(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-SNAP", guard=False)
    assert "source_baseline" not in e["a"]
    snap = fx.attempt_row(conn, e["a"]["attempt_id"])["task_snapshot"]
    assert snap["attempt_id"] == e["a"]["attempt_id"] and len(snap["source_baseline"]) == 1
    assert snap["source_baseline"][0]["vals"]["asin"] == "B0DEMO0001"


def test_hold_written_on_executing_order_stops_and_derives_conflict(conn, workspace):
    """来源在执行期间把暂停写在任务上（order.conflict，由 ingest 写；这里直接造）：进度 continue=false、
    提交登记 409，插件中止后任务按派生规则转待核实（不是带着暂停回到已分配）。"""
    from psycopg.types.json import Jsonb

    e = _claimed(conn, workspace, "PO-DEMO-HOLD")
    hold = {"kind": "review", "source_kind": "feishu", "reason": "飞书来源的人工审核为「拒绝」，需人工核实",
            "fields": {"review_status": ["approved", "rejected"]}, "incoming": {"review_status": "rejected"}}
    conn.execute("UPDATE procure.purchase_orders SET conflict = %s WHERE id = %s", (Jsonb(hold), e["order"]["id"]))
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["continue"] is False and "来源要求暂停采购" in p["cancel_reason"] and "拒绝" in p["cancel_reason"]
    err = _err(execution.mark_submitting, conn, e["dev"], e["a"]["attempt_id"])
    assert err.code == "SOURCE_CHANGED" and err.detail["conflict_kind"] == "review"
    assert _abort(conn, e)["order_status"] == "conflict"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["assigned_account_id"] is None and o["conflict"]["kind"] == "review"


def test_hold_on_executing_order_also_derived_by_lease_sweep(conn, workspace):
    from psycopg.types.json import Jsonb

    e = _claimed(conn, workspace, "PO-DEMO-HOLD2")
    conn.execute("UPDATE procure.purchase_orders SET conflict = %s WHERE id = %s",
                 (Jsonb({"kind": "admission", "reason": "DEMO 准入暂停"}), e["order"]["id"]))
    clock.freeze(NOW + timedelta(seconds=181))
    execution.sweep(conn)
    assert fx.order_row(conn, e["order"]["id"])["status"] == "conflict"


# ── 领取后才出现的来源关联按首次关联的规则比（NEW-D2-execution-safety-1）──────────────


def _new_link_record(e, **kw):
    """同一任务的记录，按「新关联只带部分列」去掉 kw 里给成 None 的列（make_record 的缺省值不带进来）。"""
    return f.make_record(upstream_order_no=e["order"]["upstream_order_no"], **kw)


NO_ADDRESS = {k: None for k in ("recipient_name", "address1", "address2", "city", "state", "postal_code", "phone",
                                "country", "district")}


def _assert_not_drift(conn, e):
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p == {"continue": True, "cancel_reason": None, "cancel_code": None}
    assert execution.mark_submitting(conn, e["dev"], e["a"]["attempt_id"])["ok"] is True


@pytest.mark.parametrize("missing", [
    {"price_cap": None},                                  # 新一批文件没有「限价」列
    {"asin": None, "quantity": None},                     # 没有 ASIN、数量列
    NO_ADDRESS,                                           # 没有收件信息列
    {"suggested_purchase_date": None},                    # 没有建议采购日期列（任务的日期来自来源）：算出的日期不算变化
], ids=["no_price_cap", "no_asin_qty", "no_address", "no_suggested_date"])
def test_new_file_batch_without_some_columns_is_not_drift(conn, workspace, missing):
    e = _claimed(conn, workspace, f"PO-DEMO-NB{len(missing):02d}{sorted(missing)[0][:4].upper()}")
    assert fx.order_row(conn, e["order"]["id"])["suggested_date_basis"] == "source"
    out = orders.ingest(conn, e["ws"], _new_link_record(e, **missing), f.file_source(conn, e["ws"], 2))
    assert out.verdict in ("unchanged", "update")
    _assert_not_drift(conn, e)


def test_new_feishu_record_without_address_columns_is_not_drift(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-FSNA")
    orders.ingest(conn, e["ws"], _new_link_record(e, **NO_ADDRESS), f.feishu_source(conn, e["ws"], "recDEMO0NA1"))
    _assert_not_drift(conn, e)


def test_partial_new_batch_with_a_real_change_conflicts_only_on_that_field(conn, workspace):
    """新一批文件只带平台订单号与 ASIN（ASIN 改了）：只有 ASIN 算变化，conflict.incoming 里没有 None。"""
    e = _claimed(conn, workspace, "PO-DEMO-NBX")
    rec = _new_link_record(e, asin="B0DEMO0X02", quantity=None, unit_price=None, price_cap=None,
                           suggested_purchase_date=None, order_time=None, ship_days=None, **NO_ADDRESS)
    orders.ingest(conn, e["ws"], rec, f.file_source(conn, e["ws"], 2))
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["cancel_code"] == "SOURCE_CHANGED" and p["cancel_reason"].count("、") == 0 and "ASIN" in p["cancel_reason"]
    _abort(conn, e, p["cancel_reason"])
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["conflict"]["incoming"] == {"asin": "B0DEMO0X02"}
    orders.resolve_conflict(conn, e["ws"], o["id"], "accept", actor_user_id=e["uid"])
    o = fx.order_row(conn, o["id"])
    assert o["asin"] == "B0DEMO0X02" and o["quantity"] == 1 and o["recipient_name"] == "Demo Buyer"
    assert o["price_cap"] is not None and o["missing_fields"] == [] and o["status"] in ("queued", "assigned")


def test_new_batch_with_a_different_asin_is_still_drift(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-NBA")
    orders.ingest(conn, e["ws"], _new_link_record(e, asin="B0DEMO0X03"), f.file_source(conn, e["ws"], 2))
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["cancel_code"] == "SOURCE_CHANGED" and "ASIN" in p["cancel_reason"]
    assert _err(execution.mark_submitting, conn, e["dev"], e["a"]["attempt_id"]).code == "SOURCE_CHANGED"


def test_same_link_clearing_price_cap_is_still_drift(conn, workspace):
    """同一条来源关联把限价清空是真的清空（与 ingest 在 prv 非空时一致）：仍算变化。"""
    e = _claimed(conn, workspace, "PO-DEMO-CLR")
    _reingest(conn, e, price_cap=None)
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["cancel_code"] == "SOURCE_CHANGED" and "限价" in p["cancel_reason"]


# ── 算出来的建议采购日期（下单时间 / 配送天数改了，指纹不变；NEW-RB-2）────────────────


def test_ship_days_change_mid_attempt_is_drift_and_accept_writes_the_new_date(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    src = f.file_source(conn, ws, 2)
    order = fx.assigned_order(conn, ws, acc, source=src, upstream_order_no="PO-DEMO-SHIP",
                              suggested_purchase_date=None)        # 下单北京 10-27 + 6 − 4 = 10-29
    assert order["suggested_date_basis"] == "computed" and order["suggested_purchase_date"].isoformat() == "2026-10-29"
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    rec = f.make_record(upstream_order_no="PO-DEMO-SHIP", suggested_purchase_date=None, ship_days=0)
    orders.ingest(conn, ws, rec, f.file_source(conn, ws, 2, batch_id=src.batch_id))
    assert fx.order_row(conn, order["id"])["ship_days"] == 4                 # 执行中不改原值
    p = execution.progress(conn, dev, a["attempt_id"], stage="before_submit")
    assert p["continue"] is False and p["cancel_code"] == "SOURCE_CHANGED" and "建议采购日期" in p["cancel_reason"]
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("ABORTED_BY_SERVER", p["cancel_reason"]))
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "conflict" and o["conflict"]["kind"] == "fields"
    inc = o["conflict"]["incoming"]
    assert inc["suggested_purchase_date"] == "2026-11-02" and inc["suggested_date_basis"] == "computed"
    assert inc["ship_days"] == 0 and "order_time" not in inc
    orders.resolve_conflict(conn, ws, order["id"], "accept", actor_user_id=workspace["user_id"])
    o = fx.order_row(conn, order["id"])
    assert o["suggested_purchase_date"].isoformat() == "2026-11-02" and o["ship_days"] == 0
    if o["status"] != "assigned":
        orders.assign_manual(conn, ws, order["id"], acc["id"], actor_user_id=workspace["user_id"])
    fx.refresh_identity(conn, dev, acc)
    nxt = execution.next_attempt(conn, dev, fx.start(conn, dev) if fx.run_row(conn, run_id)["ended_at"] else run_id)
    assert nxt["attempt"] is None and "未来" in nxt["reason"]                 # 新日期之前不领取


def test_order_time_written_in_another_timezone_is_not_drift(conn, workspace):
    """同一时刻换个时区写（来源改成北京时间写法）：算出的日期不变，不算变化。"""
    from datetime import timezone as tz

    from services.clock import BJ_TZ

    e = _claimed(conn, workspace, "PO-DEMO-TZ")
    t = datetime(2026, 10, 27, 5, 0, tzinfo=tz.utc).astimezone(BJ_TZ)
    _reingest(conn, e, order_time=t)
    _assert_not_drift(conn, e)


# ── 已有暂停时的来源变化合并进冲突（NEW-RB-1）──────────────────────────────────


def test_hold_plus_external_order_no_merges_into_one_conflict_and_accept_records_purchase(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-HEX")
    _reingest(conn, e, external_amz_order_no=EXT_NO, external_buyer_account="buyer-elsewhere")
    orders.ingest(conn, e["ws"], _new_link_record(e, review="rejected", review_value="DEMO 拒绝"),
                  f.feishu_source(conn, e["ws"], "recDEMO0HEX"))
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["cancel_code"] == "SOURCE_HOLD"
    execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                            fx.failure_payload("ABORTED_BY_SERVER", p["cancel_reason"]))
    o = fx.order_row(conn, e["order"]["id"])
    c = o["conflict"]
    assert o["status"] == "conflict" and c["kind"] == "external_order_no"
    assert c["incoming"]["external_amz_order_no"] == EXT_NO and "review_status" in c["fields"]
    assert EXT_NO in c["reason"] and "DEMO 拒绝" in c["reason"]
    detail = orders.resolve_conflict(conn, e["ws"], o["id"], "accept", actor_user_id=e["uid"])
    assert detail["status"] == "purchased"
    assert f.count(conn, "purchase_attempts", "purchase_order_id = %s AND kind = 'external' AND amazon_order_no = %s",
                   (o["id"], EXT_NO)) == 1


def test_admission_hold_then_amz_no_from_the_same_feishu_record_keeps_both(conn, workspace):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    rec_id = "recDEMO0ADM"
    order = fx.assigned_order(conn, ws, acc, source=f.feishu_source(conn, ws, rec_id), upstream_order_no="PO-DEMO-ADM")
    a = fx.claim(conn, dev, fx.start(conn, dev))
    fx.to_guard_passed(conn, dev, a)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-ADM", purchase_status_value="已拍单"),
                  f.feishu_source(conn, ws, rec_id), admission={})
    assert fx.order_row(conn, order["id"])["conflict"]["kind"] == "admission"
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-ADM", purchase_status_value="已拍单",
                                          external_amz_order_no=EXT_NO),
                  f.feishu_source(conn, ws, rec_id), admission={})
    p = execution.progress(conn, dev, a["attempt_id"], stage="before_submit")
    assert p["cancel_code"] == "SOURCE_HOLD"
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("ABORTED_BY_SERVER", p["cancel_reason"]))
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "conflict" and o["conflict"]["kind"] == "admission"
    assert o["conflict"]["incoming"]["external_amz_order_no"] == EXT_NO and EXT_NO in o["conflict"]["reason"]


def test_hold_plus_asin_change_keeps_the_new_asin_in_incoming(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-HAS")
    _reingest(conn, e, asin="B0DEMO0X99")
    orders.ingest(conn, e["ws"], _new_link_record(e, review="rejected", review_value="DEMO 拒绝"),
                  f.feishu_source(conn, e["ws"], "recDEMO0HAS"))
    _abort(conn, e)
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "conflict" and o["conflict"]["kind"] == "review"
    assert o["conflict"]["incoming"]["asin"] == "B0DEMO0X99" and "asin" in o["conflict"]["fields"]
    assert o["asin"] == "B0DEMO0001"
