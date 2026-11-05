"""HTTP：插件执行协议端到端（契约 §5）——配对码 → /plugin/v1/pair → 心跳（形状）→ 身份匹配 → 开始批次 → 领取 →
进度 → 护栏 → 提交登记 → 结果 → 控制台「已采购」视图；同账号第二台设备 → 409 ACCOUNT_BUSY（附持有者）；
护栏拦截 → 异常视图；身份不符拒绝开批次；令牌缺失 / 无效 / 撤销 → 401；插件接口不要求防跨站头；清除模拟结果。
全部数据虚构（DEMO 名称、假买家号 ID、SIM- 单号）。"""

from __future__ import annotations

from tests.api_helpers import (TODAY, api, bearer, create_account, err, find_order, good_facts,  # noqa: F401
                               identity, import_orders, import_row, ok, owner, pair_device, plugin_client, view_nos)

HEARTBEAT_KEYS = {"device", "account", "identity_status", "server_time", "bj_date", "simulation_only", "active_run"}
ACCOUNT_KEYS = {"id", "name", "amazon_customer_id", "purchase_mode", "schedule_times", "effective_max_delivery_days",
                "purchase_enabled", "execution_paused", "pause_reason", "min_amount", "max_amount", "daily_limit",
                "expected_card_last4", "quota_today"}


def _ready(owner, api, n_orders: int = 1, **acc_kw):
    acc = create_account(owner, **acc_kw)
    import_orders(owner, [import_row(f"PO-DEMO-X{i:04d}") for i in range(1, n_orders + 1)])
    plugin = plugin_client(api)
    dev = pair_device(owner, plugin, account=acc)
    return acc, plugin, dev


def test_pair_heartbeat_identity_run_claim_guard_submit_result_purchased(owner, api):
    acc, plugin, dev = _ready(owner, api)
    h = bearer(dev["token"])
    hb = dev["heartbeat"]
    assert set(hb) == HEARTBEAT_KEYS and hb["identity_status"] == "match"
    assert hb["device"] == {"id": dev["device_id"], "name": "Mac-AdsPower-DEMO-1", "bound_account_id": acc["id"]}
    assert set(hb["account"]) == ACCOUNT_KEYS and hb["account"]["min_amount"] == "0.00"
    assert hb["account"]["quota_today"] == {"limit": 10, "used": 0, "planned": 1, "available": 9}
    assert hb["bj_date"] == TODAY.isoformat() and hb["simulation_only"] is True and hb["active_run"] is None

    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    assert run["lease_ttl_s"] == 180 and run["lease_expires_at"]
    hb2 = ok(plugin.post("/plugin/v1/heartbeat", json={"ext_version": "0.1.0"}, headers=h))
    assert hb2["active_run"] == {"id": run["run_id"], "device_id": dev["device_id"],
                                 "device_name": "Mac-AdsPower-DEMO-1", "is_mine": True}
    rhb = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/heartbeat", headers=h))
    assert rhb["stop"] is False and rhb["stop_reason"] is None

    nxt = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))
    att = nxt["attempt"]
    assert att["upstream_order_no"] == "PO-DEMO-X0001" and att["simulated"] is True
    assert att["subtotal"] == "25.00" and att["price_cap"] == "40.00" and att["max_delivery_days"] == 10
    assert att["amount_range"] == {"min": "0.00", "max": "100.00"} and att["postal_code"] == "32960"
    err(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h), 409, "ATTEMPT_IN_FLIGHT")
    assert find_order(owner, "PO-DEMO-X0001")["status"] == "executing"

    pr = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/progress",
                        json={"stage": "checkout", "detail": {"page": "spc"}}, headers=h))
    assert pr == {"continue": True, "cancel_reason": None, "cancel_code": None}
    err(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h), 409, "CONFLICT")  # 护栏前

    verdict = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/guard", json=good_facts(att), headers=h))
    assert verdict["verdict"] == "pass" and verdict["code"] is None and verdict["checks"]

    sub = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h))
    assert sub["ok"] is True and sub["submitted_at"]

    no = f"SIM-{TODAY:%Y%m%d}-{att['attempt_id']}"
    result = {"outcome": "succeeded", "amazon_order_no": no,
              "amounts": {"subtotal": "25.00", "shipping": "0.00", "tax": "1.50", "total_before_tax": "25.00",
                          "total": "26.50"},
              "card_last4": "0000", "delivery": {"text": "Arriving (DEMO)", "date": None, "days": 3},
              "error_code": None, "error_detail": None}
    res = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result", json=result, headers=h))
    assert res == {"attempt_status": "succeeded", "order_status": "purchased"}
    again = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result", json=result, headers=h))
    assert again["duplicate"] is True                                  # 重复上报原样返回

    assert view_nos(owner, "purchased") == ["PO-DEMO-X0001"]
    row = find_order(owner, "PO-DEMO-X0001")
    assert row["latest_attempt"]["amazon_order_no"] == no and row["latest_attempt"]["total"] == "26.50"
    assert row["latest_attempt"]["simulated"] is True
    detail = ok(owner.get(f"/api/v1/orders/{row['id']}"))
    assert detail["attempts"][0]["status"] == "succeeded" and "mark_amazon_cancelled" in detail["allowed_actions"]

    empty = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))
    assert empty["attempt"] is None and empty["reason"]
    fin = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish", json={"reason": "finished"}, headers=h))
    assert fin["end_reason"] == "finished" and fin["ended_at"]
    err(plugin.post(f"/plugin/v1/runs/{run['run_id']}/heartbeat", headers=h), 409, "LEASE_LOST")

    quota = ok(owner.get(f"/api/v1/accounts/{acc['id']}"))["quota_today"]
    assert quota["used"] == 1 and quota["limit"] == 10


def test_second_device_on_same_account_gets_account_busy_with_holder(owner, api):
    acc, plugin, dev1 = _ready(owner, api)
    dev2 = pair_device(owner, plugin, name="Win-Chrome-DEMO-2", account=acc)
    assert dev2["heartbeat"]["identity_status"] == "match"
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True},
                         headers=bearer(dev1["token"])))
    body = err(plugin.post("/plugin/v1/runs", json={"trigger": "scheduled", "simulated": True},
                           headers=bearer(dev2["token"])), 409, "ACCOUNT_BUSY")
    assert body["detail"]["holder_device_id"] == dev1["device_id"]
    assert body["detail"]["holder_device_name"] == "Mac-AdsPower-DEMO-1" and body["detail"]["since"]
    hb = ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=bearer(dev2["token"])))
    assert hb["active_run"]["id"] == run["run_id"] and hb["active_run"]["is_mine"] is False
    acc_view = ok(owner.get(f"/api/v1/accounts/{acc['id']}"))
    assert acc_view["active_run"]["device_id"] == dev1["device_id"]


def test_guard_block_puts_order_into_exception_view(owner, api):
    acc, plugin, dev = _ready(owner, api)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    over = good_facts(att, tax="20.00", total="45.00")                  # 总额 45 > 限价 40
    verdict = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/guard", json=over, headers=h))
    assert verdict["verdict"] == "block" and verdict["code"] == "PRICE_OVER_CAP" and verdict["reasons"]
    err(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h), 409, "CONFLICT")

    assert view_nos(owner, "exception") == ["PO-DEMO-X0001"]
    row = find_order(owner, "PO-DEMO-X0001")
    assert row["status"] == "exception" and row["status_reason"]
    assert row["latest_attempt"]["status"] == "failed" and row["latest_attempt"]["error_code"] == "PRICE_OVER_CAP"
    detail = ok(owner.get(f"/api/v1/orders/{row['id']}"))
    assert {"resume", "override", "cancel"} <= set(detail["allowed_actions"])
    ov = ok(owner.get("/api/v1/overview"))
    assert [r["upstream_order_no"] for r in ov["recent_exceptions"]] == ["PO-DEMO-X0001"]
    resumed = ok(owner.post(f"/api/v1/orders/{row['id']}/resume", json={"note": "DEMO 已核对价格"}))
    assert resumed["status"] == "assigned"


def test_identity_mismatch_and_real_purchase_are_refused(owner, api):
    acc, plugin, dev = _ready(owner, api)
    h = bearer(dev["token"])
    out = ok(plugin.post("/plugin/v1/identity", json=identity("A1DEMO999999"), headers=h))
    assert out == {"identity_status": "mismatch"}
    err(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h), 403,
        "IDENTITY_MISMATCH")
    assert ok(owner.get(f"/api/v1/devices/{dev['device_id']}"))["identity_status"] == "mismatch"
    ok(plugin.post("/plugin/v1/heartbeat", json={"identity": identity(acc["amazon_customer_id"])}, headers=h))
    err(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": False}, headers=h), 403,
        "REAL_PURCHASE_DISABLED")
    err(plugin.post("/plugin/v1/runs", json={"trigger": "manual"}, headers=h), 422, "VALIDATION")


def test_unbound_device_cannot_start_a_run(owner, api):
    plugin = plugin_client(api)
    dev = pair_device(owner, plugin)
    assert dev["heartbeat"]["account"] is None and dev["heartbeat"]["identity_status"] == "unknown"
    err(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=bearer(dev["token"])),
        403, "NOT_BOUND")


def test_plugin_requires_a_valid_bearer_token_but_no_csrf_header(owner, api):
    acc, plugin, dev = _ready(owner, api)
    err(plugin.post("/plugin/v1/heartbeat", json={}), 401, "UNAUTHORIZED")
    err(plugin.post("/plugin/v1/heartbeat", json={}, headers={"Authorization": "Bearer not-a-real-token"}),
        401, "UNAUTHORIZED")
    err(plugin.post("/plugin/v1/heartbeat", json={}, headers={"Authorization": f"Token {dev['token']}"}),
        401, "UNAUTHORIZED")
    err(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}), 401, "UNAUTHORIZED")
    ok(plugin.post("/plugin/v1/heartbeat", json={}, headers=bearer(dev["token"])))      # 无 X-Requested-With 也可以
    # 控制台的会话 Cookie 不能代替设备令牌
    err(owner.post("/plugin/v1/heartbeat", json={}), 401, "UNAUTHORIZED")
    ok(owner.post(f"/api/v1/devices/{dev['device_id']}/revoke"))
    err(plugin.post("/plugin/v1/heartbeat", json={}, headers=bearer(dev["token"])), 401, "UNAUTHORIZED")
    err(plugin.post("/plugin/v1/pair", json={"code": "ZZZZZZZZ", "device_name": "x", "browser": "chrome",
                                             "ext_version": "0.1.0"}), 422, "VALIDATION")


def test_result_unknown_after_submit_pauses_account_and_resolve_via_console(owner, api):
    acc, plugin, dev = _ready(owner, api)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/guard", json=good_facts(att), headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h))
    ver = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/verifying", json={"note": "DEMO 银行验证"},
                         headers=h))
    assert ver == {"ok": True, "attempt_status": "verifying"}
    res = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                         json={"outcome": "unknown", "error_code": "NETWORK", "error_detail": "DEMO 断网"}, headers=h))
    assert res == {"attempt_status": "unknown", "order_status": "result_unknown"}
    assert view_nos(owner, "result_unknown") == ["PO-DEMO-X0001"]
    assert ok(owner.get(f"/api/v1/accounts/{acc['id']}"))["execution_paused"] is True

    row = find_order(owner, "PO-DEMO-X0001")
    d = ok(owner.post(f"/api/v1/orders/{row['id']}/resolve-unknown", json={"outcome": "not_ordered", "note": "DEMO"}))
    assert d["status"] == "assigned"


def test_simulation_reset_restores_orders_and_resumes_accounts(owner, api):
    acc, plugin, dev = _ready(owner, api, n_orders=2)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    a1 = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    ok(plugin.post(f"/plugin/v1/attempts/{a1['attempt_id']}/guard", json=good_facts(a1), headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{a1['attempt_id']}/submitting", headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{a1['attempt_id']}/result",
                   json={"outcome": "succeeded", "amazon_order_no": f"SIM-{TODAY:%Y%m%d}-{a1['attempt_id']}",
                         "amounts": {"total": "25.00"}}, headers=h))
    a2 = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    ok(plugin.post(f"/plugin/v1/attempts/{a2['attempt_id']}/guard", json=good_facts(a2), headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{a2['attempt_id']}/submitting", headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{a2['attempt_id']}/result", json={"outcome": "unknown"}, headers=h))
    assert ok(owner.get(f"/api/v1/accounts/{acc['id']}"))["execution_paused"] is True

    out = ok(owner.post("/api/v1/simulation/reset"))
    assert out["attempts_reset"] == 2 and out["runs_ended"] == [run["run_id"]]
    assert sorted(out["orders_restored"]) == sorted(r["id"] for r in ok(owner.get("/api/v1/orders"))["rows"])
    assert out["accounts_resumed"] == [acc["id"]] and out["orders_skipped"] == []
    assert view_nos(owner, "purchased") == [] and view_nos(owner, "result_unknown") == []
    assert sorted(view_nos(owner, "today")) == ["PO-DEMO-X0001", "PO-DEMO-X0002"]
    acc_view = ok(owner.get(f"/api/v1/accounts/{acc['id']}"))
    assert acc_view["execution_paused"] is False and acc_view["quota_today"]["used"] == 0


def test_late_success_after_sweep_is_recorded_and_answered_with_409(owner, api):
    """清扫后才送到的成功回执：回 409 CONFLICT（detail.late_receipt_recorded），但回执已提交入库，
    控制台核实时能看到单号；任务保持结果待核实。"""
    from datetime import timedelta

    from services import clock
    from tests.api_helpers import NOW
    from workflows import sweep as sweep_wf

    acc, plugin, dev = _ready(owner, api)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/guard", json=good_facts(att), headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h))
    clock.freeze(NOW + timedelta(seconds=181))
    assert sweep_wf.run()["unknown"] == 1
    no = f"SIM-{TODAY:%Y%m%d}-{att['attempt_id']}"
    body = err(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                           json={"outcome": "succeeded", "amazon_order_no": no, "amounts": {"total": "25.00"}},
                           headers=h), 409, "CONFLICT")
    assert body["detail"] == {"attempt_status": "unknown", "order_status": "result_unknown",
                              "late_receipt_recorded": True}
    row = find_order(owner, "PO-DEMO-X0001")
    assert row["status"] == "result_unknown"
    detail = ok(owner.get(f"/api/v1/orders/{row['id']}"))
    assert detail["attempts"][0]["status"] == "unknown" and detail["attempts"][0]["amazon_order_no"] == no
    assert any(e["kind"] == "attempt.late_result" and e["detail"]["amazon_order_no"] == no for e in detail["events"])
    assert ok(owner.get(f"/api/v1/accounts/{acc['id']}"))["execution_paused"] is True


def test_guard_facts_are_stored_sanitized(owner, api):
    acc, plugin, dev = _ready(owner, api)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    facts = good_facts(att, card_last4="4111 1111 1111 1111", billing={"card_number": "4111111111111111", "cvv": "123"})
    assert ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/guard", json=facts, headers=h))["verdict"] == "pass"
    row = find_order(owner, "PO-DEMO-X0001")
    resp = owner.get(f"/api/v1/orders/{row['id']}")
    stored = ok(resp)["attempts"][0]["guard_input"]
    assert stored["card_last4"] == "1111" and "billing" not in stored
    assert "4111111111111111" not in resp.text and "cvv" not in resp.text


def test_upstream_change_after_claim_stops_submit_over_http(owner, api):
    """领取后重新导入：内容相同不算变化；ASIN 被改 → 进度 continue=false、提交登记 409 SOURCE_CHANGED，
    插件中止后任务进待核实。"""
    acc, plugin, dev = _ready(owner, api)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/guard", json=good_facts(att), headers=h))
    import_orders(owner, [import_row("PO-DEMO-X0001", **{"备注": "DEMO 第二批"})])             # 内容相同：不算变化
    pr = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/progress", json={"stage": "x"}, headers=h))
    assert pr["continue"] is True
    import_orders(owner, [import_row("PO-DEMO-X0001", **{"ASIN": "B0DEMO0X01"})])
    pr = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/progress", json={"stage": "y"}, headers=h))
    assert pr["continue"] is False and "ASIN" in pr["cancel_reason"]
    err(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h), 409, "SOURCE_CHANGED")
    res = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                         json={"outcome": "failed", "error_code": "ABORTED_UPSTREAM_CANCELLED",
                               "error_detail": pr["cancel_reason"]}, headers=h))
    assert res == {"attempt_status": "aborted", "order_status": "conflict"}
    assert view_nos(owner, "conflict") == ["PO-DEMO-X0001"]


def _sim_attempt_guarded(plugin, h):
    run = ok(plugin.post("/plugin/v1/runs", json={"trigger": "manual", "simulated": True}, headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/guard", json=good_facts(att), headers=h))
    return run, att


def _partial_import(owner, no):
    """只有「平台订单号」「人工审核」两列的新文件（其他列都没带）。"""
    from tests.api_helpers import xlsx_bytes

    return import_orders(owner, data=xlsx_bytes([{"平台订单号": no, "人工审核": "通过"}],
                                                headers=["平台订单号", "人工审核"]))


def test_late_success_after_console_resolved_not_ordered_is_recorded_and_answered_with_409(owner, api):
    """D1-05：控制台核实未下单之后才送到的成功回执：409 CONFLICT（detail.late_receipt_recorded），回执已提交入库
    （attempt.late_result，contradicts），任务置提醒；尝试仍是失败 / 未下单。"""
    from datetime import timedelta

    from services import clock
    from tests.api_helpers import NOW
    from workflows import sweep as sweep_wf

    acc, plugin, dev = _ready(owner, api)
    h = bearer(dev["token"])
    _, att = _sim_attempt_guarded(plugin, h)
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h))
    clock.freeze(NOW + timedelta(seconds=181))
    assert sweep_wf.run()["unknown"] == 1
    row = find_order(owner, "PO-DEMO-X0001")
    ok(owner.post(f"/api/v1/orders/{row['id']}/resolve-unknown", json={"outcome": "not_ordered", "note": "DEMO"}))
    no = f"SIM-{TODAY:%Y%m%d}-{att['attempt_id']}"
    body = err(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                           json={"outcome": "succeeded", "amazon_order_no": no, "amounts": {"total": "25.00"}},
                           headers=h), 409, "CONFLICT")
    assert body["detail"] == {"attempt_status": "failed", "order_status": "assigned", "late_receipt_recorded": True}
    detail = ok(owner.get(f"/api/v1/orders/{row['id']}"))
    assert detail["needs_attention"] is True and no in detail["attention_note"]
    assert detail["attempts"][0]["status"] == "failed" and detail["attempts"][0]["amazon_order_no"] is None
    late = [e for e in detail["events"] if e["kind"] == "attempt.late_result"]
    assert len(late) == 1 and late[0]["detail"]["contradicts"] is True and late[0]["detail"]["amazon_order_no"] == no
    # 插件补交同一份回执：仍回 409 late_receipt_recorded（插件据此不再补交），不重复记录
    again = err(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                            json={"outcome": "succeeded", "amazon_order_no": no, "amounts": {"total": "25.00"}},
                            headers=h), 409, "CONFLICT")
    assert again["detail"]["late_receipt_recorded"] is True
    detail = ok(owner.get(f"/api/v1/orders/{row['id']}"))
    assert len([e for e in detail["events"] if e["kind"] == "attempt.late_result"]) == 1


def test_partial_import_during_an_attempt_is_not_a_source_change(owner, api):
    """NEW-D2-execution-safety-1：领取后导入只有平台订单号与人工审核的文件：没带的列不算来源变化。"""
    acc, plugin, dev = _ready(owner, api)
    h = bearer(dev["token"])
    _, att = _sim_attempt_guarded(plugin, h)
    _partial_import(owner, "PO-DEMO-X0001")
    pr = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/progress", json={"stage": "x"}, headers=h))
    assert pr == {"continue": True, "cancel_reason": None, "cancel_code": None}
    assert ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h))["ok"] is True


def test_partial_import_between_sim_success_and_reset_restores_assigned(owner, api):
    acc, plugin, dev = _ready(owner, api)
    h = bearer(dev["token"])
    _, att = _sim_attempt_guarded(plugin, h)
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                   json={"outcome": "succeeded", "amazon_order_no": f"SIM-{TODAY:%Y%m%d}-{att['attempt_id']}",
                         "amounts": {"total": "25.00"}}, headers=h))
    assert find_order(owner, "PO-DEMO-X0001")["status"] == "purchased"
    _partial_import(owner, "PO-DEMO-X0001")
    ok(owner.post("/api/v1/simulation/reset"))
    row = find_order(owner, "PO-DEMO-X0001")
    detail = ok(owner.get(f"/api/v1/orders/{row['id']}"))
    assert detail["status"] == "assigned" and detail["conflict"] is None
    assert detail["original"]["asin"] == "B0DEMO0001" and detail["original"]["price_cap"] == "40.00"
