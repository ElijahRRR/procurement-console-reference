"""分配的两种方式并存（契约 §3.2、方案 v2 修订二 §E）：来源指定（第 0 步）+ 自动分配。池、指定账号接不了的每种原因、
指定被清空、写回规则与重复分配、摘要、未覆盖原因、人工改派与解除分配、账号改名 / 填买家号 ID / 新建 / 归档的触发、
「分配仍有效吗」。全部数据虚构。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services import accounts, allocation, clock, designation, orders, quota
from tests import factories as f

NOW = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)   # 北京 2026-11-18 10:00
TODAY = date(2026, 11, 18)
ID_A, ID_B, ID_NEW = "A1DEMO0000", "A1DEMO0001", "A1DEMO0777"


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _order(conn, ws, price="25.00", *, cid=None, buyer=None, **kw):
    """一笔通过人审、资料齐全的待分配任务；cid / buyer = 来源行的「买家号 ID」「买家号」。"""
    return f.ingest_ready_order(conn, ws, unit_price=Decimal(price), price_cap=Decimal(price) + 20,
                                external_buyer_customer_id=cid, external_buyer_account=buyer, **kw)


def _row(conn, ws, oid):
    return orders.get_row(conn, ws, oid)


def _where(conn, ws, oid):
    r = _row(conn, ws, oid)
    return r["status"], r["assigned_account_id"], r["assignment_kind"]


def _reason(conn, ws, oid):
    r = _row(conn, ws, oid)
    assert r["status"] == "queued" and r["assigned_account_id"] is None, (r["status"], r["assigned_account_id"])
    return r["status_reason"]


def _alloc(conn, ws, **kw):
    return allocation.allocate(conn, ws, trigger="manual", **kw)


def _snapshot(conn, ws):
    """任务行（含 updated_at）与事件数：重复分配前后必须完全相同。"""
    rows = conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s ORDER BY id", (ws,)).fetchall()
    events = conn.execute("SELECT count(*) AS n, max(id) AS last FROM procure.events WHERE workspace_id = %s",
                          (ws,)).fetchone()
    return rows, (events["n"], events["last"])


def _set_designation(conn, oid, cid=None, name=None):
    """直接改任务上的指定（相当于来源更新已写入、分配还没重算）。"""
    conn.execute("UPDATE procure.purchase_orders SET designated_customer_id = %s, designated_buyer_name = %s "
                 "WHERE id = %s", (cid, name, oid))


def _two(conn, ws, **kw):
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A, **kw)
    b = f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B, priority=9)
    return a, b


# ── 验收 1：填了就按指定，没填自动 ────────────────────────────────────────────


def test_designated_by_id_by_name_and_blank_goes_auto(conn, workspace):
    """验收 修订二 第 1 条：买家号 ID → 登记了这个 ID 的账号，分配方式「来源指定」；只填买家号 → 同名账号（忽略空格、
    全半角、大小写差异）；两列都空 → 自动分配。"""
    ws = workspace["id"]
    a, b = _two(conn, ws)                                    # B 优先级高：自动分配会优先给 B
    by_id = _order(conn, ws, cid=" a1demo0000 ")
    by_name = _order(conn, ws, buyer="环境－demo－Ａ")          # 全角、大小写不同
    exact = _order(conn, ws, buyer="环境-DEMO-A")
    blank1, blank2 = _order(conn, ws), _order(conn, ws)
    s = _alloc(conn, ws)
    assert _where(conn, ws, by_id["id"]) == ("assigned", a["id"], "source")
    assert _where(conn, ws, by_name["id"]) == ("assigned", a["id"], "source")
    assert _where(conn, ws, exact["id"]) == ("assigned", a["id"], "source")
    assert _row(conn, ws, by_id["id"])["assignment_note"] == "按来源的买家号 ID A1DEMO0000"
    assert _row(conn, ws, by_name["id"])["assignment_note"] == "按来源的买家号「环境－demo－Ａ」（对应账号 环境-DEMO-A）"
    assert _row(conn, ws, exact["id"])["assignment_note"] == "按来源的买家号「环境-DEMO-A」"
    for o in (blank1, blank2):
        st, acc_id, kind = _where(conn, ws, o["id"])
        assert (st, kind) == ("assigned", "auto") and acc_id == b["id"]
    assert s["stats"] == {"considered": 5, "assigned": 5, "coverage": 1, "fill": 1, "designated": 3, "unassigned": 0}
    assert s["uncovered"] == [] and s["unassigned"] == []    # A 由来源指定的任务覆盖，不进保底、也不算未覆盖
    ev = conn.execute("SELECT detail FROM procure.events WHERE order_id = %s AND kind = 'order.assigned'",
                      (by_id["id"],)).fetchone()["detail"]
    assert ev["kind"] == "source" and ev["account_id"] == a["id"] and ev["note"] == "按来源的买家号 ID A1DEMO0000"
    assert ev["from_account_id"] is None
    row = orders.list_orders(conn, ws, view="today")["rows"]
    assert {r["assignment_kind"] for r in row} == {"source", "auto"}


def test_id_wins_over_a_name_that_matches_nothing(conn, workspace):
    ws = workspace["id"]
    a, _ = _two(conn, ws)
    o = _order(conn, ws, cid=ID_A, buyer="环境-DEMO-写错了")
    _alloc(conn, ws)
    assert _where(conn, ws, o["id"]) == ("assigned", a["id"], "source")
    assert _row(conn, ws, o["id"])["assignment_note"] == (
        "按来源的买家号 ID A1DEMO0000（来源写的买家号「环境-DEMO-写错了」与账号名称不同）")


# ── 验收 2：指定的账号接不了 → 留在待分配并写明原因，绝不分给别的账号 ───────────────────


def test_unresolvable_designations_stay_queued_with_reasons(conn, workspace):
    """验收 修订二 第 2 条（解析不了的几种）：没有登记、已归档、ID 与名称指向不同账号、格式不对、名称没有同名账号
    → 留在待分配并写明原因；另一个账号明明接得了也不分给它。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _two(conn, ws)
    old = f.make_account(conn, ws, name="环境-DEMO-旧", amazon_customer_id="A1DEMO0009")
    accounts.archive(conn, ws, old["id"], uid)
    cases = {
        "unregistered": _order(conn, ws, cid=ID_NEW),
        "unregistered_named": _order(conn, ws, cid=ID_NEW, buyer="环境-DEMO-新"),
        "archived_id": _order(conn, ws, cid="A1DEMO0009"),
        "archived_name": _order(conn, ws, buyer="环境-DEMO-旧"),
        "mismatch": _order(conn, ws, cid=ID_A, buyer="环境-DEMO-B"),
        "bad_shape": _order(conn, ws, cid="X-1"),
        "no_such_name": _order(conn, ws, buyer="环境-DEMO-不存在"),
        "other_id": _order(conn, ws, cid=ID_NEW, buyer="环境-DEMO-B"),
    }
    s = _alloc(conn, ws)
    want = {
        "unregistered": "来源指定的买家号 ID A1DEMO0777 还没有登记为账号；在「账号与浏览器」登记后会自动分配",
        "unregistered_named": "来源指定的买家号 ID A1DEMO0777（买家号「环境-DEMO-新」）还没有登记为账号；"
                              "在「账号与浏览器」登记后会自动分配",
        "archived_id": "来源指定的买家号 ID A1DEMO0009 对应的账号 环境-DEMO-旧 已归档；重新登记这个买家号，或改派到其他账号",
        "archived_name": "来源指定的买家号「环境-DEMO-旧」对应的账号已归档；在来源里改成其他买家号，或改派到其他账号",
        "mismatch": "来源指定的买家号「环境-DEMO-B」（账号 环境-DEMO-B）与买家号 ID A1DEMO0000（账号 环境-DEMO-A）"
                    "不是同一个账号；在来源里改正其中一个后会重新分配",
        "bad_shape": "来源指定的买家号 ID「X-1」格式不对（应以 A 开头，至少 9 位大写字母或数字）；在来源里改正或清空后会重新分配",
        "no_such_name": "来源指定的买家号「环境-DEMO-不存在」没有同名账号；新建同名账号、在来源里改正，或改派到其他账号",
        "other_id": "账号 环境-DEMO-B 登记的买家号 ID 是 A1DEMO0001，与来源指定的 A1DEMO0777 不同；"
                    "在来源里改正，或修改账号的买家号 ID",
    }
    for key, o in cases.items():
        assert _reason(conn, ws, o["id"]) == want[key], key
    assert s["stats"] == {"considered": 8, "assigned": 0, "coverage": 0, "fill": 0, "designated": 0, "unassigned": 8}
    assert {u["order_id"]: (u["reason"], u["designated"]) for u in s["unassigned"]} == {
        cases[k]["id"]: (want[k], True) for k in cases}
    # A、B 都有额度、金额也匹配，却没有分到任何一笔：原因写明是来源指定给了别的账号
    assert {u["name"]: u["reason"] for u in s["uncovered"]} == {
        "环境-DEMO-A": allocation.UNCOVERED_DESIGNATED, "环境-DEMO-B": allocation.UNCOVERED_DESIGNATED}
    assert f.count(conn, "purchase_orders", "workspace_id = %s AND assigned_account_id IS NOT NULL", (ws,)) == 0


def test_ignored_id_is_called_out_in_the_reason(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    _two(conn, ws)
    o = _order(conn, ws, cid=ID_NEW)
    accounts.ignore_buyer_id(conn, ws, ID_NEW, uid)
    _alloc(conn, ws)
    assert _reason(conn, ws, o["id"]) == ("来源指定的买家号 ID A1DEMO0777 还没有登记为账号；在「账号与浏览器」登记后会自动分配"
                                         "（这个买家号 ID 已被忽略，恢复后才能登记）")


def test_designated_account_cannot_take_it_amount_site_subtotal(conn, workspace):
    """验收 修订二 第 2 条（金额不在区间）：还有站点不符、小计未知。另一个账号接得了也不分给它。"""
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A, min_amount="20", max_amount="30")
    f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B)
    over = _order(conn, ws, "45.00", cid=ID_A)
    abroad = _order(conn, ws, "25.00", cid=ID_A, country="CA")
    s = _alloc(conn, ws)
    assert _reason(conn, ws, over["id"]) == (
        "金额 $45.00 不在来源指定的账号 环境-DEMO-A 的区间 $20.00–$30.00 内；调整账号的金额区间，或改派到其他账号")
    assert _reason(conn, ws, abroad["id"]) == "任务收货国家与来源指定的账号 环境-DEMO-A 的站点不符"
    assert s["stats"]["assigned"] == 0 and all(u["designated"] is True for u in s["unassigned"])
    # 小计未知（单价读不出）：任务停在资料待补齐，不进池；直接问「能不能接」得到对应的原因
    assert designation.placement_problem(
        {"name": "环境-DEMO-A", "min_amount": Decimal("0"), "max_amount": None, "purchase_enabled": True,
         "execution_paused": False, "daily_limit": 20},
        site_ok=True, subtotal=None, in_range=False, on_account=False, cap=5, day=TODAY) == (
        "商品小计未知（缺少采购单价或数量），不能分给来源指定的账号 环境-DEMO-A")
    # 调整账号的金额区间（影响分配的字段）→ 立即重新分配，任务到位
    acc_a = conn.execute("SELECT id FROM procure.buyer_accounts WHERE name = '环境-DEMO-A'").fetchone()["id"]
    accounts.update(conn, ws, acc_a, {"max_amount": "50"}, workspace["user_id"])
    assert _where(conn, ws, over["id"]) == ("assigned", acc_a, "source")


def test_quota_full_stays_queued_and_is_placed_the_next_day(conn, workspace):
    """验收 修订二 第 2 条（当天额度已满）：留在待分配；第二天的每日分配有空位了再安排。每日上限为 0 另有原因。"""
    ws = workspace["id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A, daily_limit=1)
    b = f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B)
    zero = f.make_account(conn, ws, name="环境-DEMO-零", daily_limit=0)
    first = _order(conn, ws, cid=ID_A)
    second = _order(conn, ws, cid=ID_A)
    none = _order(conn, ws, buyer="环境-DEMO-零")
    s = _alloc(conn, ws)
    assert _where(conn, ws, first["id"]) == ("assigned", a["id"], "source")
    assert _reason(conn, ws, second["id"]) == (
        "来源指定的账号 环境-DEMO-A 在 2026-11-18 的额度已满（上限 1）；有空位后的分配会再安排")
    assert _reason(conn, ws, none["id"]) == "来源指定的账号 环境-DEMO-零 的每日上限为 0"
    assert s["stats"]["designated"] == 1 and s["stats"]["unassigned"] == 2
    assert f.count(conn, "purchase_orders", "assigned_account_id = %s", (b["id"],)) == 0     # 没有落到 B
    # 当天那一笔采购掉（占用当天额度），第二天额度重新计算
    f.make_attempt(conn, ws, first["id"], a["id"], status="succeeded", submitted=True, amazon_order_no="SIM-20261118-1")
    f.set_status(conn, first["id"], "purchased")
    clock.freeze(NOW + timedelta(days=1))
    out = allocation.allocate_daily(conn, ws)
    assert out is not None and _where(conn, ws, second["id"]) == ("assigned", a["id"], "source")
    assert _reason(conn, ws, none["id"]) == "来源指定的账号 环境-DEMO-零 的每日上限为 0"
    assert zero["daily_limit"] == 0


def test_switched_off_or_paused_account_takes_no_new_designated_task(conn, workspace):
    """验收 修订二 第 2 条（账号关闭采购开关或暂停执行）：新的指定留在待分配并写明原因；打开开关 / 恢复执行后自动到位。"""
    ws, uid = workspace["id"], workspace["user_id"]
    off = f.make_account(conn, ws, name="环境-DEMO-关", amazon_customer_id=ID_A, purchase_enabled=False)
    paused = f.make_account(conn, ws, name="环境-DEMO-停", amazon_customer_id=ID_B, execution_paused=True,
                            pause_reason="DEMO 暂停")
    f.make_account(conn, ws, name="环境-DEMO-可用")
    to_off = _order(conn, ws, cid=ID_A)
    to_paused = _order(conn, ws, buyer="环境-DEMO-停")
    s = _alloc(conn, ws)
    assert _reason(conn, ws, to_off["id"]) == "来源指定的账号 环境-DEMO-关 已关闭采购开关，不接收新分配；打开开关后会自动分配"
    assert _reason(conn, ws, to_paused["id"]) == "来源指定的账号 环境-DEMO-停 已暂停执行；恢复执行后会自动分配"
    assert s["stats"]["assigned"] == 0 and [u["designated"] for u in s["unassigned"]] == [True, True]
    accounts.update(conn, ws, off["id"], {"purchase_enabled": True}, uid)
    assert _where(conn, ws, to_off["id"]) == ("assigned", off["id"], "source")
    accounts.resume(conn, ws, paused["id"], actor_kind="user", actor_id=uid)
    assert _where(conn, ws, to_paused["id"]) == ("assigned", paused["id"], "source")


def test_failed_designated_task_is_never_picked_up_by_fill(conn, workspace):
    """陷阱：分不出去的指定任务留在任务列表里的话，填充会把它给优先级最高的账号。"""
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A, daily_limit=0)
    b = f.make_account(conn, ws, name="环境-DEMO-B", priority=9, daily_limit=20)
    stuck = [_order(conn, ws, cid=ID_A) for _ in range(3)]
    free = _order(conn, ws)
    s = _alloc(conn, ws)
    for o in stuck:
        assert _reason(conn, ws, o["id"]) == "来源指定的账号 环境-DEMO-A 的每日上限为 0"
    assert _where(conn, ws, free["id"]) == ("assigned", b["id"], "auto")
    assert s["stats"] == {"considered": 4, "assigned": 1, "coverage": 1, "fill": 0, "designated": 0, "unassigned": 3}


# ── 优先级：人工改派（固定）> 来源指定 > 自动 ─────────────────────────────────────


def test_manual_keeps_its_slot_designated_displaces_auto(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A, daily_limit=2)
    auto1, auto2 = _order(conn, ws), _order(conn, ws)
    _alloc(conn, ws)
    assert {_where(conn, ws, o["id"])[2] for o in (auto1, auto2)} == {"auto"}          # 两个名额都被自动任务占着
    manual = _order(conn, ws)
    with pytest.raises(Exception) as e:                                               # 人工改派不挤掉已排的任务
        orders.assign_manual(conn, ws, manual["id"], a["id"], uid)
    assert "额度已满" in str(getattr(e.value, "message", e.value))
    orders.unassign(conn, ws, auto2["id"], uid)
    orders.assign_manual(conn, ws, manual["id"], a["id"], uid)
    des1, des2 = _order(conn, ws, cid=ID_A), _order(conn, ws, cid=ID_A)
    s = _alloc(conn, ws)
    assert _where(conn, ws, manual["id"]) == ("assigned", a["id"], "manual")           # 人工的不动
    assert _where(conn, ws, des1["id"]) == ("assigned", a["id"], "source")             # 来源指定的拿走剩下的一个名额
    assert _reason(conn, ws, des2["id"]).startswith("来源指定的账号 环境-DEMO-A 在 2026-11-18 的额度已满（上限 2）")
    assert _reason(conn, ws, auto1["id"]) == allocation.REASON_FULL                    # 原来的自动任务让位
    assert s["stats"]["designated"] == 1
    assert quota.fixed_map(conn, ws, TODAY) == {(a["id"], TODAY): 1}                   # fixed 仍只算 manual
    assert quota.planned(conn, a["id"], TODAY, today=TODAY, workspace_id=ws) == 2      # planned 把来源指定的也算上
    assert quota.summary(conn, a, TODAY, True)["available"] == 0


def test_manual_assignment_overrides_and_unassign_returns_to_the_designated_account(conn, workspace):
    """验收 修订二 第 5 条：人工改派后留在改派账号期间不跟随来源；解除分配后回到来源指定的账号。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _two(conn, ws)
    src = f.feishu_source(conn, ws, "recP01")
    o = _order(conn, ws, cid=ID_A, source=src, upstream_order_no="PO-DEMO-P01")
    _alloc(conn, ws)
    assert _where(conn, ws, o["id"]) == ("assigned", a["id"], "source")
    orders.assign_manual(conn, ws, o["id"], b["id"], uid)
    assert _where(conn, ws, o["id"]) == ("assigned", b["id"], "manual")
    _alloc(conn, ws)
    assert _where(conn, ws, o["id"]) == ("assigned", b["id"], "manual")                 # 分配不动人工改派
    assert _row(conn, ws, o["id"])["designated_customer_id"] == ID_A
    # 来源那边原样再来一次：「分配仍有效吗」不把人工改派当成不符合指定
    rec = f.make_record(upstream_order_no="PO-DEMO-P01", unit_price=Decimal("25.00"), price_cap=Decimal("45.00"),
                        external_buyer_customer_id=ID_A, sku="SKU-DEMO-9")
    assert orders.ingest(conn, ws, rec, src).status == "assigned"
    assert _where(conn, ws, o["id"]) == ("assigned", b["id"], "manual")
    orders.unassign(conn, ws, o["id"], uid)
    assert _where(conn, ws, o["id"])[0] == "queued"
    _alloc(conn, ws)
    assert _where(conn, ws, o["id"]) == ("assigned", a["id"], "source")


# ── 池：停在不参与账号上的任务 ────────────────────────────────────────────────


def test_source_task_on_a_switched_off_or_paused_account_is_kept(conn, workspace):
    """陷阱：保留在不参与账号上的任务，写回时不能用只含参与账号的映射取账号行（KeyError）。来源指定的任务已经在
    指定的账号上，账号随后关闭采购开关 / 暂停 → 原样保留，不做额度运算，也不写任何东西。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A, daily_limit=1)
    p = f.make_account(conn, ws, name="环境-DEMO-P", amazon_customer_id=ID_B)
    on_a, on_p = _order(conn, ws, cid=ID_A), _order(conn, ws, buyer="环境-DEMO-P")
    _alloc(conn, ws)
    accounts.update(conn, ws, a["id"], {"purchase_enabled": False}, uid)        # 触发一次重新分配
    accounts.pause(conn, ws, p["id"], "DEMO 暂停", actor_kind="user", actor_id=uid)
    late = _order(conn, ws, cid=ID_A)                                           # 关闭之后才来的指定：不接
    before = _snapshot(conn, ws)
    clock.freeze(NOW + timedelta(minutes=5))
    s = _alloc(conn, ws)
    assert _where(conn, ws, on_a["id"]) == ("assigned", a["id"], "source")
    assert _where(conn, ws, on_p["id"]) == ("assigned", p["id"], "source")
    assert "已关闭采购开关" in _reason(conn, ws, late["id"])
    assert s["stats"] == {"considered": 3, "assigned": 2, "coverage": 0, "fill": 0, "designated": 2, "unassigned": 1}
    assert s["uncovered"] == []                                                 # 不参与的账号不列未覆盖
    rows_before = {r["id"]: r for r in before[0]}
    for o in (on_a, on_p):
        assert _row(conn, ws, o["id"]) == rows_before[o["id"]]                  # 保留的任务一个字节都没写


def test_auto_task_parked_on_a_switched_off_account_follows_a_later_designation(conn, workspace):
    """陷阱：停在已关闭采购开关账号上的自动任务后来被指定给别的账号——它必须进池并搬走（关了开关的账号还能执行
    已分配的任务，不搬就会被不是指定的账号买走）。"""
    ws, uid = workspace["id"], workspace["user_id"]
    b = f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B)
    parked, stays = _order(conn, ws), _order(conn, ws)
    _alloc(conn, ws)
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    accounts.update(conn, ws, b["id"], {"purchase_enabled": False}, uid)
    assert _where(conn, ws, parked["id"]) == ("assigned", b["id"], "auto")       # 关闭开关：已分配的自动任务保留
    _set_designation(conn, parked["id"], cid=ID_A)
    s = _alloc(conn, ws)
    assert _where(conn, ws, parked["id"]) == ("assigned", a["id"], "source")
    assert _where(conn, ws, stays["id"]) == ("assigned", b["id"], "auto")        # 没被指定的那笔不进池、原样留着
    assert s["stats"]["considered"] == 1 and s["stats"]["designated"] == 1
    ev = conn.execute("SELECT detail FROM procure.events WHERE order_id = %s AND kind = 'order.reassigned'",
                      (parked["id"],)).fetchone()["detail"]
    assert (ev["kind"], ev["from_account_id"], ev["account_id"]) == ("source", b["id"], a["id"])


def test_auto_task_parked_on_a_paused_account_designated_to_that_same_account_is_kept(conn, workspace):
    """任务本来就在指定的账号上（当时是自动分的）、账号已暂停：按「当前在不在这个账号上」保留，只把分配方式改成来源指定
    （不记事件、不动 assigned_at）；指定解析不了时则解除分配并写明原因。"""
    ws, uid = workspace["id"], workspace["user_id"]
    b = f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B)
    same, broken = _order(conn, ws), _order(conn, ws)
    _alloc(conn, ws)
    accounts.pause(conn, ws, b["id"], "DEMO 暂停", actor_kind="user", actor_id=uid)
    assigned_at = _row(conn, ws, same["id"])["assigned_at"]
    events_before = f.count(conn, "events", "order_id = %s", (same["id"],))
    _set_designation(conn, same["id"], name="环境-DEMO-B")
    _set_designation(conn, broken["id"], cid=ID_NEW)
    clock.freeze(NOW + timedelta(minutes=5))
    s = _alloc(conn, ws)
    row = _row(conn, ws, same["id"])
    assert (row["status"], row["assigned_account_id"], row["assignment_kind"]) == ("assigned", b["id"], "source")
    assert row["assignment_note"] == "按来源的买家号「环境-DEMO-B」" and row["assigned_at"] == assigned_at
    assert f.count(conn, "events", "order_id = %s", (same["id"],)) == events_before
    assert "还没有登记为账号" in _reason(conn, ws, broken["id"])
    assert s["stats"]["designated"] == 1 and s["stats"]["unassigned"] == 1


# ── 指定被来源清空 ────────────────────────────────────────────────────────────


def test_cleared_designation_goes_back_to_auto(conn, workspace):
    """验收 修订二 第 4 条（清空 → 回到自动分配）：指定被清空的任务，当前账号参与自动分配 → 当作自动任务重算
    （这里它会按优先级换到 B）；当前账号不参与 → 原地改为自动并保留。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _two(conn, ws)                                    # B 优先级 9
    p = f.make_account(conn, ws, name="环境-DEMO-P", amazon_customer_id="A1DEMO0005")
    moved, kept = _order(conn, ws, cid=ID_A), _order(conn, ws, buyer="环境-DEMO-P")
    filler = _order(conn, ws)
    _alloc(conn, ws)
    assert _where(conn, ws, moved["id"]) == ("assigned", a["id"], "source")
    assert _where(conn, ws, kept["id"]) == ("assigned", p["id"], "source")
    accounts.pause(conn, ws, p["id"], "DEMO 暂停", actor_kind="user", actor_id=uid)
    _set_designation(conn, moved["id"])
    _set_designation(conn, kept["id"])
    assigned_at = _row(conn, ws, kept["id"])["assigned_at"]
    events_before = f.count(conn, "events", "order_id = %s", (kept["id"],))
    clock.freeze(NOW + timedelta(minutes=5))
    s = _alloc(conn, ws)
    st, acc_id, kind = _where(conn, ws, moved["id"])
    assert (st, kind) == ("assigned", "auto") and acc_id in (a["id"], b["id"])
    assert _row(conn, ws, moved["id"])["assignment_note"] in (allocation.NOTE_COVERAGE, allocation.NOTE_FILL)
    row = _row(conn, ws, kept["id"])
    assert (row["status"], row["assigned_account_id"], row["assignment_kind"]) == ("assigned", p["id"], "auto")
    assert row["assignment_note"] == "来源取消了指定，保留在原账号" == designation.NOTE_CLEARED_KEPT
    assert row["assigned_at"] == assigned_at
    assert f.count(conn, "events", "order_id = %s", (kept["id"],)) == events_before      # 只改列，不记事件
    # 保留下来的那笔不算本次考虑的任务：considered = assigned + unassigned
    assert s["stats"]["considered"] == 2 == s["stats"]["assigned"] and s["stats"]["designated"] == 0
    assert _where(conn, ws, filler["id"])[2] == "auto"
    # 再来一次：那笔已是普通的自动任务（停在暂停的账号上，不进池），什么都不写
    before = _snapshot(conn, ws)
    clock.freeze(NOW + timedelta(minutes=10))
    again = _alloc(conn, ws)
    assert _snapshot(conn, ws) == before and again["stats"]["considered"] == 2


# ── 验收 9：重复分配不产生任务写入和事件 ────────────────────────────────────────


def test_second_identical_allocation_writes_nothing(conn, workspace):
    """验收 修订二 第 9 条：两次相同的分配，第二次除了 allocation_runs 多一行，不写任务、不写事件
    （events 与 purchase_orders.updated_at 都不变）。覆盖：来源指定已分配、保留在已关闭账号上的、指定分不出去的各种
    原因、自动分配的、分不出去的自动任务。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A, min_amount="20", max_amount="30",
                       daily_limit=2)
    b = f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B, min_amount="20", max_amount="60")
    off = f.make_account(conn, ws, name="环境-DEMO-关", amazon_customer_id="A1DEMO0005")
    kept = _order(conn, ws, buyer="环境-DEMO-关")
    _alloc(conn, ws)
    accounts.update(conn, ws, off["id"], {"purchase_enabled": False}, uid)
    orders_ = {
        "kept": kept,
        "src_id": _order(conn, ws, cid=ID_A),
        "src_name": _order(conn, ws, buyer="环境-demo-a"),
        "full": _order(conn, ws, cid=ID_A),                       # A 每天 2 单：第三笔额度已满
        "range": _order(conn, ws, "45.00", cid=ID_A),
        "unregistered": _order(conn, ws, cid=ID_NEW, buyer="环境-DEMO-新"),
        "bad": _order(conn, ws, cid="oops"),
        "to_off": _order(conn, ws, cid="A1DEMO0005"),
        "auto": _order(conn, ws, "45.00"),
        "auto_none": _order(conn, ws, "99.00"),
    }
    first = _alloc(conn, ws)
    assert first["stats"] == {"considered": 10, "assigned": 4, "coverage": 1, "fill": 0, "designated": 3,
                              "unassigned": 6}
    assert _where(conn, ws, orders_["kept"]["id"]) == ("assigned", off["id"], "source")
    assert _where(conn, ws, orders_["auto"]["id"]) == ("assigned", b["id"], "auto")
    before = _snapshot(conn, ws)
    runs = f.count(conn, "allocation_runs", "workspace_id = %s", (ws,))
    for minutes in (5, 10):
        clock.freeze(NOW + timedelta(minutes=minutes))
        again = _alloc(conn, ws)
        assert again["stats"] == first["stats"] and again["unassigned"] == first["unassigned"]
        assert again["uncovered"] == first["uncovered"]
        assert _snapshot(conn, ws) == before, "重复分配改了任务或写了事件"
    assert f.count(conn, "allocation_runs", "workspace_id = %s", (ws,)) == runs + 2
    assert a["daily_limit"] == 2


@pytest.mark.parametrize("change", ["auto_to_source", "source_to_other_account", "source_to_queued", "note_only"])
def test_each_transition_is_written_once_then_stable(conn, workspace, change):
    """写回规则：账号变了才记 order.assigned / order.reassigned（detail 带 kind）；只有分配方式或说明变 → 只更新列，
    不记事件、不动 assigned_at；之后再分配什么都不写。"""
    ws = workspace["id"]
    a, b = _two(conn, ws)
    o = _order(conn, ws) if change == "auto_to_source" else _order(conn, ws, cid=ID_A)
    _alloc(conn, ws)
    start = _row(conn, ws, o["id"])
    n_events = f.count(conn, "events", "order_id = %s", (o["id"],))
    if change == "auto_to_source":
        assert start["assignment_kind"] == "auto" and start["assigned_account_id"] == b["id"]
        _set_designation(conn, o["id"], name="环境-DEMO-B")           # 指定的正是它所在的账号
    elif change == "source_to_other_account":
        _set_designation(conn, o["id"], cid=ID_B)
    elif change == "source_to_queued":
        _set_designation(conn, o["id"], cid=ID_NEW)
    else:
        _set_designation(conn, o["id"], name="环境-DEMO-A")           # 由按 ID 改成按名称，账号没变
    clock.freeze(NOW + timedelta(minutes=5))
    _alloc(conn, ws)
    row = _row(conn, ws, o["id"])
    new_events = [r for r in conn.execute("SELECT kind, detail FROM procure.events WHERE order_id = %s ORDER BY id",
                                          (o["id"],)).fetchall()][n_events:]
    if change == "auto_to_source":
        assert (row["assigned_account_id"], row["assignment_kind"]) == (b["id"], "source")
        assert row["assignment_note"] == "按来源的买家号「环境-DEMO-B」"
        assert new_events == [] and row["assigned_at"] == start["assigned_at"]
    elif change == "source_to_other_account":
        assert (row["assigned_account_id"], row["assignment_kind"]) == (b["id"], "source")
        assert [e["kind"] for e in new_events] == ["order.reassigned"]
        assert new_events[0]["detail"]["kind"] == "source" and new_events[0]["detail"]["from_account_id"] == a["id"]
        assert row["assigned_at"] == NOW + timedelta(minutes=5)
    elif change == "source_to_queued":
        assert row["status"] == "queued" and row["assigned_account_id"] is None and row["assignment_kind"] is None
        assert "还没有登记为账号" in row["status_reason"]
        assert [e["kind"] for e in new_events] == ["order.status_changed"]
    else:
        assert (row["assigned_account_id"], row["assignment_kind"]) == (a["id"], "source")
        assert row["assignment_note"] == "按来源的买家号「环境-DEMO-A」"
        assert new_events == [] and row["assigned_at"] == start["assigned_at"]
    before = _snapshot(conn, ws)
    clock.freeze(NOW + timedelta(minutes=10))
    _alloc(conn, ws)
    assert _snapshot(conn, ws) == before


# ── 摘要与未覆盖原因 ──────────────────────────────────────────────────────────


def test_uncovered_reason_when_matching_orders_are_designated_elsewhere(conn, workspace):
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A, min_amount="20", max_amount="30")
    f.make_account(conn, ws, name="环境-DEMO-B", min_amount="20", max_amount="30")
    f.make_account(conn, ws, name="环境-DEMO-C", min_amount="40", max_amount="60")
    f.make_account(conn, ws, name="环境-DEMO-D", min_amount="100", max_amount="200")
    for _ in range(2):
        _order(conn, ws, "25.00", cid=ID_A)
    _order(conn, ws, "45.00")
    s = _alloc(conn, ws)
    reasons = {u["name"]: u["reason"] for u in s["uncovered"]}
    assert reasons == {"环境-DEMO-B": "金额匹配的订单都已由来源指定给其他账号",      # 有匹配的，但都指定给了 A
                       "环境-DEMO-D": allocation.UNCOVERED_NO_MATCH}                # 什么都不匹配：原来的原因
    assert s["stats"] == {"considered": 3, "assigned": 3, "coverage": 1, "fill": 0, "designated": 2, "unassigned": 0}


def test_old_allocation_runs_get_designated_zero(conn, workspace):
    """有「来源指定」之前写下的分配记录没有 designated：读出时补 0。"""
    ws = workspace["id"]
    conn.execute(
        "INSERT INTO procure.allocation_runs (workspace_id, trigger, bj_date, at, stats) VALUES (%s, 'daily', %s, %s, "
        "'{\"considered\": 2, \"assigned\": 2, \"coverage\": 1, \"fill\": 1, \"unassigned\": 0}'::jsonb)",
        (ws, TODAY, NOW))
    out = allocation.last_run(conn, ws)
    assert out["stats"] == {"considered": 2, "assigned": 2, "coverage": 1, "fill": 1, "unassigned": 0, "designated": 0}
    stored = conn.execute("SELECT stats FROM procure.allocation_runs WHERE workspace_id = %s", (ws,)).fetchone()
    assert "designated" not in stored["stats"]                                # 只在读出时补，不改库


# ── 账号变化触发重新分配 ──────────────────────────────────────────────────────


def test_registering_the_designated_id_assigns_the_waiting_tasks_at_once(conn, workspace):
    """验收 修订二 第 8 条：来源指定但未登记的买家号 ID 出现在「待登记的买家号」里；登记（新建账号，或填到已有账号）后
    任务立刻分给它。新建时采购开关关着也会重新分配（原因换成当前的）。"""
    ws, uid = workspace["id"], workspace["user_id"]
    f.make_account(conn, ws, name="环境-DEMO-别的")
    blank = f.make_account(conn, ws, name="环境-DEMO-空")
    o1 = _order(conn, ws, cid=ID_NEW, buyer="环境-DEMO-新")
    o2 = _order(conn, ws, cid=ID_NEW)
    o3 = _order(conn, ws, cid="A1DEMO0888")
    o4 = _order(conn, ws, cid="A1DEMO0999")
    _alloc(conn, ws)
    items = {i["customer_id"]: i for i in accounts.discovered(conn, ws)}
    assert set(items) == {ID_NEW, "A1DEMO0888", "A1DEMO0999"}
    assert items[ID_NEW] == {"customer_id": ID_NEW, "ignored": False, "browsers": [], "last_reported_at": None,
                             "name_hint": "环境-DEMO-新", "source_orders": 2}
    assert accounts.discovered_count(conn, ws) == 3

    made = accounts.create(conn, ws, {"name": "环境-DEMO-新", "amazon_customer_id": ID_NEW}, uid)     # 登记 = 新建账号
    assert _where(conn, ws, o1["id"]) == ("assigned", made["id"], "source")
    assert _where(conn, ws, o2["id"]) == ("assigned", made["id"], "source")
    accounts.update(conn, ws, blank["id"], {"amazon_customer_id": "A1DEMO0888"}, uid)               # 填到已有账号
    assert _where(conn, ws, o3["id"]) == ("assigned", blank["id"], "source")
    accounts.create(conn, ws, {"name": "环境-DEMO-关着", "amazon_customer_id": "A1DEMO0999",
                               "purchase_enabled": False}, uid)
    assert _reason(conn, ws, o4["id"]) == "来源指定的账号 环境-DEMO-关着 已关闭采购开关，不接收新分配；打开开关后会自动分配"
    assert accounts.discovered(conn, ws) == [] and accounts.discovered_count(conn, ws) == 0
    assert accounts.ALLOCATION_FIELDS >= {"name", "amazon_customer_id"}


def test_renaming_or_changing_the_id_of_an_account_reallocates_at_once(conn, workspace):
    """陷阱：账号改名、改买家号 ID 不触发重新分配的话，按旧名称 / 旧 ID 指定的任务会继续挂在这个账号上被它买走。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, _ = _two(conn, ws)
    by_name = _order(conn, ws, buyer="环境-DEMO-A")
    by_id = _order(conn, ws, cid=ID_A)
    waiting = _order(conn, ws, buyer="环境-DEMO-A2")
    _alloc(conn, ws)
    assert _where(conn, ws, by_name["id"]) == ("assigned", a["id"], "source")
    runs = f.count(conn, "allocation_runs", "workspace_id = %s", (ws,))
    accounts.update(conn, ws, a["id"], {"name": "环境-DEMO-A2"}, uid)
    assert f.count(conn, "allocation_runs", "workspace_id = %s AND trigger = 'account_change'", (ws,)) == 1
    assert _reason(conn, ws, by_name["id"]) == (
        "来源指定的买家号「环境-DEMO-A」没有同名账号；新建同名账号、在来源里改正，或改派到其他账号")
    assert _where(conn, ws, waiting["id"]) == ("assigned", a["id"], "source")           # 等着新名字的任务到位
    assert _where(conn, ws, by_id["id"]) == ("assigned", a["id"], "source")
    accounts.update(conn, ws, a["id"], {"amazon_customer_id": "A1DEMO0444"}, uid)
    assert _reason(conn, ws, by_id["id"]) == (
        "来源指定的买家号 ID A1DEMO0000 还没有登记为账号；在「账号与浏览器」登记后会自动分配")
    accounts.update(conn, ws, a["id"], {"notes": "只改备注"}, uid)                        # 不影响分配的字段：不重新分配
    assert f.count(conn, "allocation_runs", "workspace_id = %s", (ws,)) == runs + 2


def test_archiving_the_designated_account_leaves_its_tasks_queued_with_the_archived_reason(conn, workspace):
    """验收 修订二 第 2 条（已归档）：账号归档后，指定给它的任务回到待分配并写明已归档，不会自动分给别的账号。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _two(conn, ws)
    by_id, by_name = _order(conn, ws, cid=ID_A), _order(conn, ws, buyer="环境-DEMO-A")
    auto = _order(conn, ws)
    _alloc(conn, ws)
    assert _where(conn, ws, by_id["id"]) == ("assigned", a["id"], "source")
    accounts.archive(conn, ws, a["id"], uid)
    assert _reason(conn, ws, by_id["id"]) == (
        "来源指定的买家号 ID A1DEMO0000 对应的账号 环境-DEMO-A 已归档；重新登记这个买家号，或改派到其他账号")
    assert _reason(conn, ws, by_name["id"]) == (
        "来源指定的买家号「环境-DEMO-A」对应的账号已归档；在来源里改成其他买家号，或改派到其他账号")
    assert _where(conn, ws, auto["id"]) == ("assigned", b["id"], "auto")
    assert f.count(conn, "purchase_orders", "assigned_account_id = %s", (b["id"],)) == 1


# ── 「分配仍有效吗」（指定变了而分配还没重算时）────────────────────────────────────


def test_returning_to_assigned_without_an_allocation_run_does_not_keep_a_stale_account(conn, workspace):
    """§E.6：异常恢复、核实未下单等把任务放回「已分配」时不跑分配；指定已不是原账号 → 当场解除分配，下一次分配按指定落位。
    人工改派的不受这条约束。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _two(conn, ws)
    stale, manual, fine = _order(conn, ws), _order(conn, ws), _order(conn, ws, cid=ID_B)
    for o in (stale, fine):
        f.make_attempt(conn, ws, o["id"], b["id"], status="failed")
        f.set_status(conn, o["id"], "exception", assigned_account_id=b["id"], assignment_kind="auto", assigned_at=NOW,
                     assignment_note="DEMO 分配")
    f.make_attempt(conn, ws, manual["id"], b["id"], status="failed")
    f.set_status(conn, manual["id"], "exception", assigned_account_id=b["id"], assignment_kind="manual",
                 assigned_at=NOW, assignment_note="人工改派")
    _set_designation(conn, stale["id"], cid=ID_A)            # 异常期间来源把买家号改成了 A
    _set_designation(conn, manual["id"], cid=ID_A)
    assert orders.resume(conn, ws, stale["id"], None, uid)["status"] == "queued"
    row = _row(conn, ws, stale["id"])
    assert row["status_reason"] == "来源指定的买家号已不是账号 环境-DEMO-B，已解除分配" and row["assigned_account_id"] is None
    assert orders.resume(conn, ws, manual["id"], None, uid)["status"] == "assigned"
    assert _where(conn, ws, manual["id"]) == ("assigned", b["id"], "manual")
    assert orders.resume(conn, ws, fine["id"], None, uid)["status"] == "assigned"       # 指定的就是当前账号：保留
    _alloc(conn, ws)
    assert _where(conn, ws, stale["id"]) == ("assigned", a["id"], "source")
    assert _where(conn, ws, fine["id"]) == ("assigned", b["id"], "source")
