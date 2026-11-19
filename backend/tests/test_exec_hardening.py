"""执行协议加固：提交登记前按最新配置复核护栏（v1 §3.3「最终提交前重新校验有效限价和配送配置」、v2 默认 5）、
身份新鲜度在领取与提交前也生效、候选任务只是被锁着时不算「没有任务」、清除模拟结果与开始批次 / 领取互斥、
尝试种类规则、结算事实只存规范化后的内容（卡号只留后 4 位）、插件送来的文本里 PostgreSQL 存不了的字符
（NUL、不成对的代理项）不变成 500。全部数据虚构。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from registry import db, settings
from services import accounts, clock, execution, guard, orders, simulation
from services.errors import DomainError
from tests import factories as f
from tests import factories_exec as fx

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _err(fn, *a, **kw) -> DomainError:
    with pytest.raises(DomainError) as e:
        fn(*a, **kw)
    return e.value


def _env(conn, workspace, **acc_kw):
    acc = fx.exec_account(conn, workspace["id"], **acc_kw)
    dev = fx.logged_in_device(conn, workspace, account=acc)
    return acc, dev


def _days(n: int) -> dict:
    return {"text": f"Arriving in {n} days (DEMO)", "date": (clock.site_today() + timedelta(days=n)).isoformat(),
            "days": None}


# ── 提交登记前按最新配置复核护栏 ──────────────────────────────────────────────


def _guard_passed(conn, workspace, *, delivery_days=8, **acc_kw):
    ws = workspace["id"]
    acc, dev = _env(conn, workspace, **acc_kw)
    order = fx.assigned_order(conn, ws, acc, unit_price="30.00")
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_guard_passed(conn, dev, a, delivery=_days(delivery_days))
    return acc, dev, order, run_id, a


@pytest.mark.parametrize("patch,code", [
    ({"max_delivery_days": 5}, "DELIVERY_TOO_LONG"),
    ({"max_amount": "25.00"}, "SUBTOTAL_OUT_OF_RANGE"),
    ({"min_amount": "31.00"}, "SUBTOTAL_OUT_OF_RANGE"),
    ({"expected_card_last4": "1234"}, "CARD_MISMATCH"),
])
def test_submitting_rechecks_guard_against_latest_account_config(conn, workspace, patch, code):
    acc, dev, order, run_id, a = _guard_passed(conn, workspace, max_delivery_days=10, max_amount="50.00")
    accounts.update(conn, workspace["id"], acc["id"], patch, workspace["user_id"])
    e = _err(execution.mark_submitting, conn, dev, a["attempt_id"])
    assert e.code == "GUARD_BLOCKED" and e.http_status == 409 and e.detail["block_code"] == code
    assert "按最新配置复核护栏未通过" in e.message and e.message.endswith("不能提交")
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "guard_passed" and att["submitted_at"] is None
    # 插件的处理：再问一次进度（仍可继续）→ 上报 SUBMIT_REJECTED → 任务异常，不会提交
    assert execution.progress(conn, dev, a["attempt_id"], stage="before_submit")["continue"] is True
    res = execution.report_result(conn, dev, a["attempt_id"],
                                  fx.failure_payload("SUBMIT_REJECTED", f"{e.code}：{e.message}"))
    assert res == {"attempt_status": "failed", "order_status": "exception"}
    assert "服务端拒绝了提交登记" in fx.order_row(conn, order["id"])["status_reason"]


def test_submitting_rechecks_global_default_delivery_limit(conn, workspace):
    acc, dev, order, run_id, a = _guard_passed(conn, workspace, delivery_days=9)
    conn.execute("UPDATE procure.workspace_settings SET default_max_delivery_days = 3 WHERE workspace_id = %s",
                 (workspace["id"],))
    e = _err(execution.mark_submitting, conn, dev, a["attempt_id"])
    assert e.code == "GUARD_BLOCKED" and e.detail["block_code"] == "DELIVERY_TOO_LONG"
    assert "超过配送上限 3 天" in e.message


def test_submitting_passes_when_config_unchanged_or_loosened(conn, workspace):
    acc, dev, order, run_id, a = _guard_passed(conn, workspace, max_delivery_days=10)
    accounts.update(conn, workspace["id"], acc["id"], {"max_delivery_days": 12}, workspace["user_id"])
    assert execution.mark_submitting(conn, dev, a["attempt_id"])["ok"] is True


# ── 身份新鲜度：领取与提交前 ─────────────────────────────────────────────────


def _keep_lease(conn, dev, run_id, until_s: int) -> None:
    for t in range(150, until_s + 1, 150):
        clock.freeze(NOW + timedelta(seconds=t))
        execution.run_heartbeat(conn, dev, run_id)
    clock.freeze(NOW + timedelta(seconds=until_s))
    execution.run_heartbeat(conn, dev, run_id)


def test_stale_identity_stops_claiming_until_reported_again(conn, workspace):
    acc, dev = _env(conn, workspace)
    fx.assigned_order(conn, workspace["id"], acc)
    run_id = fx.start(conn, dev)
    _keep_lease(conn, dev, run_id, 601)
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is None and "插件超过 10 分钟没有上报登录状态" in out["reason"]
    fx.refresh_identity(conn, dev, acc)
    assert execution.next_attempt(conn, dev, run_id)["attempt"] is not None


def test_stale_identity_stops_before_submit(conn, workspace):
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, workspace["id"], acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_guard_passed(conn, dev, a)
    _keep_lease(conn, dev, run_id, 601)
    p = execution.progress(conn, dev, a["attempt_id"], stage="before_submit")
    assert p["continue"] is False and p["cancel_code"] == "IDENTITY_STALE"
    assert p["cancel_reason"] == "插件超过 10 分钟没有上报登录状态"
    e = _err(execution.mark_submitting, conn, dev, a["attempt_id"])
    assert e.code == "IDENTITY_MISMATCH" and e.http_status == 409
    assert e.detail == {"login_state": "signed_in", "stale": True, "identity_at": NOW.isoformat()}
    fx.refresh_identity(conn, dev, acc)
    assert execution.mark_submitting(conn, dev, a["attempt_id"])["ok"] is True
    assert fx.order_row(conn, order["id"])["status"] == "executing"


def test_stale_identity_abort_returns_task_to_assigned(conn, workspace):
    """身份过期时插件按 continue=false 中止：任务回已分配（不是异常），重新上报身份后可再领取。"""
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, workspace["id"], acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    _keep_lease(conn, dev, run_id, 601)
    p = execution.progress(conn, dev, a["attempt_id"], stage="checkout")
    res = execution.report_result(conn, dev, a["attempt_id"],
                                  fx.failure_payload("ABORTED_UPSTREAM_CANCELLED", p["cancel_reason"]))
    assert res == {"attempt_status": "aborted", "order_status": "assigned"}
    fx.refresh_identity(conn, dev, acc)
    assert fx.claim(conn, dev, run_id)["order_id"] == order["id"]


def test_identity_within_window_keeps_claiming(conn, workspace):
    acc, dev = _env(conn, workspace)
    fx.assigned_order(conn, workspace["id"], acc)
    run_id = fx.start(conn, dev)
    _keep_lease(conn, dev, run_id, 600)
    assert execution.next_attempt(conn, dev, run_id)["attempt"] is not None


# ── 候选任务被锁 ≠ 没有任务 ──────────────────────────────────────────────────


def _raw(dsn):
    return psycopg.connect(dsn, row_factory=dict_row)


def test_next_says_busy_with_retry_hint_when_candidates_are_locked(clean_db, conn, workspace):
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, workspace["id"], acc)
    run_id = fx.start(conn, dev)
    conn.commit()
    holder = _raw(clean_db)
    try:
        holder.execute("SELECT 1 FROM procure.purchase_orders WHERE id = %s FOR UPDATE", (order["id"],))
        with db.pg_conn(clean_db) as c:
            out = execution.next_attempt(c, dev, run_id)
        assert out["attempt"] is None and out["retry_after_s"] == settings.get().claim_busy_retry_s == 5
        assert "占用" in out["reason"] and "今天没有可执行的任务" not in out["reason"]
    finally:
        holder.rollback()
        holder.close()
    with db.pg_conn(clean_db) as c:
        got = execution.next_attempt(c, dev, run_id)
    assert got["attempt"]["order_id"] == order["id"] and "retry_after_s" not in got


def test_next_without_candidates_has_no_retry_hint(conn, workspace):
    acc, dev = _env(conn, workspace)
    out = execution.next_attempt(conn, dev, fx.start(conn, dev))
    assert out == {"attempt": None, "reason": "今天没有可执行的任务"}


# ── 清除模拟结果与开始批次 / 领取互斥 ─────────────────────────────────────────


def _thread(fn):
    out: dict = {}

    def run():
        try:
            out["v"] = fn()
        except BaseException as exc:  # noqa: BLE001
            out["e"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def _wait_lock(dsn, *, granted: bool, mode: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    with _raw(dsn) as c:
        c.autocommit = True
        while time.monotonic() < deadline:
            # pg_locks 是整个集群的：只看本测试库（别的测试会话可能同时在跑）
            row = c.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND classid = %s "
                            "AND granted = %s AND mode = %s "
                            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
                            (7_310_433, granted, mode)).fetchone()
            if row["n"]:
                return
            time.sleep(0.05)
    raise AssertionError(f"等不到咨询锁 granted={granted} mode={mode}")


def test_reset_blocks_new_runs_and_claims_until_done(clean_db, conn, workspace):
    """清除时另一台设备开批次领取：要么在清除之前（被一并清掉、任务恢复），要么在之后（不受影响）；
    不会出现「尝试被清掉、任务却卡在采购中」。"""
    ws, uid = workspace["id"], workspace["user_id"]
    acc_a, dev_a = _env(conn, workspace)
    acc_b, dev_b = _env(conn, workspace)
    x = fx.assigned_order(conn, ws, acc_a)
    y = fx.assigned_order(conn, ws, acc_b)
    run_a = fx.start(conn, dev_a)
    fx.complete_success(conn, dev_a, fx.claim(conn, dev_a, run_a))
    execution.finish_run(conn, dev_a, run_a)
    conn.commit()

    holder = _raw(clean_db)                      # 例如一次导入正锁着 X
    holder.execute("SELECT 1 FROM procure.purchase_orders WHERE id = %s FOR UPDATE", (x["id"],))

    def do_reset():
        with db.pg_conn(clean_db) as c:
            return simulation.reset(c, ws, uid)

    def do_claim():
        with db.pg_conn(clean_db) as c:
            rid = fx.start(c, dev_b)
            return rid, fx.claim(c, dev_b, rid)

    t1, res1 = _thread(do_reset)
    try:
        _wait_lock(clean_db, granted=True, mode="ExclusiveLock")        # 清除已开始、卡在 X 的行锁上
        t2, res2 = _thread(do_claim)
        _wait_lock(clean_db, granted=False, mode="ShareLock")           # 开批次在等清除结束
        assert t2.is_alive()
    finally:
        holder.rollback()
        holder.close()
    t1.join(20)
    t2.join(20)
    assert "e" not in res1 and "e" not in res2, (res1, res2)
    summary = res1["v"]
    run_b, b = res2["v"]
    assert summary["orders_restored"] == [x["id"]] and summary["attempts_reset"] == 1
    conn.rollback()
    assert fx.order_row(conn, x["id"])["status"] == "assigned"
    yr = fx.order_row(conn, y["id"])
    assert yr["status"] == "executing" and yr["current_attempt_id"] == b["attempt_id"]
    assert fx.attempt_row(conn, b["attempt_id"])["status"] == "claimed"
    assert fx.run_row(conn, run_b)["ended_at"] is None


def test_reset_only_touches_attempts_of_collected_orders(conn, workspace):
    """清除只改它收集到的任务的模拟尝试（同一事务里不会多改、少恢复）。"""
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace)
    o1 = fx.assigned_order(conn, ws, acc)
    o2 = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    fx.complete_success(conn, dev, fx.claim(conn, dev, run_id))
    fx.complete_success(conn, dev, fx.claim(conn, dev, run_id))
    out = simulation.reset(conn, ws, uid)
    assert out["attempts_reset"] == 2 and sorted(out["orders_restored"]) == sorted([o1["id"], o2["id"]])


# ── 尝试种类（唯一规则：execution._attempt_kind）─────────────────────────────


def test_kind_auto_after_manual_resume_of_page_load_exception(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("PAGE_LOAD_FAILED"))
    b = fx.claim(conn, dev, run_id)
    assert b["kind"] == "retry"
    execution.report_result(conn, dev, b["attempt_id"], fx.failure_payload("PAGE_LOAD_FAILED"))
    assert fx.order_row(conn, order["id"])["status"] == "exception"
    orders.resume(conn, ws, order["id"], actor_user_id=uid)
    c = fx.claim(conn, dev, run_id)
    assert c["order_id"] == order["id"] and c["kind"] == "auto"


def test_kind_auto_after_abort_and_retry_after_repurchase_page_error(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("ABORTED_BY_USER"))
    b = fx.claim(conn, dev, run_id)
    assert b["kind"] == "auto"
    fx.complete_success(conn, dev, b)
    orders.mark_amazon_cancelled(conn, ws, order["id"], "DEMO", actor_user_id=uid)
    orders.decide_repurchase(conn, ws, order["id"], "approve", actor_user_id=uid)
    orders.assign_manual(conn, ws, order["id"], acc["id"], actor_user_id=uid)
    c = fx.claim(conn, dev, run_id)
    assert c["kind"] == "repurchase"
    execution.report_result(conn, dev, c["attempt_id"], fx.failure_payload("PAGE_LOAD_FAILED"))
    d = fx.claim(conn, dev, run_id)
    assert d["order_id"] == order["id"] and d["kind"] == "retry"


# ── 结算事实规范化（卡号只留后 4 位、未知键丢弃）─────────────────────────────


def test_guard_facts_are_sanitized_before_storage_and_detail(conn, workspace):
    ws = workspace["id"]
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    facts = fx.facts_for(conn, order, card_last4="4111 1111 1111 1111",
                         delivery={**_days(3), "text": "Arriving " + "x" * 500},
                         billing={"card_number": "4111111111111111", "cvv": "123"}, page_html="<html>DEMO</html>",
                         cookies="session-id=DEMO")
    facts["ship_to"] = {"postal_code": "32960-1234", "name": "Demo Buyer", "line1": "100 Demo Street"}
    v = execution.submit_guard(conn, dev, a["attempt_id"], facts)
    assert v["verdict"] == "pass"
    stored = fx.attempt_row(conn, a["attempt_id"])["guard_input"]
    assert set(stored) == set(guard.FACT_KEYS)
    assert stored["card_last4"] == "1111" and stored["ship_to"] == {"postal_code": "32960-1234"}
    assert len(stored["delivery"]["text"]) == 200
    detail = json.dumps(orders.get_detail(conn, ws, order["id"]), ensure_ascii=False, default=str)
    assert "4111111111111111" not in detail and "4111 1111" not in detail
    assert "cvv" not in detail and "page_html" not in detail and "billing" not in detail
    assert "cookies" not in detail and "session-id" not in detail
    assert stored["customer_id"] == acc["amazon_customer_id"]          # 结算页读到的买家号 ID 留作凭据


def test_non_dict_guard_facts_block_without_storing_raw(conn, workspace):
    acc, dev = _env(conn, workspace)
    fx.assigned_order(conn, workspace["id"], acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    v = execution.submit_guard(conn, dev, a["attempt_id"], ["4111111111111111"])
    assert v["verdict"] == "block"
    stored = fx.attempt_row(conn, a["attempt_id"])["guard_input"]
    assert set(stored) == set(guard.FACT_KEYS) and all(x is None for x in stored.values())


# ── 插件送来的文本里 PostgreSQL 存不了的字符 ─────────────────────────────────

#: NUL（text 与 jsonb 都不收）；半个代理项（页面文字被截断在一个表情中间时会带出来，编不成 UTF-8）
BAD_CHARS = ["\x00", "\ud83d"]


def _last_event(conn, kind):
    return conn.execute("SELECT * FROM procure.events WHERE kind = %s ORDER BY id DESC LIMIT 1", (kind,)).fetchone()


@pytest.mark.parametrize("bad", BAD_CHARS)
def test_unstorable_characters_in_progress_verifying_result_and_finish_text_are_dropped(conn, workspace, bad):
    """进度的阶段与细节、等待验证的说明、结果体里的单号 / 说明 / 配送文字、结束批次的原因与说明：带 NUL 或半个
    代理项时去掉坏字符照常处理（以前这些请求全是 500：进度报不上去，回执也收不下来）。"""
    ws = workspace["id"]
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    out = execution.progress(conn, dev, a["attempt_id"], stage=f" check{bad}out ",
                             detail={f"k{bad}": [f"v{bad}", 1, None, {"深": f"层 😀{bad}"}]})
    assert out == {"continue": True, "cancel_reason": None, "cancel_code": None}
    assert fx.attempt_row(conn, a["attempt_id"])["stage"] == "checkout"
    ev = _last_event(conn, "attempt.progress")["detail"]
    assert ev["stage"] == "checkout" and ev["detail"] == {"k": ["v", 1, None, {"深": "层 😀"}]}
    assert _err(execution.progress, conn, dev, a["attempt_id"], stage=bad).code == "VALIDATION"   # 清完是空的
    fx.to_submitted(conn, dev, a)
    execution.mark_verifying(conn, dev, a["attempt_id"], f"3DS{bad} 验证")
    assert fx.order_row(conn, order["id"])["status_reason"] == "等待银行人工验证：3DS 验证"
    assert _last_event(conn, "attempt.verifying")["detail"] == {"note": "3DS 验证"}
    payload = fx.success_payload(a, order_no=fx.sim_order_no(a) + bad, error_detail=f"备注 😀{bad}",
                                 delivery={"text": f"Arriving 😀{bad} soon", "date": None, "days": 3})
    assert execution.report_result(conn, dev, a["attempt_id"], payload)["order_status"] == "purchased"
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["amazon_order_no"] == fx.sim_order_no(a) and att["delivery_text"] == "Arriving 😀 soon"
    assert execution.report_result(conn, dev, a["attempt_id"], payload)["duplicate"] is True     # 重复上报照样认得
    done = execution.finish_run(conn, dev, run_id, f"fin{bad}ished", {"note": f"done{bad}", f"键{bad}": bad})
    assert done["end_reason"] == "finished"
    ended = _last_event(conn, "run.ended")["detail"]
    assert ended["plugin_reason"] == "finished" and json.loads(ended["plugin_detail"]) == {"note": "done", "键": ""}
    conn.commit()                                                              # 全部写得进库


@pytest.mark.parametrize("bad", BAD_CHARS)
def test_unstorable_characters_in_failure_detail_and_string_finish_detail_are_dropped(conn, workspace, bad):
    ws = workspace["id"]
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    out = execution.report_result(conn, dev, a["attempt_id"],
                                  fx.failure_payload(f"item_un{bad}available", f"Currently unavailable 😀{bad}"))
    assert out["attempt_status"] == "failed"
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["error_code"] == "ITEM_UNAVAILABLE" and att["error_detail"] == "Currently unavailable 😀"
    assert bad not in (fx.order_row(conn, order["id"])["status_reason"] or "")
    execution.finish_run(conn, dev, run_id, "finished", f"  收尾 😀{bad}  ")
    assert _last_event(conn, "run.ended")["detail"]["plugin_detail"] == "收尾 😀"
    conn.commit()


@pytest.mark.parametrize("bad", BAD_CHARS)
def test_guard_stores_and_returns_facts_without_unstorable_characters(conn, workspace, bad):
    """只用来展示、不参与放行的文字（配送说明、配送方）带坏字符：照常判定、放行；存下的事实与返回给插件的裁决里
    已经没有坏字符，提交登记前的复核（用的是存下的事实）照常通过。"""
    ws = workspace["id"]
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    facts = fx.facts_for(conn, order, delivery={**_days(3), "text": f"Arriving 😀{bad} soon"},
                         fulfillment=f"ama{bad}zon")
    v = execution.submit_guard(conn, dev, a["attempt_id"], facts)
    assert v["verdict"] == "pass" and v["fulfillment"] == "amazon"
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] == "guard_passed" and att["guard_input"]["delivery"]["text"] == "Arriving 😀 soon"
    assert att["guard_input"]["fulfillment"] == "amazon"
    json.dumps(v, ensure_ascii=False, default=str).encode("utf-8")              # 写得进 JSON 响应
    assert execution.mark_submitting(conn, dev, a["attempt_id"])["ok"] is True
    conn.commit()


@pytest.mark.parametrize("bad", BAD_CHARS)
@pytest.mark.parametrize("field", ["currency", "customer_id", "asin"])
def test_guard_does_not_repair_compared_values_that_carry_unstorable_characters(conn, workspace, field, bad):
    """要整段比对的值（币种、买家号 ID、ASIN）带坏字符：护栏按原样判定——不会先把坏字符去掉、凑成一个相等的值再
    放行。照样拦截；清理只发生在判定之后，所以拦截原因、存下的事实与裁决、任务上的说明都写得进库。"""
    ws = workspace["id"]
    acc, dev = _env(conn, workspace)
    order = fx.assigned_order(conn, ws, acc)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    facts = fx.facts_for(conn, order)
    assert facts["currency"] == "USD"
    if field == "customer_id":
        facts["customer_id"] = facts["customer_id"][:4] + bad + facts["customer_id"][4:]
    elif field == "asin":
        facts["items"][0]["asin"] = facts["items"][0]["asin"][:3] + bad + facts["items"][0]["asin"][3:]
    else:
        facts[field] = facts[field][:2] + bad + facts[field][2:]
    v = execution.submit_guard(conn, dev, a["attempt_id"], facts)
    assert v["verdict"] == "block", v
    att = fx.attempt_row(conn, a["attempt_id"])
    assert att["status"] in ("failed", "aborted") and att["guard_verdict"]["verdict"] == "block"
    o = fx.order_row(conn, order["id"])
    blob = json.dumps([v, att["guard_input"], att["guard_verdict"], att["error_detail"], o["status_reason"]],
                      ensure_ascii=False, default=str)
    assert bad not in blob
    blob.encode("utf-8")
    conn.commit()

