"""来源（导入 / 飞书同步）→ 执行协议的「暂停」链路端到端（契约 §2「领取后来源变化」、§5 第 7、9 步）：

任务正在执行、尚未登记提交时，上游把人审改成「拒绝」或撤回：

- 新一批文件（与任务同种来源、任务由来源人审通过）带来「拒绝」→ 领取基线发现人审变化 → 进度 continue=false、
  cancel_code SOURCE_CHANGED；提交登记 409 SOURCE_CHANGED；插件报 ABORTED_BY_SERVER → 任务按来源最新内容转
  「待确认」（人审拒绝），解除分配，不能再被领取；
- 任务由控制台人审通过，新一批文件带来「拒绝」→ 来源把暂停写在任务上（conflict kind review）→ cancel_code
  SOURCE_HOLD；提交登记 409 SOURCE_CHANGED（detail.conflict_kind=review）；中止后任务「待核实」（人审冲突，控制台
  通过的结论不被悄悄改写），解除分配，不能再被领取；人工「保留」后才回到可分配；
- 飞书主来源的同一条记录把人审改成拒绝或撤回成未知 → SOURCE_CHANGED → 中止后「待确认」。

全部数据虚构（DEMO 名称、假地址、555 电话、B0DEMO ASIN、虚构飞书记录 id）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services import clock, execution, orders
from tests import factories as f
from tests import factories_exec as fx
from tests.api_helpers import (api, bearer, create_account, err, find_order, good_facts, import_orders,  # noqa: F401
                               import_row, ok, owner, pair_device, plugin_client, view_nos)

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00


# ── HTTP：导入新一批文件 ──────────────────────────────────────────────────────


def _executing_over_http(owner, api, no: str, *, console_review: bool = False):
    acc = create_account(owner)
    if console_review:
        import_orders(owner, [import_row(no, **{"人工审核": None})])
        row = find_order(owner, no)
        assert row["status"] == "pending_review"
        ok(owner.post(f"/api/v1/orders/{row['id']}/review", json={"decision": "approve"}))
        if find_order(owner, no)["status"] != "assigned":
            ok(owner.post(f"/api/v1/orders/{row['id']}/assign", json={"account_id": acc["id"]}))
    else:
        import_orders(owner, [import_row(no)])
    assert find_order(owner, no)["status"] == "assigned"
    plugin = plugin_client(api)
    dev = pair_device(owner, plugin, account=acc)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    assert att is not None and att["upstream_order_no"] == no
    pr = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/progress", json={"stage": "checkout"}, headers=h))
    assert pr == {"continue": True, "cancel_reason": None, "cancel_code": None}
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/guard", json=good_facts(att), headers=h))
    assert find_order(owner, no)["status"] == "executing"
    return {"acc": acc, "plugin": plugin, "h": h, "run_id": run["run_id"], "att": att}


def _stop_and_abort(owner, e, expected_code: str) -> dict:
    plugin, h, aid = e["plugin"], e["h"], e["att"]["attempt_id"]
    pr = ok(plugin.post(f"/plugin/v1/attempts/{aid}/progress", json={"stage": "before_submit"}, headers=h))
    assert pr["continue"] is False and pr["cancel_code"] == expected_code
    body = err(plugin.post(f"/plugin/v1/attempts/{aid}/submitting", headers=h), 409, "SOURCE_CHANGED")
    res = ok(plugin.post(f"/plugin/v1/attempts/{aid}/result",
                         json={"outcome": "failed", "error_code": "ABORTED_BY_SERVER",
                               "error_detail": pr["cancel_reason"]}, headers=h))
    assert res["attempt_status"] == "aborted"
    # 不能再被领取；账号没有因此暂停
    nxt = ok(plugin.post(f"/plugin/v1/runs/{e['run_id']}/next", headers=h))
    assert nxt["attempt"] is None
    assert ok(owner.get(f"/api/v1/accounts/{e['acc']['id']}"))["execution_paused"] is False
    return {"progress": pr, "submitting": body, "result": res}


def test_new_file_batch_rejects_review_mid_attempt_lands_in_pending_review(owner, api):
    no = "PO-DEMO-HB01"
    e = _executing_over_http(owner, api, no)
    out = import_orders(owner, [import_row(no, **{"人工审核": "拒绝", "备注": "DEMO 第二批"})])
    assert out["commit"]
    assert find_order(owner, no)["status"] == "executing"                    # 执行中不改原值
    got = _stop_and_abort(owner, e, "SOURCE_CHANGED")
    assert "人审结论变为「拒绝」" in got["progress"]["cancel_reason"]
    assert got["result"]["order_status"] == "pending_review"
    row = find_order(owner, no)
    detail = ok(owner.get(f"/api/v1/orders/{row['id']}"))
    assert detail["status"] == "pending_review" and detail["review_status"] == "rejected"
    assert detail["assigned_account"] is None and detail["needs_attention"] is True
    assert view_nos(owner, "pending_review") == [no]
    assert any(ev["kind"] == "order.source_changed_during_attempt" for ev in detail["events"])


def test_new_file_batch_rejects_console_approved_order_holds_and_lands_in_review_conflict(owner, api):
    no = "PO-DEMO-HB02"
    e = _executing_over_http(owner, api, no, console_review=True)
    import_orders(owner, [import_row(no, **{"人工审核": "拒绝", "备注": "DEMO 第二批"})])
    row = find_order(owner, no)
    assert row["status"] == "executing"
    got = _stop_and_abort(owner, e, "SOURCE_HOLD")
    assert "来源要求暂停采购" in got["progress"]["cancel_reason"]
    assert got["submitting"]["detail"]["conflict_kind"] == "review"
    assert got["result"]["order_status"] == "conflict"
    detail = ok(owner.get(f"/api/v1/orders/{row['id']}"))
    assert detail["status"] == "conflict" and detail["conflict"]["kind"] == "review"
    assert detail["review_status"] == "approved"                            # 控制台的通过结论没有被悄悄改写
    assert detail["assigned_account"] is None
    # 人工「保留」→ 仍按控制台通过，回到可分配
    after = ok(owner.post(f"/api/v1/orders/{row['id']}/resolve-conflict", json={"choice": "keep"}))
    assert after["status"] in ("queued", "assigned") and after["conflict"] is None


# ── 服务层：飞书主来源撤回人审 ────────────────────────────────────────────────


@pytest.fixture()
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


@pytest.mark.parametrize("review,value", [("rejected", "拒绝"), ("unknown", "待审核")])
def test_feishu_primary_revokes_review_mid_attempt(_frozen, conn, workspace, review, value):
    ws = workspace["id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    src = f.feishu_source(conn, ws, "recDEMOHOLD01")
    order = fx.assigned_order(conn, ws, acc, source=src, upstream_order_no="PO-DEMO-FSREV")
    assert order["status"] == "assigned" and order["primary_source_kind"] == "feishu"
    run_id = fx.start(conn, dev)
    a = fx.claim(conn, dev, run_id)
    fx.to_guard_passed(conn, dev, a)
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-FSREV", review=review, review_value=value),
                  f.feishu_source(conn, ws, "recDEMOHOLD01", connection_id=src.connection_id))
    assert fx.order_row(conn, order["id"])["review_status"] == "approved"
    p = execution.progress(conn, dev, a["attempt_id"], stage="before_submit")
    assert p["continue"] is False and p["cancel_code"] == "SOURCE_CHANGED" and value in p["cancel_reason"]
    with pytest.raises(Exception) as ex:
        execution.mark_submitting(conn, dev, a["attempt_id"])
    assert getattr(ex.value, "code", None) == "SOURCE_CHANGED"
    res = execution.report_result(conn, dev, a["attempt_id"], fx.failure_payload("ABORTED_BY_SERVER", p["cancel_reason"]))
    assert res == {"attempt_status": "aborted", "order_status": "pending_review"}
    o = fx.order_row(conn, order["id"])
    assert o["review_status"] == review and o["review_value"] == value and o["assigned_account_id"] is None
    assert execution.next_attempt(conn, dev, run_id)["attempt"] is None
    assert fx.account_row(conn, acc["id"])["execution_paused"] is False


def test_console_approved_sim_purchase_then_new_batch_rejects_reset_lands_in_review_conflict(_frozen, conn, workspace):
    """已模拟采购期间新一批文件带来「拒绝」（ingest 对已采购的任务只提醒）；清除模拟结果时这条变化落到任务上：
    任务由控制台人审通过 → 人审冲突（待核实），不悄悄改成拒绝、也不带着通过回到可领取。"""
    from services import simulation

    ws, uid = workspace["id"], workspace["user_id"]
    acc = fx.exec_account(conn, ws)
    dev = fx.paired_device(conn, workspace, account=acc)
    o = f.ingest_ready_order(conn, ws, source=f.file_source(conn, ws, 2), upstream_order_no="PO-DEMO-CRST",
                             review="unknown", review_value=None)
    assert o["status"] == "pending_review"
    orders.review(conn, ws, o["id"], "approve", None, actor_user_id=uid)
    if fx.order_row(conn, o["id"])["status"] != "assigned":
        orders.assign_manual(conn, ws, o["id"], acc["id"], actor_user_id=uid)
    a = fx.claim(conn, dev, fx.start(conn, dev))
    fx.complete_success(conn, dev, a)
    assert fx.order_row(conn, o["id"])["status"] == "purchased"
    orders.ingest(conn, ws, f.make_record(upstream_order_no="PO-DEMO-CRST", review="rejected", review_value="拒绝"),
                  f.file_source(conn, ws, 2))
    simulation.reset(conn, ws, uid)
    row = fx.order_row(conn, o["id"])
    assert row["status"] == "conflict" and row["conflict"]["kind"] == "review"
    assert row["review_status"] == "approved" and row["review_source"] == "console"
    assert row["conflict"]["incoming"]["review_status"] == "rejected" and "需人工核实" in row["status_reason"]
    fx.refresh_identity(conn, dev, acc)
    assert execution.next_attempt(conn, dev, fx.start(conn, dev))["attempt"] is None
    orders.resolve_conflict(conn, ws, o["id"], "accept", actor_user_id=uid)
    after = fx.order_row(conn, o["id"])
    assert after["status"] == "pending_review" and after["review_status"] == "rejected"
