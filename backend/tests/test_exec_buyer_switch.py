"""执行中的身份检查（契约 §5）：基准是「这个浏览器最新的登录状态 × 批次账号当前登记的买家号 ID」。

- 执行中途换了买家号（或退出登录、读数不一致、太久没上报）：未登记提交的那一笔在进度 / 提交登记处被拦下并中止，
  任务回已分配、不暂停账号、不占额度；已登记提交的照常收结果。
- 结果上报、结束批次、迟到回执不做身份检查（回执不丢）。
- 护栏的 buyer 项把「谁在下单」钉在结算页：结算页读到的买家号 ID 必须是批次账号登记的那个。
- 提交登记、开始批次先锁浏览器行，和并发的身份上报串行。
全部数据虚构。"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from registry import db
from services import accounts, clock, devices, execution, quota
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


def test_guard_blocks_when_checkout_page_shows_another_buyer(conn, workspace):
    """浏览器上报的登录状态没变，但结算页当场读到的是另一个买家号：护栏拦截，这一单不会被提交。"""
    e = _claimed(conn, workspace)
    aid, cid = e["a"]["attempt_id"], e["acc"]["amazon_customer_id"]
    execution.progress(conn, e["dev"], aid, stage="checkout")
    v = execution.submit_guard(conn, e["dev"], aid, fx.facts_for(conn, e["order"], customer_id=OTHER))
    assert v["verdict"] == "block" and v["code"] == "IDENTITY_MISMATCH"
    assert v["checks"]["buyer"] == {"ok": False, "expected": cid, "actual": OTHER}
    assert v["reasons"] == [f"结算页登录的买家号（{OTHER}）不是这个批次的买家号（{cid}）"]
    att = fx.attempt_row(conn, aid)
    assert att["status"] == "failed" and att["error_code"] == "IDENTITY_MISMATCH" and att["submitted_at"] is None
    assert att["guard_input"]["customer_id"] == OTHER
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == "exception" and "结算页登录的买家号" in o["status_reason"]
    assert not _paused(conn, e) and quota.used(conn, e["acc"]["id"], TODAY, True) == 0
    assert _err(execution.mark_submitting, conn, e["dev"], aid).code == "CONFLICT"


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
    err = _err(execution.mark_submitting, conn, e["dev"], aid)
    assert err.code == "GUARD_BLOCKED" and err.detail["block_code"] == "IDENTITY_MISMATCH"
    assert fx.attempt_row(conn, aid)["submitted_at"] is None


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
