"""执行中的身份检查（契约 §5）：基准是「这个浏览器最新的登录状态 × 批次账号当前登记的买家号 ID」。

- 执行中途换了买家号（或退出登录、读数不一致、太久没上报）：未登记提交的那一笔在进度 / 提交登记处被拦下并中止，
  任务回已分配、不暂停账号、不占额度；已登记提交的照常收结果。
- 结果上报、结束批次、迟到回执不做身份检查（回执不丢）。
- 护栏的 buyer 项把「谁在下单」钉在结算页：结算页读到的买家号 ID 必须是批次账号登记的那个。它拦下时（护栏判定处、
  提交登记复核处）按中止类落地：尝试 aborted（IDENTITY_MISMATCH）、任务回已分配、不进异常、不暂停账号，批次同时被
  服务端结束（end_reason = identity_mismatch）——同一批次不会再领到同一笔（方案 v2 修订二验收第 10 条）。
- 提交登记、开始批次先锁浏览器行，和并发的身份上报串行。
全部数据虚构。"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from registry import db
from services import accounts, clock, devices, execution, orders, quota
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)
OTHER = "A2TEST000002"                                    # 另一个买家号（虚构）


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _err(fn, *a, **kw) -> DomainError:
    with pytest.raises(DomainError) as e:
        fn(*a, **kw)
    return e.value


def _claimed(conn, workspace, **acc_kw):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, name="环境-DEMO-A", **acc_kw)
    dev = fx.logged_in_device(conn, workspace, account=acc, name="Mac-DEMO")
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    return {"ws": ws, "uid": workspace["user_id"], "acc": acc, "dev": dev, "order": order, "run_id": run_id, "a": a,
            "workspace": workspace}


def _paused(conn, e) -> bool:
    return fx.account_row(conn, e["acc"]["id"])["execution_paused"]


def _switches(cid: str):
    """执行中途可能出现的各种「不再是批次的买家号」：(上报的身份, 登录状态, 说明里应有的字)。"""
    return [
        (fx.identity(OTHER), "signed_in", "浏览器登录的买家号变了"),
        (fx.identity(None, signed_in=False), "signed_out", "浏览器没有登录亚马逊"),
        (fx.identity(None), "no_id", "没有读到浏览器登录的买家号 ID"),
        (fx.identity(cid, conflict=True), "conflict", "标签页显示的买家号不一致"),
    ]


# ── 未登记提交：中止、不暂停、不占额度 ─────────────────────────────────────────


@pytest.mark.parametrize("case", range(4), ids=["other_buyer", "signed_out", "no_id", "conflict"])
def test_switch_before_submit_stops_progress_and_refuses_submitting(conn, workspace, case):
    e = _claimed(conn, workspace)
    fx.to_guard_passed(conn, e["dev"], e["a"])
    ident, state, word = _switches(e["acc"]["amazon_customer_id"])[case]
    devices.report_identity(conn, e["dev"], ident)
    aid = e["a"]["attempt_id"]
    p = execution.progress(conn, e["dev"], aid, stage="before_submit")
    assert p["continue"] is False and p["cancel_code"] == "IDENTITY_MISMATCH" and word in p["cancel_reason"]
    err = _err(execution.mark_submitting, conn, e["dev"], aid)
    assert err.code == "IDENTITY_MISMATCH" and err.http_status == 409 and word in err.message
    assert err.detail == {"login_state": state, "stale": False, "identity_at": NOW.isoformat()}
    assert "identity_status" not in err.detail
    assert fx.attempt_row(conn, aid)["submitted_at"] is None                    # 拒绝时什么都没改
    hb = execution.run_heartbeat(conn, e["dev"], e["run_id"])
    assert hb["stop"] is True and hb["stop_reason"] == "identity_mismatch"
    # 插件按 continue=false 上报中止：任务回已分配，账号不暂停，额度不占
    res = execution.report_result(conn, e["dev"], aid, fx.failure_payload("ABORTED_BY_SERVER", p["cancel_reason"]))
    assert res == {"attempt_status": "aborted", "order_status": "assigned"}
    o = fx.order_row(conn, e["order"]["id"])
    assert o["assigned_account_id"] == e["acc"]["id"] and o["current_attempt_id"] is None
    assert not _paused(conn, e) and f.count(conn, "events", "kind = %s", (execution.PAUSE_MARKER_KIND,)) == 0
    assert quota.used(conn, e["acc"]["id"], TODAY, True) == 0
    out = execution.next_attempt(conn, e["dev"], e["run_id"])
    assert out["attempt"] is None and word in out["reason"]
    assert execution.finish_run(conn, e["dev"], e["run_id"])["end_reason"] == "finished"


def test_released_task_is_picked_up_by_a_browser_logged_into_the_right_buyer(conn, workspace):
    """换了买家号的浏览器放下的那一笔，登录着原买家号的另一个浏览器接着做；换号的浏览器转去执行新买家号的账号。"""
    e = _claimed(conn, workspace)
    ws = e["ws"]
    acc_b = fx.exec_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=OTHER)
    order_b = fx.assigned_order(conn, ws, acc_b)
    devices.report_identity(conn, e["dev"], fx.identity(OTHER))
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="checkout")
    execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                            fx.failure_payload("IDENTITY_MISMATCH", p["cancel_reason"]))
    assert fx.attempt_row(conn, e["a"]["attempt_id"])["status"] == "aborted"
    execution.finish_run(conn, e["dev"], e["run_id"])
    pc = fx.logged_in_device(conn, e["workspace"], account=e["acc"], name="PC-DEMO")
    b = fx.claim(conn, pc, fx.start(conn, pc))
    assert b["order_id"] == e["order"]["id"] and b["account"]["name"] == "环境-DEMO-A"
    switched = fx.start_run(conn, e["dev"])                       # 同一个浏览器，现在登录的是买家号 B
    assert switched["account"] == {"id": acc_b["id"], "name": "环境-DEMO-B"}
    assert fx.claim(conn, e["dev"], switched["run_id"])["order_id"] == order_b["id"]


def test_stale_identity_mid_run_uses_stale_code_and_submitting_reports_stale(conn, workspace):
    e = _claimed(conn, workspace)
    fx.to_guard_passed(conn, e["dev"], e["a"])
    for t in (150, 300, 450, 600, 601):
        clock.freeze(NOW + timedelta(seconds=t))
        hb = execution.run_heartbeat(conn, e["dev"], e["run_id"])
    assert hb["stop"] is False                                                   # 批次心跳不看新鲜度
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p == {"continue": False, "cancel_code": "IDENTITY_STALE", "cancel_reason": "插件超过 10 分钟没有上报登录状态"}
    err = _err(execution.mark_submitting, conn, e["dev"], e["a"]["attempt_id"])
    assert err.code == "IDENTITY_MISMATCH" and err.message == "插件超过 10 分钟没有上报登录状态，不能提交"
    assert err.detail == {"login_state": "signed_in", "stale": True, "identity_at": NOW.isoformat()}
    fx.refresh_identity(conn, e["dev"], e["acc"])
    assert execution.mark_submitting(conn, e["dev"], e["a"]["attempt_id"])["ok"] is True


# ── 已登记提交：照常收结果；结果 / 结束 / 迟到回执不做身份检查 ─────────────────


@pytest.mark.parametrize("case", range(4), ids=["other_buyer", "signed_out", "no_id", "conflict"])
def test_switch_after_submit_still_accepts_the_result(conn, workspace, case):
    e = _claimed(conn, workspace)
    aid = e["a"]["attempt_id"]
    fx.to_submitted(conn, e["dev"], e["a"])
    devices.report_identity(conn, e["dev"], _switches(e["acc"]["amazon_customer_id"])[case][0])
    assert execution.progress(conn, e["dev"], aid, stage="after_click") == {
        "continue": True, "cancel_reason": None, "cancel_code": None}
    assert execution.mark_verifying(conn, e["dev"], aid, "DEMO 银行验证")["attempt_status"] == "verifying"
    res = execution.report_result(conn, e["dev"], aid, fx.success_payload(e["a"]))
    assert res == {"attempt_status": "succeeded", "order_status": "purchased"}
    assert execution.report_result(conn, e["dev"], aid, fx.success_payload(e["a"]))["duplicate"] is True
    assert not _paused(conn, e)
    out = execution.finish_run(conn, e["dev"], e["run_id"])
    assert out["end_reason"] == "finished" and out["stats"]["succeeded"] == 1


def test_failure_after_submit_from_switched_browser_goes_result_unknown_as_usual(conn, workspace):
    """铁律 9 不变：已登记提交的非成功结局照样进结果待核实并暂停账号，与身份无关。"""
    e = _claimed(conn, workspace)
    fx.to_submitted(conn, e["dev"], e["a"])
    devices.report_identity(conn, e["dev"], fx.identity(OTHER))
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.failure_payload("IDENTITY_MISMATCH", "DEMO"))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    assert _paused(conn, e)
    assert fx.account_row(conn, e["acc"]["id"])["pause_reason"] == f"结果待核实：{e['order']['upstream_order_no']}"


def test_late_receipt_and_finish_are_accepted_without_fresh_or_matching_identity(conn, workspace):
    e = _claimed(conn, workspace)
    aid = e["a"]["attempt_id"]
    fx.to_submitted(conn, e["dev"], e["a"])
    clock.freeze(NOW + timedelta(seconds=200))
    assert execution.sweep(conn)["unknown"] == 1                                  # 断线：租约过期，结果待核实
    clock.freeze(NOW + timedelta(hours=2))                                        # 身份早已过期，还换了买家号
    devices.report_identity(conn, e["dev"], fx.identity(None, signed_in=False))
    res = execution.report_result(conn, e["dev"], aid, fx.success_payload(e["a"]))
    assert res["late_receipt"] is True and res["attempt_status"] == "unknown"
    assert f.count(conn, "events", "entity = 'attempt' AND entity_id = %s AND kind = 'attempt.late_result'",
                   (aid,)) == 1


# ── 改了账号登记的 ID：基准跟着走 ─────────────────────────────────────────────


def test_baseline_is_the_account_current_customer_id(conn, workspace):
    """基准是批次账号**当前**登记的 ID：直接改库里的 ID（服务层在执行中不让改）后，原来匹配的浏览器立即不符。"""
    e = _claimed(conn, workspace)
    aid = e["a"]["attempt_id"]
    assert execution.progress(conn, e["dev"], aid, stage="checkout")["continue"] is True
    assert _err(accounts.update, conn, e["ws"], e["acc"]["id"], {"amazon_customer_id": OTHER}, e["uid"]).code == "CONFLICT"
    conn.execute("UPDATE procure.buyer_accounts SET amazon_customer_id = %s WHERE id = %s", (OTHER, e["acc"]["id"]))
    p = execution.progress(conn, e["dev"], aid, stage="checkout")
    assert p["cancel_code"] == "IDENTITY_MISMATCH" and OTHER in p["cancel_reason"]
    assert execution.run_heartbeat(conn, e["dev"], e["run_id"])["stop_reason"] == "identity_mismatch"
    devices.report_identity(conn, e["dev"], fx.identity(OTHER))                   # 浏览器也换到新 ID → 又一致了
    assert execution.progress(conn, e["dev"], aid, stage="checkout")["continue"] is True
    conn.execute("UPDATE procure.buyer_accounts SET amazon_customer_id = NULL WHERE id = %s", (e["acc"]["id"],))
    p = execution.progress(conn, e["dev"], aid, stage="checkout")
    assert p["cancel_code"] == "IDENTITY_MISMATCH" and "没有登记买家号 ID" in p["cancel_reason"]


# ── 护栏 buyer 项 ─────────────────────────────────────────────────────────────


def _events(conn, entity: str, entity_id: int, kind: str) -> list[dict]:
    return [r["detail"] for r in conn.execute(
        "SELECT detail FROM procure.events WHERE entity = %s AND entity_id = %s AND kind = %s ORDER BY id",
        (entity, entity_id, kind)).fetchall()]


def _assert_run_is_over(conn, e, aid: int) -> None:
    """批次被服务端结束之后，插件对它的每一种请求都得到「批次已结束」的既有答复（409 LEASE_LOST），不报 500、
    不改任何东西、不会再建第二笔尝试。"""
    ended = "执行批次已结束（结算页登录的买家号不是这个批次的买家号），请重新开始批次"
    for call in (lambda: execution.next_attempt(conn, e["dev"], e["run_id"]),
                 lambda: execution.run_heartbeat(conn, e["dev"], e["run_id"]),
                 lambda: execution.finish_run(conn, e["dev"], e["run_id"]),
                 lambda: execution.finish_run(conn, e["dev"], e["run_id"], "buyer_changed", "DEMO"),
                 lambda: execution.progress(conn, e["dev"], aid, stage="before_submit"),
                 lambda: execution.submit_guard(conn, e["dev"], aid, fx.facts_for(conn, e["order"])),
                 lambda: execution.mark_submitting(conn, e["dev"], aid),
                 lambda: execution.mark_verifying(conn, e["dev"], aid, "DEMO")):
        err = _err(call)
        assert (err.code, err.http_status, err.message) == ("LEASE_LOST", 409, ended)
        assert err.detail == {"run_id": e["run_id"], "end_reason": "identity_mismatch"}
    # 这笔尝试再报结果：报的正是 IDENTITY_MISMATCH 的失败 → 原样返回（duplicate）；别的 → CONFLICT
    dup = execution.report_result(conn, e["dev"], aid, fx.failure_payload("IDENTITY_MISMATCH", "DEMO"))
    assert dup == {"attempt_status": "aborted", "order_status": "assigned", "duplicate": True}
    for payload in (fx.failure_payload("ABORTED_BY_SERVER", "DEMO"), fx.failure_payload("GUARD_BLOCKED", "DEMO"),
                    fx.success_payload(e["a"]), fx.failure_payload(None, "DEMO", outcome="unknown")):
        err = _err(execution.report_result, conn, e["dev"], aid, payload)
        assert err.code == "CONFLICT" and err.http_status == 409 and "已中止" in err.message
    assert f.count(conn, "purchase_attempts", "purchase_order_id = %s", (e["order"]["id"],)) == 1
    run = fx.run_row(conn, e["run_id"])
    assert run["end_reason"] == "identity_mismatch" and run["stats"] == {"claimed": 1, "aborted": 1}
    assert len(_events(conn, "run", e["run_id"], "run.ended")) == 1                  # 只结束一次


def _assert_aborted_not_exception(conn, e, aid: int, reason: str) -> dict:
    """尝试中止（IDENTITY_MISMATCH）、任务回已分配并留在原账号、不进异常、不暂停账号、不占额度。"""
    att = fx.attempt_row(conn, aid)
    assert (att["status"], att["error_code"], att["submitted_at"]) == ("aborted", "IDENTITY_MISMATCH", None)
    assert att["error_detail"] == reason and att["finished_at"] == NOW
    o = fx.order_row(conn, e["order"]["id"])
    assert (o["status"], o["assigned_account_id"], o["current_attempt_id"]) == ("assigned", e["acc"]["id"], None)
    assert o["assignment_kind"] == "auto" and o["status_reason"] == f"{reason}，未提交前中止，任务保留待重新领取"
    assert o["page_load_retries"] == 0 and o["needs_attention"] is False
    assert not _paused(conn, e) and f.count(conn, "events", "kind = %s", (execution.PAUSE_MARKER_KIND,)) == 0
    assert f.count(conn, "events", "kind = 'account.paused'") == 0
    assert quota.used(conn, e["acc"]["id"], TODAY, True) == 0
    status_events = _events(conn, "order", e["order"]["id"], "order.status_changed")
    assert [(d["from"], d["to"]) for d in status_events][-1] == ("executing", "assigned")
    assert not any(d["to"] == "exception" for d in status_events)
    return att


def test_guard_blocks_when_checkout_page_shows_another_buyer(conn, workspace):
    """验收第 10 条（护栏判定处）：浏览器上报的登录状态没变，但结算页当场读到的是另一个买家号——这一笔提交前中止、
    任务回已分配、批次结束，不进异常、不暂停账号；插件拿到的裁决照旧是 block / IDENTITY_MISMATCH。"""
    e = _claimed(conn, workspace)
    aid, cid = e["a"]["attempt_id"], e["acc"]["amazon_customer_id"]
    execution.progress(conn, e["dev"], aid, stage="checkout")
    v = execution.submit_guard(conn, e["dev"], aid, fx.facts_for(conn, e["order"], customer_id=OTHER))
    assert v["verdict"] == "block" and v["code"] == "IDENTITY_MISMATCH"
    assert v["checks"]["buyer"] == {"ok": False, "expected": cid, "actual": OTHER}
    reason = f"结算页登录的买家号（{OTHER}）不是这个批次的买家号（{cid}）"
    assert v["reasons"] == [reason]
    assert set(v) == {"verdict", "code", "reasons", "checks", "delivery_days", "total_for_cap", "fulfillment"}
    att = _assert_aborted_not_exception(conn, e, aid, reason)
    assert att["stage"] == "guard" and att["guard_input"]["customer_id"] == OTHER
    assert att["guard_verdict"]["code"] == "IDENTITY_MISMATCH" and att["delivery_days"] == 3
    # 事件：护栏拦截、尝试中止、批次结束（同一个事务里）
    assert _events(conn, "attempt", aid, "attempt.guard_blocked") == [
        {"code": "IDENTITY_MISMATCH", "reasons": [reason], "at": "guard"}]
    assert _events(conn, "attempt", aid, "attempt.aborted") == [
        {"error_code": "IDENTITY_MISMATCH", "reason": reason, "run_id": e["run_id"]}]
    assert _events(conn, "attempt", aid, "attempt.failed") == []
    assert _events(conn, "run", e["run_id"], "run.ended") == [
        {"end_reason": "identity_mismatch", "account_id": e["acc"]["id"], "device_id": e["dev"]["id"], "aborted": 0,
         "unknown": 0, "attempt_id": aid, "at": "guard", "reason": reason}]
    run = fx.run_row(conn, e["run_id"])
    assert run["ended_at"] == NOW and run["end_reason"] == "identity_mismatch"
    _assert_run_is_over(conn, e, aid)


def test_buyer_block_does_not_loop_and_a_new_run_can_pick_the_task_up_again(conn, workspace):
    """不循环：拦下之后同一个批次领不到任何东西（批次已结束）；重新开始的批次才会再领到这一笔，结算页读数对了就照常
    买完。任务与账号都没有留下需要人工处理的状态。"""
    e = _claimed(conn, workspace)
    aid = e["a"]["attempt_id"]
    other_order = fx.assigned_order(conn, e["ws"], e["acc"])                      # 同一账号上还有别的可领任务
    execution.submit_guard(conn, e["dev"], aid, fx.facts_for(conn, e["order"], customer_id=OTHER))
    for _ in range(3):
        assert _err(execution.next_attempt, conn, e["dev"], e["run_id"]).code == "LEASE_LOST"
    assert f.count(conn, "purchase_attempts") == 1
    assert fx.order_row(conn, other_order["id"])["status"] == "assigned"
    # 浏览器的批次已经放开：马上能开新批次（不会 DEVICE_BUSY / ACCOUNT_BUSY），按顺序再领到同一笔
    run2 = fx.start(conn, e["dev"])
    assert run2 != e["run_id"]
    b = fx.claim(conn, e["dev"], run2)
    assert (b["order_id"], b["attempt_no"], b["kind"]) == (e["order"]["id"], 2, "auto")
    assert fx.complete_success(conn, e["dev"], b) == {"attempt_status": "succeeded", "order_status": "purchased"}
    assert fx.claim(conn, e["dev"], run2)["order_id"] == other_order["id"]
    assert not _paused(conn, e)


def test_buyer_block_at_guard_for_an_upstream_cancelled_task_cancels_it(conn, workspace):
    """中止类的既有规则照旧：上游在执行期间取消了的任务，中止后直接转已取消（不回已分配）；批次同样结束。"""
    e = _claimed(conn, workspace)
    aid = e["a"]["attempt_id"]
    conn.execute("UPDATE procure.purchase_orders SET upstream_cancelled = true, upstream_cancel_note = '已取消' "
                 "WHERE id = %s", (e["order"]["id"],))
    v = execution.submit_guard(conn, e["dev"], aid, fx.facts_for(conn, e["order"], customer_id=OTHER))
    assert v["code"] == "IDENTITY_MISMATCH"
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "cancelled" and o["assigned_account_id"] is None
    assert fx.attempt_row(conn, aid)["status"] == "aborted" and not _paused(conn, e)
    assert fx.run_row(conn, e["run_id"])["end_reason"] == "identity_mismatch"


def test_buyer_block_rederives_the_task_when_the_source_changed_after_claim(conn, workspace):
    """任务回已分配后按派生规则重新校验：领取后来源改了执行相关字段，中止时照样落到任务上（转待核实），
    不会带着过时的内容留在已分配。"""
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, name="环境-DEMO-A")
    dev = fx.logged_in_device(conn, workspace, account=acc, name="Mac-DEMO")
    src = f.feishu_source(conn, ws, "recDEMOBUYER1")
    order = fx.assigned_order(conn, ws, acc, source=src, upstream_order_no="PO-DEMO-BUYER-1")
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    changed = f.make_record(upstream_order_no="PO-DEMO-BUYER-1", quantity=3)
    orders.ingest(conn, ws, changed, src)                                         # 采购开始后：只记在来源关联上
    assert fx.order_row(conn, order["id"])["quantity"] == 1
    v = execution.submit_guard(conn, dev, a["attempt_id"], fx.facts_for(conn, order, customer_id=OTHER))
    assert v["code"] == "IDENTITY_MISMATCH"
    o = fx.order_row(conn, order["id"])
    assert o["status"] == "conflict" and o["needs_attention"] is True and "数量" in o["status_reason"]
    assert o["assigned_account_id"] is None
    att = fx.attempt_row(conn, a["attempt_id"])
    assert (att["status"], att["error_code"]) == ("aborted", "IDENTITY_MISMATCH")
    assert fx.run_row(conn, run_id)["end_reason"] == "identity_mismatch"
    assert not fx.account_row(conn, acc["id"])["execution_paused"]


def test_buyer_block_ending_a_scheduled_run_leaves_the_slot_free_for_another_browser(conn, workspace):
    """定时批次被 buyer 项拦截结束：和租约过期一样不占这个时间点（只有 finished 才算「已经执行过」），登录着正确
    买家号的另一个浏览器今天还能为同一个时间点开批次，把放下的那一笔做完。接手的是到点时就在线的浏览器：批次还
    在跑时它来问过，得到的是 ACCOUNT_BUSY 而不是 SLOT_DONE，所以没有把这个时间点记掉。接手的批次在跑时，后来才
    登记的第三个浏览器同样得到 ACCOUNT_BUSY；正常结束之后三个浏览器都是 SLOT_DONE。"""
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws, name="环境-DEMO-定时", purchase_mode="scheduled", schedule_times=["09:30"])
    dev = fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-1")
    pc = fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-2")       # 到点时就在线
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev, trigger="scheduled", slot="09:30")
    a = fx.claim(conn, dev, run_id)
    asked = _err(fx.start_run, conn, pc, trigger="scheduled", slot="09:30")           # 批次还在跑：不是 SLOT_DONE
    assert asked.code == "ACCOUNT_BUSY" and asked.detail["holder_device_id"] == dev["id"]
    execution.submit_guard(conn, dev, a["attempt_id"], fx.facts_for(conn, order, customer_id=OTHER))
    run = fx.run_row(conn, run_id)
    assert (run["end_reason"], run["scheduled_slot"]) == ("identity_mismatch", "09:30")
    again = fx.start(conn, pc, trigger="scheduled", slot="09:30")
    late = fx.logged_in_device(conn, workspace, account=acc, name="定时-DEMO-3")     # 批次结束之后才来的
    assert _err(fx.start_run, conn, late, trigger="scheduled", slot="09:30").code == "ACCOUNT_BUSY"
    b = fx.claim(conn, pc, again)
    assert b["order_id"] == order["id"] and b["attempt_no"] == 2
    assert fx.complete_success(conn, pc, b)["order_status"] == "purchased"
    execution.finish_run(conn, pc, again)
    for d in (dev, pc, late):
        assert _err(fx.start_run, conn, d, trigger="scheduled", slot="09:30").code == "SLOT_DONE"


def test_run_end_reason_labels_cover_the_new_and_the_legacy_reason(conn, workspace):
    """批次结束原因的中文：identity_mismatch 是护栏 buyer 项拦截时服务端写的；device_revoked 只剩历史行，标签三端
    统一为「浏览器的授权已撤销（旧版）」。两种批次之后的请求都得到带这句话的 LEASE_LOST。"""
    assert execution.END_REASON_ZH == {
        "finished": "插件结束了批次", "lease_expired": "执行权超时（租约过期）",
        "identity_mismatch": "结算页登录的买家号不是这个批次的买家号", "device_revoked": "浏览器的授权已撤销（旧版）",
        "simulation_reset": "已清除模拟结果"}
    assert (execution.BUYER_BLOCK_CODE, execution.BUYER_BLOCK_END_REASON) == ("IDENTITY_MISMATCH", "identity_mismatch")
    assert execution.BUYER_BLOCK_END_REASON in execution.STOP_REASONS             # 与批次心跳的停止原因同一个词
    e = _claimed(conn, workspace)
    conn.execute("UPDATE procure.execution_runs SET ended_at = %s, end_reason = 'device_revoked' WHERE id = %s",
                 (NOW, e["run_id"]))                                              # 旧版留下的历史行
    err = _err(execution.run_heartbeat, conn, e["dev"], e["run_id"])
    assert err.code == "LEASE_LOST" and err.message == "执行批次已结束（浏览器的授权已撤销（旧版）），请重新开始批次"
    assert err.detail == {"run_id": e["run_id"], "end_reason": "device_revoked"}


@pytest.mark.parametrize("cid", [None, "", "not-an-id", 12345])
def test_guard_blocks_as_data_unreliable_without_checkout_buyer_id(conn, workspace, cid):
    """旧插件不带 customer_id、或没读到：按数据不可靠拦截，绝不因为缺失而放行。"""
    e = _claimed(conn, workspace)
    aid = e["a"]["attempt_id"]
    v = execution.submit_guard(conn, e["dev"], aid, fx.good_facts(e["order"], customer_id=cid))
    assert v["verdict"] == "block" and v["code"] == "DATA_UNRELIABLE"
    assert v["checks"]["buyer"]["ok"] is None and "结算页没有读到买家号 ID" in v["reasons"][0]
    assert fx.attempt_row(conn, aid)["status"] == "failed"
    assert fx.order_row(conn, e["order"]["id"])["status"] == "exception"


def test_guard_passes_with_matching_checkout_buyer_and_recheck_uses_current_account_id(conn, workspace):
    e = _claimed(conn, workspace)
    aid, cid = e["a"]["attempt_id"], e["acc"]["amazon_customer_id"]
    v = execution.submit_guard(conn, e["dev"], aid, fx.facts_for(conn, e["order"], customer_id=f" {cid.lower()} "))
    assert v["verdict"] == "pass" and v["checks"]["buyer"] == {"ok": True, "expected": cid, "actual": cid}
    assert fx.attempt_row(conn, aid)["guard_input"]["customer_id"] == cid
    # 提交登记时按账号当前登记的 ID 复核存下的事实：账号的 ID 与浏览器的登录一起被换掉（身份检查过了）也拦得住
    conn.execute("UPDATE procure.buyer_accounts SET amazon_customer_id = %s WHERE id = %s", (OTHER, e["acc"]["id"]))
    devices.report_identity(conn, e["dev"], fx.identity(OTHER))
    out = execution.mark_submitting(conn, e["dev"], aid)
    assert out["ok"] is False and out["blocked"] is True and out["code"] == "GUARD_BLOCKED"
    assert out["detail"]["block_code"] == "IDENTITY_MISMATCH"
    assert fx.attempt_row(conn, aid)["submitted_at"] is None


def test_submitting_recheck_buyer_block_aborts_the_attempt_and_ends_the_run(conn, workspace):
    """验收第 10 条（提交登记复核处）：护栏放行之后、登记之前，存下的结算页买家号 ID 已不是批次账号当前登记的 ID——
    不登记提交（插件不会点击下单），落地与护栏判定处相同：尝试中止、任务回已分配、不暂停账号、批次结束。服务层不抛
    异常（否则落地会被一起回滚），返回带 blocked 标记的结果，由插件路由转成 409 GUARD_BLOCKED。"""
    e = _claimed(conn, workspace)
    aid, cid = e["a"]["attempt_id"], e["acc"]["amazon_customer_id"]
    fx.to_guard_passed(conn, e["dev"], e["a"])
    conn.execute("UPDATE procure.buyer_accounts SET amazon_customer_id = %s WHERE id = %s", (OTHER, e["acc"]["id"]))
    devices.report_identity(conn, e["dev"], fx.identity(OTHER))
    reason = f"结算页登录的买家号（{cid}）不是这个批次的买家号（{OTHER}）"
    out = execution.mark_submitting(conn, e["dev"], aid)
    assert out == {
        "ok": False, "blocked": True, "code": "GUARD_BLOCKED",
        "message": f"按最新配置复核护栏未通过：{reason}，不能提交",
        "detail": {"block_code": "IDENTITY_MISMATCH", "reasons": [reason], "attempt_status": "aborted",
                   "order_status": "assigned", "run_ended": True, "end_reason": "identity_mismatch"}}
    assert "submitted_at" not in out
    att = _assert_aborted_not_exception(conn, e, aid, reason)
    assert att["stage"] == "submitting" and att["guard_input"]["customer_id"] == cid       # 事实原样保留
    assert att["guard_verdict"]["verdict"] == "block" and att["guard_verdict"]["code"] == "IDENTITY_MISMATCH"
    assert _events(conn, "attempt", aid, "attempt.submitting") == []
    assert _events(conn, "attempt", aid, "attempt.guard_blocked") == [
        {"code": "IDENTITY_MISMATCH", "reasons": [reason], "at": "submitting"}]
    assert _events(conn, "attempt", aid, "attempt.aborted") == [
        {"error_code": "IDENTITY_MISMATCH", "reason": reason, "run_id": e["run_id"]}]
    ended = _events(conn, "run", e["run_id"], "run.ended")
    assert len(ended) == 1 and ended[0]["end_reason"] == "identity_mismatch"
    assert (ended[0]["attempt_id"], ended[0]["at"], ended[0]["reason"]) == (aid, "submitting", reason)
    _assert_run_is_over(conn, e, aid)


def test_submitting_recheck_other_guard_codes_still_refuse_without_changing_anything(conn, workspace):
    """复核时别的拦截码（这里是金额区间）照旧：409 GUARD_BLOCKED、什么都不改、批次继续；只有 buyer 项结束批次。"""
    e = _claimed(conn, workspace)
    aid = e["a"]["attempt_id"]
    fx.to_guard_passed(conn, e["dev"], e["a"])
    conn.execute("UPDATE procure.buyer_accounts SET min_amount = 90 WHERE id = %s", (e["acc"]["id"],))
    err = _err(execution.mark_submitting, conn, e["dev"], aid)
    assert err.code == "GUARD_BLOCKED" and err.http_status == 409
    assert err.detail["block_code"] == "SUBTOTAL_OUT_OF_RANGE" and set(err.detail) == {"block_code", "reasons"}
    assert fx.attempt_row(conn, aid)["status"] == "guard_passed"
    assert fx.run_row(conn, e["run_id"])["ended_at"] is None
    assert execution.run_heartbeat(conn, e["dev"], e["run_id"])["stop"] is False


# ── 浏览器行的锁：提交登记 / 开始批次与身份上报串行 ───────────────────────────


def _blocked_call(dsn, fn):
    """在另一条连接、另一个线程里调 fn(conn)，返回 (线程, 结果字典)。"""
    result: dict = {}

    def worker():
        try:
            with db.pg_conn(dsn, statement_timeout_s=20) as c:
                result["out"] = fn(c)
        except DomainError as exc:
            result["err"] = exc
        except Exception as exc:  # noqa: BLE001 - 带回主线程断言
            result["crash"] = exc

    t = threading.Thread(target=worker)
    t.start()
    return t, result


def test_submitting_waits_for_concurrent_identity_report_and_sees_the_new_identity(clean_db, conn, workspace):
    """身份上报先到（还没提交事务）：提交登记等它，然后按新身份拒绝——不会按旧身份放行点击下单。"""
    e = _claimed(conn, workspace)
    fx.to_guard_passed(conn, e["dev"], e["a"])
    conn.commit()
    with db.pg_conn(clean_db) as reporter:
        devices.report_identity(reporter, e["dev"], fx.identity(OTHER))           # 持有浏览器行的锁
        t, result = _blocked_call(clean_db, lambda c: execution.mark_submitting(c, e["dev"], e["a"]["attempt_id"]))
        t.join(timeout=1.0)
        assert t.is_alive() and not result                                        # 在等浏览器行的锁
        reporter.commit()
    t.join(timeout=20)
    assert "crash" not in result and result["err"].code == "IDENTITY_MISMATCH", result
    conn.commit()
    assert fx.attempt_row(conn, e["a"]["attempt_id"])["submitted_at"] is None


def test_identity_report_waits_for_submitting_then_is_handled_by_later_steps(clean_db, conn, workspace):
    """提交登记先到：身份上报排在它后面；登记有效（可能已下单），之后的结果照常收。"""
    e = _claimed(conn, workspace)
    fx.to_guard_passed(conn, e["dev"], e["a"])
    conn.commit()
    with db.pg_conn(clean_db) as submitter:
        assert execution.mark_submitting(submitter, e["dev"], e["a"]["attempt_id"])["ok"] is True
        t, result = _blocked_call(clean_db, lambda c: devices.report_identity(c, e["dev"], fx.identity(OTHER)))
        t.join(timeout=1.0)
        assert t.is_alive() and not result
        submitter.commit()
    t.join(timeout=20)
    assert "crash" not in result and result["out"]["login"]["customer_id"] == OTHER, result
    conn.commit()
    assert fx.attempt_row(conn, e["a"]["attempt_id"])["status"] == "submitting"
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.success_payload(e["a"]))
    assert res["attempt_status"] == "succeeded"


def test_start_run_waits_for_concurrent_identity_report(clean_db, conn, workspace):
    acc = fx.exec_account(conn, workspace["id"])
    dev = fx.logged_in_device(conn, workspace, account=acc)
    conn.commit()
    with db.pg_conn(clean_db) as reporter:
        devices.report_identity(reporter, dev, fx.identity(None, signed_in=False))
        t, result = _blocked_call(clean_db, lambda c: execution.start_run(
            c, dev, customer_id=acc["amazon_customer_id"], trigger="manual", simulated=True))
        t.join(timeout=1.0)
        assert t.is_alive() and not result
        reporter.commit()
    t.join(timeout=20)
    assert "crash" not in result and result["err"].code == "IDENTITY_MISMATCH", result
    assert result["err"].detail["login_state"] == "signed_out"
    conn.commit()
    assert f.count(conn, "execution_runs") == 0
