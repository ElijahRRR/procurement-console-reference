"""账号分配（契约 §3.2）：保底匹配、优先级填充、开关与暂停、额度占用、人工固定分配、跨日。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services import allocation, clock, orders
from services.errors import DomainError
from tests import factories as f

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _order(conn, ws, price, **kw):
    return f.ingest_ready_order(conn, ws, unit_price=Decimal(price), price_cap=Decimal(price) + 20, **kw)


def _assigned(conn, ws, order_id):
    row = orders.get_row(conn, ws, order_id)
    return row["assigned_account_id"], row["assignment_note"]


def test_ab_example_a_gets_25_b_gets_45(conn, workspace):
    """v1 §3.2 / §10：A 只接 20–30，B 接 20–50，25 与 45 两单 → A 得 25，B 得 45（即使 B 优先级更高）。"""
    ws = workspace["id"]
    a = f.make_account(conn, ws, name="A-DEMO", min_amount="20", max_amount="30", priority=2)
    b = f.make_account(conn, ws, name="B-DEMO", min_amount="20", max_amount="50", priority=9)
    o25 = _order(conn, ws, "25.00")
    o45 = _order(conn, ws, "45.00")
    s = allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, o25["id"]) == (a["id"], allocation.NOTE_COVERAGE)
    assert _assigned(conn, ws, o45["id"]) == (b["id"], allocation.NOTE_COVERAGE)
    assert s["stats"] == {"considered": 2, "assigned": 2, "coverage": 2, "fill": 0, "unassigned": 0}
    assert s["uncovered"] == []


def test_ab_example_order_independent(conn, workspace):
    """45 的单先出现、B 的名称排在前面也不影响结果（增广路）。"""
    ws = workspace["id"]
    b = f.make_account(conn, ws, name="A-wide", min_amount="20", max_amount="50", priority=9)
    a = f.make_account(conn, ws, name="Z-narrow", min_amount="20", max_amount="30", priority=0)
    o45 = _order(conn, ws, "45.00")
    o25 = _order(conn, ws, "25.00")
    allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, o25["id"])[0] == a["id"]
    assert _assigned(conn, ws, o45["id"])[0] == b["id"]


def test_coverage_then_priority_fill(conn, workspace):
    ws = workspace["id"]
    hi = f.make_account(conn, ws, name="HI-DEMO", priority=5)
    lo1 = f.make_account(conn, ws, name="LO1-DEMO", priority=2)
    lo2 = f.make_account(conn, ws, name="LO2-DEMO", priority=2)
    ords = [_order(conn, ws, "25.00") for _ in range(5)]
    s = allocation.allocate(conn, ws, trigger="manual")
    got = [_assigned(conn, ws, o["id"]) for o in ords]
    per = {acc: sum(1 for g in got if g[0] == acc) for acc in (hi["id"], lo1["id"], lo2["id"])}
    assert per == {hi["id"]: 3, lo1["id"]: 1, lo2["id"]: 1}
    notes = [g[1] for g in got]
    assert notes.count(allocation.NOTE_COVERAGE) == 3 and notes.count(allocation.NOTE_FILL) == 2
    assert s["stats"]["coverage"] == 3 and s["stats"]["fill"] == 2


def test_same_priority_fill_goes_by_name(conn, workspace):
    ws = workspace["id"]
    b = f.make_account(conn, ws, name="BETA-DEMO", priority=3)
    a = f.make_account(conn, ws, name="ALPHA-DEMO", priority=3)
    ords = [_order(conn, ws, "25.00") for _ in range(3)]
    allocation.allocate(conn, ws, trigger="manual")
    got = [_assigned(conn, ws, o["id"]) for o in ords]
    fill = [g[0] for g in got if g[1] == allocation.NOTE_FILL]
    assert fill == [a["id"]]
    assert {g[0] for g in got if g[1] == allocation.NOTE_COVERAGE} == {a["id"], b["id"]}


def test_unassigned_reasons_out_of_range_and_capacity(conn, workspace):
    ws = workspace["id"]
    f.make_account(conn, ws, name="ONE-DEMO", min_amount="20", max_amount="30", daily_limit=1)
    o1 = _order(conn, ws, "25.00")
    o2 = _order(conn, ws, "26.00")
    o3 = _order(conn, ws, "80.00")
    s = allocation.allocate(conn, ws, trigger="manual")
    r2 = orders.get_row(conn, ws, o2["id"])
    r3 = orders.get_row(conn, ws, o3["id"])
    assert orders.get_row(conn, ws, o1["id"])["status"] == "assigned"
    assert r2["status"] == "queued" and r2["status_reason"] == allocation.REASON_FULL
    assert r3["status"] == "queued" and r3["status_reason"] == "金额 $80.00 不在任何启用账号的区间内"
    assert {u["order_id"]: u["reason"] for u in s["unassigned"]} == {
        o2["id"]: allocation.REASON_FULL, o3["id"]: "金额 $80.00 不在任何启用账号的区间内"}


def test_uncovered_account_reasons(conn, workspace):
    ws = workspace["id"]
    f.make_account(conn, ws, name="A-DEMO", min_amount="20", max_amount="30")
    f.make_account(conn, ws, name="B-DEMO", min_amount="100", max_amount="200")
    f.make_account(conn, ws, name="C-DEMO", daily_limit=0)
    f.make_account(conn, ws, name="D-DEMO", min_amount="20", max_amount="30")
    _order(conn, ws, "25.00")
    s = allocation.allocate(conn, ws, trigger="manual")
    reasons = {u["name"]: u["reason"] for u in s["uncovered"]}
    assert reasons == {"B-DEMO": allocation.UNCOVERED_NO_MATCH, "C-DEMO": allocation.UNCOVERED_NO_QUOTA,
                       "D-DEMO": allocation.UNCOVERED_NOT_ENOUGH}
    assert all(u["date"] == TODAY.isoformat() for u in s["uncovered"])


def test_no_accounts_reason(conn, workspace):
    ws = workspace["id"]
    o = _order(conn, ws, "25.00")
    allocation.allocate(conn, ws, trigger="manual")
    assert orders.get_row(conn, ws, o["id"])["status_reason"] == allocation.REASON_NO_ACCOUNT


def test_purchase_disabled_gets_no_new_assignment_but_keeps_existing(conn, workspace):
    """v1 §10：关闭账号采购开关 → 无新分配，已有任务保留。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws, name="ONLY-DEMO")
    first = _order(conn, ws, "25.00")
    allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, first["id"])[0] == acc["id"]
    conn.execute("UPDATE procure.buyer_accounts SET purchase_enabled = false WHERE id = %s", (acc["id"],))
    second = _order(conn, ws, "25.00")
    allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, first["id"])[0] == acc["id"]
    assert orders.get_row(conn, ws, second["id"])["status"] == "queued"


def test_paused_account_not_allocated_existing_kept(conn, workspace):
    ws = workspace["id"]
    paused = f.make_account(conn, ws, name="P-DEMO", priority=9)
    other = f.make_account(conn, ws, name="Q-DEMO", priority=1)
    o1 = _order(conn, ws, "25.00")
    allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, o1["id"])[0] == paused["id"]
    conn.execute("UPDATE procure.buyer_accounts SET execution_paused = true WHERE id = %s", (paused["id"],))
    o2 = _order(conn, ws, "25.00")
    o3 = _order(conn, ws, "25.00")
    allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, o1["id"])[0] == paused["id"]
    assert _assigned(conn, ws, o2["id"])[0] == other["id"] and _assigned(conn, ws, o3["id"])[0] == other["id"]


def test_archived_account_orders_are_reallocated(conn, workspace):
    ws = workspace["id"]
    gone = f.make_account(conn, ws, name="GONE-DEMO", priority=9)
    keep = f.make_account(conn, ws, name="KEEP-DEMO", priority=1)
    o = _order(conn, ws, "25.00")
    o2 = _order(conn, ws, "25.00")
    allocation.allocate(conn, ws, trigger="manual")
    orders.assign_manual(conn, ws, o2["id"], gone["id"], actor_user_id=workspace["user_id"])
    conn.execute("UPDATE procure.buyer_accounts SET archived_at = %s WHERE id = %s", (clock.now(), gone["id"]))
    allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, o["id"])[0] == keep["id"]
    assert _assigned(conn, ws, o2["id"])[0] == keep["id"]


def test_daily_limit_and_inflight_and_unknown_attempts_consume_capacity(conn, workspace):
    """v1 §10：达到每日额度或额度被在途 / 待核实占用 → 不再分配；失败的尝试释放额度。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws, name="CAP-DEMO", daily_limit=3)
    busy = [_order(conn, ws, "25.00") for _ in range(3)]
    f.make_attempt(conn, ws, busy[0]["id"], acc["id"], status="claimed")
    f.make_attempt(conn, ws, busy[1]["id"], acc["id"], status="unknown")
    f.make_attempt(conn, ws, busy[2]["id"], acc["id"], status="failed")
    for b in busy:
        f.set_status(conn, b["id"], "exception")
    new1 = _order(conn, ws, "25.00")
    new2 = _order(conn, ws, "25.00")
    s = allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, new1["id"])[0] == acc["id"]
    assert orders.get_row(conn, ws, new2["id"])["status"] == "queued"
    assert s["stats"]["assigned"] == 1 and s["stats"]["unassigned"] == 1


def test_real_attempts_do_not_consume_simulated_capacity(conn, workspace):
    """额度按工作区模拟模式统计：模拟阶段只数模拟尝试。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws, daily_limit=1)
    old = _order(conn, ws, "25.00")
    f.make_attempt(conn, ws, old["id"], acc["id"], status="succeeded", simulated=False)
    f.set_status(conn, old["id"], "purchased")
    new = _order(conn, ws, "25.00")
    allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, new["id"])[0] == acc["id"]


def test_attempt_at_2359_beijing_counts_for_that_day_only(conn, workspace):
    """午夜跨日：北京 23:59 创建的尝试只占当天额度，不扣新一天。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws, daily_limit=1)
    clock.freeze(datetime(2026, 10, 31, 15, 59, tzinfo=timezone.utc))      # 北京 10-31 23:59
    o1 = _order(conn, ws, "25.00")
    att = f.make_attempt(conn, ws, o1["id"], acc["id"], status="unknown")
    f.set_status(conn, o1["id"], "result_unknown", assigned_account_id=acc["id"], assignment_kind="auto")
    assert att["bj_date"] == date(2026, 10, 31)
    o2 = _order(conn, ws, "25.00")
    allocation.allocate(conn, ws, trigger="manual")
    assert orders.get_row(conn, ws, o2["id"])["status"] == "queued"
    clock.freeze(datetime(2026, 10, 31, 16, 1, tzinfo=timezone.utc))       # 北京 11-01 00:01
    s = allocation.allocate(conn, ws, trigger="daily")
    assert s["bj_date"] == "2026-11-01"
    assert _assigned(conn, ws, o2["id"])[0] == acc["id"]
    # 跨零点的待核实尝试仍是同一笔任务的唯一尝试（防重复保护照常）
    assert orders.get_row(conn, ws, o1["id"])["status"] == "result_unknown"


def test_manual_assignment_is_fixed_and_counts_against_capacity(conn, workspace):
    ws = workspace["id"]
    a = f.make_account(conn, ws, name="A-DEMO", daily_limit=1, priority=9)
    b = f.make_account(conn, ws, name="B-DEMO", daily_limit=5, priority=1)
    o1 = _order(conn, ws, "25.00")
    o2 = _order(conn, ws, "25.00")
    orders.assign_manual(conn, ws, o1["id"], b["id"], actor_user_id=workspace["user_id"])
    orders.assign_manual(conn, ws, o2["id"], a["id"], actor_user_id=workspace["user_id"])
    o3 = _order(conn, ws, "25.00")
    s = allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, o1["id"]) == (b["id"], "人工改派")
    assert _assigned(conn, ws, o2["id"]) == (a["id"], "人工改派")
    assert _assigned(conn, ws, o3["id"])[0] == b["id"]            # A 已被人工分配占满
    assert s["stats"]["considered"] == 1


def test_future_orders_planned_for_their_own_date(conn, workspace):
    """未来日期的任务按计划日期分配，不消耗今天的额度。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws, daily_limit=1)
    today = _order(conn, ws, "25.00")
    tomorrow = _order(conn, ws, "25.00", suggested_purchase_date=TODAY + timedelta(days=1))
    overdue = _order(conn, ws, "25.00", suggested_purchase_date=TODAY - timedelta(days=3))
    s = allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, tomorrow["id"])[0] == acc["id"]
    assigned_today = [o for o in (today, overdue) if orders.get_row(conn, ws, o["id"])["status"] == "assigned"]
    assert len(assigned_today) == 1 and assigned_today[0]["id"] == overdue["id"]   # 过期的先（建议日期更早）
    assert s["stats"]["assigned"] == 2


def test_allocation_run_recorded_and_last_run(conn, workspace):
    ws = workspace["id"]
    assert allocation.last_run(conn, ws) is None
    f.make_account(conn, ws)
    _order(conn, ws, "25.00")
    s = allocation.allocate(conn, ws, trigger="import")
    last = allocation.last_run(conn, ws)
    assert last == s
    assert set(last) == {"id", "bj_date", "at", "trigger", "stats", "uncovered", "unassigned"}
    assert last["trigger"] == "import" and last["bj_date"] == TODAY.isoformat()
    with pytest.raises(DomainError) as e:
        allocation.allocate(conn, ws, trigger="bogus")
    assert e.value.code == "VALIDATION"


def test_reallocation_keeps_in_flight_orders(conn, workspace):
    ws = workspace["id"]
    a = f.make_account(conn, ws, name="A-DEMO")
    o = _order(conn, ws, "25.00")
    allocation.allocate(conn, ws, trigger="manual")
    f.make_attempt(conn, ws, o["id"], a["id"], status="checking")
    f.set_status(conn, o["id"], "executing")
    f.make_account(conn, ws, name="0-FIRST", priority=9)
    s = allocation.allocate(conn, ws, trigger="manual")
    assert s["stats"]["considered"] == 0
    assert _assigned(conn, ws, o["id"])[0] == a["id"]


def test_check_manual_assignment_validation(conn, workspace):
    ws = workspace["id"]
    narrow = f.make_account(conn, ws, name="N-DEMO", min_amount="20", max_amount="30", daily_limit=1)
    off = f.make_account(conn, ws, name="OFF-DEMO", purchase_enabled=False)
    o = _order(conn, ws, "45.00")
    with pytest.raises(DomainError) as e:
        allocation.check_manual_assignment(conn, ws, o, narrow, TODAY)
    assert e.value.code == "VALIDATION" and "不在账号 N-DEMO 的区间" in e.value.message
    with pytest.raises(DomainError) as e:
        allocation.check_manual_assignment(conn, ws, o, off, TODAY)
    assert "采购开关" in e.value.message
    ok = _order(conn, ws, "25.00")
    allocation.check_manual_assignment(conn, ws, ok, narrow, TODAY)
    orders.assign_manual(conn, ws, ok["id"], narrow["id"], actor_user_id=workspace["user_id"])
    another = _order(conn, ws, "26.00")
    with pytest.raises(DomainError) as e:
        allocation.check_manual_assignment(conn, ws, another, narrow, TODAY)
    assert "额度已满" in e.value.message
    # 已分配在该账号的任务再次改派到同一账号不重复计算
    allocation.check_manual_assignment(conn, ws, orders.get_row(conn, ws, ok["id"]), narrow, TODAY)


def test_amount_range_endpoints_inclusive_and_48_dollar_example(conn, workspace):
    """v1 §3.3 / §10：商品小计 48 美元（结算 52、限价 50）仍匹配 20–50 的账号；区间端点含。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws, name="R-DEMO", min_amount="20", max_amount="50")
    o48 = f.ingest_ready_order(conn, ws, unit_price=Decimal("48.00"), price_cap=Decimal("50.00"))
    o20 = _order(conn, ws, "20.00")
    o50 = _order(conn, ws, "25.00", quantity=2)                     # 25 × 2 = 50：按有效数量算小计
    over = _order(conn, ws, "50.01")
    under = _order(conn, ws, "19.99")
    allocation.allocate(conn, ws, trigger="manual")
    for o in (o48, o20, o50):
        assert _assigned(conn, ws, o["id"])[0] == acc["id"]
    for o in (over, under):
        assert orders.get_row(conn, ws, o["id"])["status"] == "queued"


def test_unlimited_max_amount(conn, workspace):
    ws = workspace["id"]
    acc = f.make_account(conn, ws, min_amount="20", max_amount=None)
    o = _order(conn, ws, "999.00")
    allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, o["id"])[0] == acc["id"]


def test_failed_attempt_today_counts_as_coverage_but_simulation_reset_does_not(conn, workspace):
    """D1-06 / 契约 §3.2：当天已有采购尝试（即使失败）= 已覆盖，保底名额给真正没覆盖的账号；
    「清除模拟结果」中止的尝试不算（清除后分配恢复原样）。"""
    ws = workspace["id"]
    a = f.make_account(conn, ws, name="A-DEMO", priority=1)
    b = f.make_account(conn, ws, name="B-DEMO", priority=9)
    old = _order(conn, ws, "25.00")
    f.make_attempt(conn, ws, old["id"], b["id"], status="failed")
    f.set_status(conn, old["id"], "exception")            # 失败后转异常，不在分配池里
    new = _order(conn, ws, "25.00")
    s = allocation.allocate(conn, ws, trigger="manual")
    assert _assigned(conn, ws, new["id"]) == (a["id"], allocation.NOTE_COVERAGE)
    assert s["uncovered"] == []                             # B 当天已有尝试 → 已覆盖，不算「订单数量不足」
    conn.execute("UPDATE procure.purchase_attempts SET status = 'aborted', resolution = 'simulation_reset' "
                 "WHERE account_id = %s", (b["id"],))
    orders.unassign(conn, ws, new["id"])
    s2 = allocation.allocate(conn, ws, trigger="manual")
    # 清除模拟结果后 B 又是「未覆盖」：两个都未覆盖、只有一单 → 按优先级给 B，A 记为订单数量不足
    assert _assigned(conn, ws, new["id"]) == (b["id"], allocation.NOTE_COVERAGE)
    assert [u["account_id"] for u in s2["uncovered"]] == [a["id"]]
