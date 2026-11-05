"""执行协议的停止码与中止码（契约 §5 第 7 步与结果处理表）：

- 进度每个响应都带 cancel_code：continue=true 时为 None，continue=false 时是 8 个码之一（每个码一个用例），
  attempt.progress 事件也记下 cancel_code；
- 插件按 cancel_code 上报中止：UPSTREAM_CANCELLED → ABORTED_UPSTREAM_CANCELLED（任务取消），ATTEMPT_FINISHED →
  不再上报，其余 → ABORTED_BY_SERVER：尝试 aborted、任务回已分配再按派生规则重新校验（来源暂停 / 变化 → 待核实 /
  待确认）、不暂停账号、不占额度；已登记提交后再报 ABORTED_BY_SERVER 仍是结果待核实（铁律 9）；
- 结束批次的 detail 记进 run.ended 事件的 plugin_detail（截到 200 字，非字符串先转成文本）；
- 候选任务被锁时的 retry_after_s 来自 registry 的 claim_busy_retry_s。

全部数据虚构（DEMO 名称、假买家号 ID、假地址、555 电话、B0DEMO ASIN）。
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from registry import db, settings
from services import accounts, clock, devices, execution, orders, quota
from services.models import ERROR_CODES
from tests import factories as f
from tests import factories_exec as fx
from tests.api_helpers import (api, bearer, create_account, err, import_orders, import_row, ok,  # noqa: F401
                               owner, pair_device, plugin_client)

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)
EXT_NO = "111-0000000-0000002"


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _claimed(conn, workspace, no: str) -> dict:
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    src = f.file_source(conn, ws, 2)
    order = fx.assigned_order(conn, ws, acc, source=src, upstream_order_no=no)
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_guard_passed(conn, dev, a)
    assert quota.used(conn, acc["id"], TODAY, True, workspace_id=ws) == 1
    return {"ws": ws, "uid": workspace["user_id"], "acc": acc, "dev": dev, "src": src, "order": order,
            "run_id": run_id, "a": a}


def _reingest(conn, e, **kw):
    rec = f.make_record(upstream_order_no=e["order"]["upstream_order_no"], **kw)
    return orders.ingest(conn, e["ws"], rec, f.file_source(conn, e["ws"], 2, batch_id=e["src"].batch_id))


def _keep_lease(conn, e, until_s: int) -> None:
    for t in range(150, until_s + 1, 150):
        clock.freeze(NOW + timedelta(seconds=t))
        execution.run_heartbeat(conn, e["dev"], e["run_id"])
    clock.freeze(NOW + timedelta(seconds=until_s))
    execution.run_heartbeat(conn, e["dev"], e["run_id"])


def _last_progress_detail(conn, attempt_id: int) -> dict:
    return conn.execute("SELECT detail FROM procure.events WHERE entity = 'attempt' AND entity_id = %s "
                        "AND kind = 'attempt.progress' ORDER BY id DESC LIMIT 1", (attempt_id,)).fetchone()["detail"]


def _pause_markers(conn, account_id: int) -> int:
    return f.count(conn, "events", "entity = 'account' AND entity_id = %s AND kind = %s",
                   (account_id, execution.PAUSE_MARKER_KIND))


def _plugin_abort_code(cancel_code: str) -> str | None:
    """插件的映射（契约 §5）：UPSTREAM_CANCELLED → ABORTED_UPSTREAM_CANCELLED，ATTEMPT_FINISHED → 不报，其余 → ABORTED_BY_SERVER。"""
    if cancel_code == "ATTEMPT_FINISHED":
        return None
    return "ABORTED_UPSTREAM_CANCELLED" if cancel_code == "UPSTREAM_CANCELLED" else "ABORTED_BY_SERVER"


# ── 每个停止原因一个码 ────────────────────────────────────────────────────────


def _cause_upstream(conn, e):
    _reingest(conn, e, upstream_cancelled=True, after_sale_value="DEMO 退款")


def _cause_archived(conn, e):
    conn.execute("UPDATE procure.buyer_accounts SET archived_at = %s WHERE id = %s", (clock.now(), e["acc"]["id"]))


def _cause_paused(conn, e):
    accounts.pause(conn, e["ws"], e["acc"]["id"], "DEMO 人工暂停", actor_kind="user", actor_id=e["uid"])


def _cause_mismatch(conn, e):
    devices.report_identity(conn, e["dev"], fx.identity(fx.customer_id(999_998)))


def _cause_stale(conn, e):
    _keep_lease(conn, e, 601)


def _cause_hold(conn, e):
    hold = {"kind": "review", "source_kind": "feishu", "reason": "飞书来源的人工审核为「拒绝」，需人工核实",
            "fields": {"review_status": ["approved", "rejected"]}, "incoming": {"review_status": "rejected"}}
    conn.execute("UPDATE procure.purchase_orders SET conflict = %s WHERE id = %s", (Jsonb(hold), e["order"]["id"]))


def _cause_changed(conn, e):
    _reingest(conn, e, asin="B0DEMO0X02")


CASES = [
    # (cancel_code, 造原因, 原因里应有的字, 中止后任务状态)
    ("UPSTREAM_CANCELLED", _cause_upstream, "上游已取消", "cancelled"),
    ("ACCOUNT_ARCHIVED", _cause_archived, "账号已归档", "assigned"),
    ("ACCOUNT_PAUSED", _cause_paused, "DEMO 人工暂停", "assigned"),
    ("IDENTITY_MISMATCH", _cause_mismatch, "不符", "assigned"),
    ("IDENTITY_STALE", _cause_stale, "身份信息已过期", "assigned"),
    ("SOURCE_HOLD", _cause_hold, "来源要求暂停采购", "conflict"),
    ("SOURCE_CHANGED", _cause_changed, "ASIN", "conflict"),
]


@pytest.mark.parametrize("code,cause,text,final", CASES, ids=[c[0] for c in CASES])
def test_progress_stop_code_then_plugin_abort(conn, workspace, code, cause, text, final):
    e = _claimed(conn, workspace, f"PO-DEMO-{code[:12]}")
    paused_before = _pause_markers(conn, e["acc"]["id"])
    cause(conn, e)
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["continue"] is False and p["cancel_code"] == code and text in p["cancel_reason"]
    assert set(p) == {"continue", "cancel_reason", "cancel_code"}
    ev = _last_progress_detail(conn, e["a"]["attempt_id"])
    assert ev["continue"] is False and ev["cancel_code"] == code and ev["cancel_reason"] == p["cancel_reason"]

    abort = _plugin_abort_code(code)
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.failure_payload(abort, p["cancel_reason"]))
    assert res == {"attempt_status": "aborted", "order_status": final}
    att = fx.attempt_row(conn, e["a"]["attempt_id"])
    assert att["status"] == "aborted" and att["error_code"] == abort and att["submitted_at"] is None
    o = fx.order_row(conn, e["order"]["id"])
    assert o["status"] == final and o["current_attempt_id"] is None
    if final == "assigned":
        assert o["assigned_account_id"] == e["acc"]["id"]          # 保留分配
    else:
        assert o["assigned_account_id"] is None
    # 中止不暂停账号（ACCOUNT_PAUSED 本来就是人工暂停的，不另加执行模块的暂停）、不占额度
    assert _pause_markers(conn, e["acc"]["id"]) == paused_before
    acc = fx.account_row(conn, e["acc"]["id"])
    assert acc["execution_paused"] is (code == "ACCOUNT_PAUSED")
    assert quota.used(conn, e["acc"]["id"], clock.bj_today(), True, workspace_id=e["ws"]) == 0


def test_progress_on_finished_attempt_says_attempt_finished(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-FIN")
    execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.failure_payload("OUT_OF_STOCK", "DEMO 缺货"))
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="late")
    assert p["continue"] is False and p["cancel_code"] == "ATTEMPT_FINISHED" and "已结束" in p["cancel_reason"]
    assert _plugin_abort_code(p["cancel_code"]) is None


def test_progress_continue_has_null_code_and_event_records_it(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-GO")
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="checkout")
    assert p == {"continue": True, "cancel_reason": None, "cancel_code": None}
    ev = _last_progress_detail(conn, e["a"]["attempt_id"])
    assert ev["continue"] is True and ev["cancel_code"] is None and "cancel_code" in ev


def test_submitted_attempt_always_continues_even_if_account_paused(conn, workspace):
    """已登记提交：不再叫停（cancel_code 为 None），之后报 ABORTED_BY_SERVER 也只能是结果待核实、暂停账号。"""
    e = _claimed(conn, workspace, "PO-DEMO-SUBM")
    execution.mark_submitting(conn, e["dev"], e["a"]["attempt_id"])
    _cause_upstream(conn, e)
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="placed")
    assert p == {"continue": True, "cancel_reason": None, "cancel_code": None}
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.failure_payload("ABORTED_BY_SERVER", "DEMO"))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    assert fx.account_row(conn, e["acc"]["id"])["execution_paused"] is True
    assert quota.used(conn, e["acc"]["id"], TODAY, True, workspace_id=e["ws"]) == 1


def test_cancel_codes_constant_matches_contract():
    assert execution.CANCEL_CODES == ("UPSTREAM_CANCELLED", "ACCOUNT_ARCHIVED", "ACCOUNT_PAUSED", "IDENTITY_MISMATCH",
                                      "IDENTITY_STALE", "SOURCE_HOLD", "SOURCE_CHANGED", "ATTEMPT_FINISHED")
    assert ERROR_CODES["ABORTED_BY_SERVER"].category == "abort"


# ── ABORTED_BY_SERVER：任务回已分配后重新校验 ──────────────────────────────────


def test_aborted_by_server_rederives_review_revocation_to_pending_review(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-ABSREV")
    _reingest(conn, e, review_value="拒绝", review="rejected")
    p = execution.progress(conn, e["dev"], e["a"]["attempt_id"], stage="before_submit")
    assert p["cancel_code"] == "SOURCE_CHANGED"
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"],
                                  fx.failure_payload("ABORTED_BY_SERVER", p["cancel_reason"]))
    assert res == {"attempt_status": "aborted", "order_status": "pending_review"}
    o = fx.order_row(conn, e["order"]["id"])
    assert o["review_status"] == "rejected" and o["assigned_account_id"] is None
    assert fx.account_row(conn, e["acc"]["id"])["execution_paused"] is False
    assert execution.next_attempt(conn, e["dev"], e["run_id"])["attempt"] is None


def test_aborted_by_server_with_upstream_cancel_still_cancels(conn, workspace):
    """插件报错了码（上游取消却报 ABORTED_BY_SERVER）：任务照样取消。"""
    e = _claimed(conn, workspace, "PO-DEMO-ABSCXL")
    _cause_upstream(conn, e)
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.failure_payload("ABORTED_BY_SERVER", "DEMO"))
    assert res == {"attempt_status": "aborted", "order_status": "cancelled"}


def test_aborted_by_server_without_cause_returns_to_assigned_and_is_claimable_again(conn, workspace):
    e = _claimed(conn, workspace, "PO-DEMO-ABSOK")
    res = execution.report_result(conn, e["dev"], e["a"]["attempt_id"], fx.failure_payload("ABORTED_BY_SERVER", "DEMO"))
    assert res == {"attempt_status": "aborted", "order_status": "assigned"}
    o = fx.order_row(conn, e["order"]["id"])
    assert "服务端要求中止" in o["status_reason"] and "任务保留待重新领取" in o["status_reason"]
    b = fx.claim(conn, e["dev"], e["run_id"])
    assert b["order_id"] == e["order"]["id"] and b["kind"] == "auto"
    assert fx.run_row(conn, e["run_id"])["stats"]["aborted"] == 1


def test_aborted_by_server_over_http(owner, api):  # noqa: F811
    acc = create_account(owner)
    import_orders(owner, [import_row("PO-DEMO-HABS")])
    plugin = plugin_client(api)
    dev = pair_device(owner, plugin, account=acc)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    ok(owner.post(f"/api/v1/accounts/{acc['id']}/pause", json={"reason": "DEMO 暂停"}))
    pr = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/progress", json={"stage": "x"}, headers=h))
    assert pr["continue"] is False and pr["cancel_code"] == "ACCOUNT_PAUSED"
    err(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h), 409, "CONFLICT")
    res = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                         json={"outcome": "failed", "error_code": "ABORTED_BY_SERVER",
                               "error_detail": pr["cancel_reason"]}, headers=h))
    assert res == {"attempt_status": "aborted", "order_status": "assigned"}


# ── 结束批次的说明 ────────────────────────────────────────────────────────────


def _run_ended_detail(conn, run_id: int) -> dict:
    return conn.execute("SELECT detail FROM procure.events WHERE entity = 'run' AND entity_id = %s "
                        "AND kind = 'run.ended'", (run_id,)).fetchone()["detail"]


@pytest.mark.parametrize("detail,expected", [
    (None, None),
    ("", None),
    ("  DEMO 插件已停止：额度用完  ", "DEMO 插件已停止：额度用完"),
    ("长" * 300, "长" * 200),
    ({"why": "DEMO", "n": 2}, '{"n": 2, "why": "DEMO"}'),
    ([1, "x" * 400], ('[1, "' + "x" * 400)[:200]),
    (12, "12"),
])
def test_finish_run_records_plugin_detail(conn, workspace, detail, expected):
    acc = fx.exec_account(conn, workspace["id"])
    dev = fx.paired_device(conn, workspace, account=acc)
    run_id = fx.start(conn, dev)
    out = execution.finish_run(conn, dev, run_id, "completed", detail)
    assert out["end_reason"] == "finished"
    d = _run_ended_detail(conn, run_id)
    assert d["plugin_detail"] == expected and d["end_reason"] == "finished" and d["plugin_reason"] == "completed"


def test_finish_run_detail_over_http(owner, api):  # noqa: F811
    acc = create_account(owner)
    plugin = plugin_client(api)
    dev = pair_device(owner, plugin, account=acc)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    out = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish",
                         json={"reason": "completed", "detail": "DEMO 今天没有可执行的任务"}, headers=h))
    assert out["end_reason"] == "finished"
    with db.pg_conn() as conn:
        d = _run_ended_detail(conn, run["run_id"])
    assert d["plugin_detail"] == "DEMO 今天没有可执行的任务"
    run2 = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    ok(plugin.post(f"/plugin/v1/runs/{run2['run_id']}/finish", json={"reason": "stopped"}, headers=h))
    with db.pg_conn() as conn:
        assert _run_ended_detail(conn, run2["run_id"])["plugin_detail"] is None


# ── 候选被锁时的等待秒数来自 registry ──────────────────────────────────────────


def test_claim_busy_retry_comes_from_settings(clean_db, conn, workspace, monkeypatch):
    monkeypatch.setattr(settings, "_loaded", dataclasses.replace(settings.get(), claim_busy_retry_s=7))
    acc = fx.exec_account(conn, workspace["id"])
    dev = fx.paired_device(conn, workspace, account=acc)
    order = fx.assigned_order(conn, workspace["id"], acc)
    run_id = fx.start(conn, dev)
    conn.commit()
    holder = psycopg.connect(clean_db, row_factory=dict_row)
    try:
        holder.execute("SELECT 1 FROM procure.purchase_orders WHERE id = %s FOR UPDATE", (order["id"],))
        with db.pg_conn(clean_db) as c:
            out = execution.next_attempt(c, dev, run_id)
        assert out["attempt"] is None and out["retry_after_s"] == 7 and "7 秒后再领取" in out["reason"]
    finally:
        holder.rollback()
        holder.close()
    assert not hasattr(execution, "CLAIM_BUSY_RETRY_S")


def test_claim_busy_retry_env_default(monkeypatch):
    monkeypatch.delenv("PC_CLAIM_BUSY_RETRY_S", raising=False)
    monkeypatch.setattr(settings, "_loaded", None)
    assert settings.get().claim_busy_retry_s == 5
    monkeypatch.setenv("PC_CLAIM_BUSY_RETRY_S", "9")
    monkeypatch.setattr(settings, "_loaded", None)
    assert settings.get().claim_busy_retry_s == 9
