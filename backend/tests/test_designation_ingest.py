"""来源指定买家号的接收规则（orders.ingest，方案 v2 修订二 §C；契约 §2.1）：新任务与已有任务、首次关联与已关联、
主次来源、逐字比较、任何状态都写且不进「采购开始后修改 / 冲突 / 关注」、人工改派、写回读回、同意重采、读没读到买家号
按列算（没读到的那一列不动、关联上的「从来没读到过」标记）、没采用时的说明与事件，以及不该动指定的那些动作。
全部数据虚构。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services import allocation, clock, designation, orders
from services.models import SourceRef
from tests import factories as f

NOW = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)   # 北京 2026-11-18 10:00
ID_A, ID_B = "A1DEMO0000", "A1DEMO0001"
EVENT = "order.designation_changed"
NOT_ADOPTED = "order.designation_not_adopted"
K_ID, K_NAME = designation.RECORD_KEYS          # external_buyer_customer_id, external_buyer_account
ADVICE = "未采用；要改请在主来源里改，或在控制台改派（这条来源之后再改买家号，会按改后的应用）"


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _row(conn, ws, oid):
    return orders.get_row(conn, ws, oid)


def _des(conn, ws, oid):
    r = _row(conn, ws, oid)
    return r["designated_customer_id"], r["designated_buyer_name"]


def _events(conn, oid, kind=None):
    rows = conn.execute("SELECT kind, actor_kind, detail FROM procure.events WHERE order_id = %s ORDER BY id",
                        (oid,)).fetchall()
    return [r for r in rows if kind is None or r["kind"] == kind]


def _kinds(conn, oid):
    return [r["kind"] for r in _events(conn, oid)]


def _ingest(conn, ws, src, no, **kw):
    """同一个平台订单号的记录（其余字段固定）经 src 接收一次。buyer / cid 是「买家号」「买家号 ID」两列。"""
    buyer, cid = kw.pop("buyer", None), kw.pop("cid", None)
    dry_run = kw.pop("dry_run", False)
    rec = f.make_record(upstream_order_no=no, external_buyer_account=buyer, external_buyer_customer_id=cid, **kw)
    return orders.ingest(conn, ws, rec, src, dry_run=dry_run)


# ── 列清单（漏加会静默丢掉指定）───────────────────────────────────────────────


def test_designation_columns_are_writable_but_outside_the_plain_and_clear_groups():
    """陷阱：ORDER_COLUMNS 漏加 → 新任务的指定被静默丢掉；加进 PLAIN_COLUMNS / _CLEAR_ASSIGNMENT → 走错规则。"""
    cols = set(designation.ORDER_COLUMNS)
    assert cols == {"designated_customer_id", "designated_buyer_name"} and cols <= orders.ORDER_COLUMNS
    assert not cols & set(orders.PLAIN_COLUMNS) and not cols & set(orders._CLEAR_ASSIGNMENT)
    assert not cols & set(orders.DISPLAY_COLUMNS)


# ── 新任务 ───────────────────────────────────────────────────────────────────


def test_new_task_stores_the_cleaned_designation_and_shows_it(conn, workspace):
    """验收 修订二 第 1 条（接收部分）：没有 AMZ 订单号的行填了买家号 ID / 买家号 → 任务带上指定；两列都空 → 没有指定。"""
    ws = workspace["id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    by_id = _ingest(conn, ws, f.feishu_source(conn, ws, "recD01"), "PO-DEMO-D01", cid=" a1demo 0000 ")
    by_name = _ingest(conn, ws, f.feishu_source(conn, ws, "recD02"), "PO-DEMO-D02", buyer="  环境-DEMO-A ")
    both = _ingest(conn, ws, f.feishu_source(conn, ws, "recD03"), "PO-DEMO-D03", cid="Ａ１ＤＥＭＯ００００",
                   buyer="环境-DEMO-A")
    none = _ingest(conn, ws, f.feishu_source(conn, ws, "recD04"), "PO-DEMO-D04")
    assert [o.verdict for o in (by_id, by_name, both, none)] == ["new"] * 4
    assert _des(conn, ws, by_id.order_id) == (ID_A, None)                    # NFKC、去全部空白、转大写
    assert _des(conn, ws, by_name.order_id) == (None, "环境-DEMO-A")          # 名称只去首尾空白
    assert _des(conn, ws, both.order_id) == (ID_A, "环境-DEMO-A")
    assert _des(conn, ws, none.order_id) == (None, None)
    assert _events(conn, by_id.order_id, EVENT) == []                        # 新建不算「指定变了」

    rows = {r["id"]: r for r in orders.list_orders(conn, ws)["rows"]}
    acc = {"id": a["id"], "name": "环境-DEMO-A"}
    assert rows[by_id.order_id]["designated_buyer"] == {"customer_id": ID_A, "name": None, "account": acc}
    assert rows[by_name.order_id]["designated_buyer"] == {"customer_id": None, "name": "环境-DEMO-A", "account": acc}
    assert rows[none.order_id]["designated_buyer"] is None
    detail = orders.get_detail(conn, ws, both.order_id)
    assert detail["designated_buyer"] == {"customer_id": ID_A, "name": "环境-DEMO-A", "account": acc}
    assert orders.get_detail(conn, ws, none.order_id)["designated_buyer"] is None


def test_designated_buyer_account_is_null_when_it_does_not_resolve(conn, workspace):
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    o = _ingest(conn, ws, f.feishu_source(conn, ws, "recD05"), "PO-DEMO-D05", cid="A1DEMO0777", buyer="环境-DEMO-新")
    row = orders.list_orders(conn, ws)["rows"][0]
    assert row["designated_buyer"] == {"customer_id": "A1DEMO0777", "name": "环境-DEMO-新", "account": None}
    assert orders.get_detail(conn, ws, o.order_id)["designated_buyer"]["account"] is None


def test_list_reads_the_account_table_once_per_query(conn, workspace, monkeypatch):
    """§E.11：一次列表查询只读一次账号表（没有带指定的行时一次也不读）。"""
    ws = workspace["id"]
    f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    calls = []
    real = designation.load_context
    monkeypatch.setattr(designation, "load_context", lambda *a, **k: calls.append(a) or real(*a, **k))
    _ingest(conn, ws, f.feishu_source(conn, ws, "recD06"), "PO-DEMO-D06")
    assert orders.list_orders(conn, ws)["total"] == 1 and calls == []
    for i in range(3):
        _ingest(conn, ws, f.feishu_source(conn, ws, f"recD1{i}"), f"PO-DEMO-D1{i}", cid=ID_A)
    calls.clear()
    assert orders.list_orders(conn, ws)["total"] == 4 and len(calls) == 1


def test_row_with_a_credible_order_number_is_history_not_a_designation(conn, workspace):
    """验收 修订二 第 3 条（接收部分）：带可信 AMZ 订单号的行，买家号两列只是历史采购记录。"""
    ws = workspace["id"]
    out = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-H01", cid=ID_A, buyer="环境-DEMO-A",
                  external_amz_order_no="111-0000000-0000001")
    assert out.verdict == "historical" and _des(conn, ws, out.order_id) == (None, None)
    att = conn.execute("SELECT buyer_account_name FROM procure.purchase_attempts WHERE purchase_order_id = %s",
                       (out.order_id,)).fetchone()
    assert att["buyer_account_name"] == "环境-DEMO-A"                         # 外部尝试仍记下是谁买的
    assert orders.get_detail(conn, ws, out.order_id)["designated_buyer"] is None


def test_malformed_order_number_does_not_suppress_the_designation(conn, workspace):
    """格式不对的单号不算可信：任务进待核实，指定照常生效；「保留」后回到待分配，指定还在。"""
    ws, uid = workspace["id"], workspace["user_id"]
    out = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-H02", cid=ID_A, external_amz_order_no="重拍")
    assert out.verdict == "conflict" and out.status == "conflict"
    assert _des(conn, ws, out.order_id) == (ID_A, None)
    orders.resolve_conflict(conn, ws, out.order_id, "keep", None, uid)
    row = _row(conn, ws, out.order_id)
    assert row["status"] == "queued" and (row["designated_customer_id"], row["conflict"]) == (ID_A, None)


# ── 已有任务：同一条来源记录的变化 ────────────────────────────────────────────


def test_same_feishu_record_sets_changes_and_clears_the_designation(conn, workspace):
    """验收 修订二 第 4 条（接收部分）：同一条飞书记录 空 → A → B → 空，每次都是 update、记事件、不置关注；原样重读
    → unchanged。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recE01")
    oid = _ingest(conn, ws, src, "PO-DEMO-E01").order_id

    out = _ingest(conn, ws, src, "PO-DEMO-E01", cid=ID_A)
    assert out.verdict == "update" and out.status == "queued"
    assert out.changes == {"designated_customer_id": [None, ID_A]}           # 扁平键，只列变了的
    assert out.reason == "来源指定的买家号由「（未指定）」改为「A1DEMO0000」"
    assert _des(conn, ws, oid) == (ID_A, None)
    ev = _events(conn, oid, EVENT)
    assert len(ev) == 1 and ev[0]["actor_kind"] == "source"
    assert ev[0]["detail"] == {"changes": {"designated_customer_id": [None, ID_A]},
                               "source": {"kind": "feishu", "connection_id": src.connection_id, "record_id": "recE01"}}

    out = _ingest(conn, ws, src, "PO-DEMO-E01", cid=ID_B, buyer="环境-DEMO-B")
    assert out.verdict == "update"
    assert out.changes == {"designated_customer_id": [ID_A, ID_B], "designated_buyer_name": [None, "环境-DEMO-B"]}
    assert out.reason == "来源指定的买家号由「A1DEMO0000」改为「A1DEMO0001」"   # 有 ID 时写 ID
    assert _des(conn, ws, oid) == (ID_B, "环境-DEMO-B")

    assert _ingest(conn, ws, src, "PO-DEMO-E01", cid=ID_B, buyer="环境-DEMO-B").verdict == "unchanged"
    assert len(_events(conn, oid, EVENT)) == 2

    out = _ingest(conn, ws, src, "PO-DEMO-E01", buyer="环境-DEMO-B")           # 只去掉 ID
    assert out.changes == {"designated_customer_id": [ID_B, None]}
    assert out.reason == "来源指定的买家号由「A1DEMO0001」改为「环境-DEMO-B」"   # 没有 ID 时写名称

    out = _ingest(conn, ws, src, "PO-DEMO-E01")                              # 清空
    assert out.verdict == "update" and out.changes == {"designated_buyer_name": ["环境-DEMO-B", None]}
    assert out.reason == "来源指定的买家号由「环境-DEMO-B」改为「（未指定）」"
    row = _row(conn, ws, oid)
    assert (row["designated_customer_id"], row["designated_buyer_name"]) == (None, None)
    assert row["needs_attention"] is False and row["conflict"] is None and row["status"] == "queued"
    assert len(_events(conn, oid, EVENT)) == 4
    assert "order.upstream_changed_after_start" not in _kinds(conn, oid)
    assert NOT_ADOPTED not in _kinds(conn, oid)


def test_reason_shows_the_name_when_only_the_name_changed(conn, workspace):
    """ING-7：只有名称变了（ID 没变）时，原因不能写成「由「A1DEMO0000」改为「A1DEMO0000」」——这种修改正好会把任务
    从账号上拉下来（名称指向别的账号 → 解析不了），文案得看得出改了什么。两边显示成同一个标签时写成 名称（ID）。"""
    ws = workspace["id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B, priority=9)
    src = f.feishu_source(conn, ws, "recE05")
    oid = _ingest(conn, ws, src, "PO-DEMO-E05", cid=ID_A).order_id
    allocation.allocate(conn, ws, trigger="intake")
    assert _row(conn, ws, oid)["assigned_account_id"] == a["id"]
    out = _ingest(conn, ws, src, "PO-DEMO-E05", cid=ID_A, buyer="环境-DEMO-B")
    assert out.verdict == "update" and out.changes == {"designated_buyer_name": [None, "环境-DEMO-B"]}
    assert "由「A1DEMO0000」改为「A1DEMO0000」" not in out.reason
    assert out.reason == "来源指定的买家号由「A1DEMO0000」改为「环境-DEMO-B（A1DEMO0000）」"
    row = _row(conn, ws, oid)                                                 # 名称指向另一个账号：解除分配回待分配
    assert (row["status"], row["assigned_account_id"]) == ("queued", None)
    out = _ingest(conn, ws, src, "PO-DEMO-E05", cid=ID_A, buyer="环境-DEMO-A")   # 名称又改了一次：两边都带名称
    assert out.reason == "来源指定的买家号由「环境-DEMO-B（A1DEMO0000）」改为「环境-DEMO-A（A1DEMO0000）」"
    out = _ingest(conn, ws, src, "PO-DEMO-E05", cid=ID_B, buyer="环境-DEMO-A")   # ID 变了：照常只写 ID
    assert out.reason == "来源指定的买家号由「A1DEMO0000」改为「A1DEMO0001」"

    # 「未采用」的说明同理：另一种来源首次关联，ID 相同、名称不同
    other = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-E05", cid=ID_B, buyer="环境-DEMO-C")
    assert other.issues == [f"文件来源的买家号「环境-DEMO-C（A1DEMO0001）」与任务现有的指定"
                            f"「环境-DEMO-A（A1DEMO0001）」不同，{ADVICE}"]
    assert not any("「A1DEMO0001」与任务现有的指定「A1DEMO0001」" in i for i in other.issues)


def test_comparison_is_exact_so_a_case_only_correction_applies(conn, workspace):
    """陷阱：沿用 _same（忽略大小写、压缩空白）的话，把「demo-a」改对成「DEMO-A」不会生效。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recE02")
    oid = _ingest(conn, ws, src, "PO-DEMO-E02", buyer="环境-demo-a").order_id
    out = _ingest(conn, ws, src, "PO-DEMO-E02", buyer="环境-DEMO-A")
    assert out.verdict == "update" and out.changes == {"designated_buyer_name": ["环境-demo-a", "环境-DEMO-A"]}
    assert _des(conn, ws, oid) == (None, "环境-DEMO-A")
    out = _ingest(conn, ws, src, "PO-DEMO-E02", buyer="环境 - DEMO - A")       # 只改了空白也算变化
    assert out.verdict == "update" and _des(conn, ws, oid) == (None, "环境 - DEMO - A")
    # 清洗后相同的写法不算变化（首尾空白；ID 的大小写与空白）
    src2 = f.feishu_source(conn, ws, "recE03")
    oid2 = _ingest(conn, ws, src2, "PO-DEMO-E03", cid=ID_A, buyer="环境-DEMO-A").order_id
    again = _ingest(conn, ws, src2, "PO-DEMO-E03", cid=" a1demo0000 ", buyer=" 环境-DEMO-A ")
    assert again.changes == {} and _des(conn, ws, oid2) == (ID_A, "环境-DEMO-A")
    assert _events(conn, oid2, EVENT) == []


def test_other_field_changes_keep_their_own_reason_and_event(conn, workspace):
    """同一次里别的字段也变了：结论 update，changes 两类都有，'order.source_updated' 不含指定的键，reason 不写成指定。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recE04")
    oid = _ingest(conn, ws, src, "PO-DEMO-E04").order_id
    out = _ingest(conn, ws, src, "PO-DEMO-E04", cid=ID_A, sku="SKU-DEMO-2")
    assert out.verdict == "update" and set(out.changes) == {"sku", "designated_customer_id"}
    assert out.reason is None
    updated = _events(conn, oid, "order.source_updated")
    assert len(updated) == 1 and set(updated[0]["detail"]["changes"]) == {"sku"}
    assert len(_events(conn, oid, EVENT)) == 1


# ── 任何状态都写，不触发冲突 / 关注 ────────────────────────────────────────────


def _make(conn, ws, status, acc):
    """造一笔处在某个状态、还没有指定的任务（飞书来源）→ (任务 id, 来源, 接收用的记录字段)。"""
    no = f"PO-DEMO-S-{status}"
    src = f.feishu_source(conn, ws, f"recS-{status}")
    kw: dict = {}
    if status == "pending_review":
        kw = {"review": "unknown", "review_value": None}
    elif status == "needs_info":
        kw = {"asin": None}
    oid = _ingest(conn, ws, src, no, **kw).order_id
    row = _row(conn, ws, oid)
    if status in ("pending_review", "needs_info", "queued"):
        assert row["status"] == status
        return oid, src, no, kw
    if status == "conflict":
        orders._update(conn, ws, oid, {"conflict": {"kind": "fields", "source_kind": "file", "reason": "DEMO 冲突",
                                                    "fields": {}, "incoming": {}}})
        f.set_status(conn, oid, "conflict")
        return oid, src, no, kw
    assigned = dict(assigned_account_id=acc["id"], assignment_kind="auto", assigned_at=NOW, assignment_note="DEMO 分配")
    if status == "assigned":
        f.set_status(conn, oid, "assigned", **assigned)
    elif status in ("executing", "executing_submitted"):
        submitted = status.endswith("submitted")
        att = f.make_attempt(conn, ws, oid, acc["id"], status="submitting" if submitted else "checking",
                             submitted=submitted)
        f.set_status(conn, oid, "executing", current_attempt_id=att["id"], **assigned)
    elif status == "result_unknown":
        att = f.make_attempt(conn, ws, oid, acc["id"], status="unknown", submitted=True)
        f.set_status(conn, oid, "result_unknown", current_attempt_id=att["id"], **assigned)
    elif status == "exception":
        f.make_attempt(conn, ws, oid, acc["id"], status="failed")
        f.set_status(conn, oid, "exception", **assigned)
    elif status in ("purchased", "repurchase_pending"):
        att = f.make_attempt(conn, ws, oid, acc["id"], status="succeeded", simulated=True, submitted=True,
                             amazon_order_no="SIM-20261118-1")
        f.set_status(conn, oid, status, purchased_attempt_id=att["id"], **assigned)
    else:                                   # cancelled / closed
        f.set_status(conn, oid, status)
    return oid, src, no, kw


STARTED = ("executing", "executing_submitted", "result_unknown", "exception", "purchased", "repurchase_pending")


@pytest.mark.parametrize("status", ["pending_review", "needs_info", "conflict", "queued", "assigned", *STARTED,
                                    "cancelled", "closed"])
def test_designation_change_is_written_in_every_status_without_side_effects(conn, workspace, status):
    """陷阱：指定的变化不进「采购开始后修改」分支。任何状态都写两列、记事件、结论 update；不置 needs_attention、
    不记 order.upstream_changed_after_start、不产生冲突；在途的尝试不受影响。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws, name="环境-DEMO-现用")
    oid, src, no, kw = _make(conn, ws, status, acc)
    before = _row(conn, ws, oid)
    dry = _ingest(conn, ws, src, no, cid=ID_A, dry_run=True, **kw)
    out = _ingest(conn, ws, src, no, cid=ID_A, **kw)
    after = _row(conn, ws, oid)
    assert (dry.verdict, dry.status, dry.changes, dry.reason) == (out.verdict, out.status, out.changes, out.reason)
    assert out.verdict == "update" and out.changes == {"designated_customer_id": [None, ID_A]}
    assert (after["designated_customer_id"], after["designated_buyer_name"]) == (ID_A, None)
    assert after["needs_attention"] is False and after["attention_note"] is None
    assert after["conflict"] == before["conflict"]
    kinds = _kinds(conn, oid)
    assert kinds.count(EVENT) == 1
    assert not {"order.upstream_changed_after_start", "order.attention", "order.source_conflict"} & set(kinds)
    base = "来源指定的买家号由「（未指定）」改为「A1DEMO0000」"
    assert out.reason == (base + "；任务已开始采购，不影响这一次" if status in STARTED else base)
    if status == "assigned":
        # 原来自动分给「现用」，而来源指定了别的买家号：解除分配回待分配（随后的分配按指定落位）
        assert after["status"] == "queued" and after["assigned_account_id"] is None
        assert after["status_reason"] == "来源指定的买家号已不是账号 环境-DEMO-现用，已解除分配"
        assert "order.unassigned" in kinds
    else:
        assert after["status"] == before["status"]
        assert after["assigned_account_id"] == before["assigned_account_id"]
        assert after["current_attempt_id"] == before["current_attempt_id"]
    att = conn.execute("SELECT status FROM procure.purchase_attempts WHERE purchase_order_id = %s", (oid,)).fetchone()
    if status.startswith("executing"):
        assert att["status"] in ("checking", "submitting")                    # 在途的尝试原样继续


def test_after_start_field_change_and_designation_change_stay_separate(conn, workspace):
    """采购中同一次读取既改了 ASIN 又改了买家号：ASIN 照旧算「采购开始后修改」（结论 conflict、置关注），
    指定只是写入并记自己的事件，不出现在被挡住的变化里。"""
    ws = workspace["id"]
    acc = f.make_account(conn, ws)
    oid, src, no, _ = _make(conn, ws, "executing_submitted", acc)
    out = _ingest(conn, ws, src, no, cid=ID_A, asin="B0DEMO0002")
    assert out.verdict == "conflict" and set(out.changes) == {"asin", "designated_customer_id"}
    assert "ASIN" in out.reason and "买家号" not in out.reason
    blocked = _events(conn, oid, "order.upstream_changed_after_start")
    assert len(blocked) == 1 and set(blocked[0]["detail"]["changes"]) == {"asin"}
    row = _row(conn, ws, oid)
    assert row["designated_customer_id"] == ID_A and row["asin"] == "B0DEMO0001"
    assert row["needs_attention"] is True and "买家号" not in row["attention_note"]


# ── 首次关联：主次来源 ────────────────────────────────────────────────────────


def test_first_link_of_another_kind_fills_a_missing_designation(conn, workspace):
    """飞书为主、还没有指定的任务：文件第一次关联带了买家号 → 采用。"""
    ws = workspace["id"]
    oid = _ingest(conn, ws, f.feishu_source(conn, ws, "recF01"), "PO-DEMO-F01").order_id
    out = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F01", buyer="环境-DEMO-A")
    assert out.verdict == "update" and out.issues == []
    assert _des(conn, ws, oid) == (None, "环境-DEMO-A")
    ev = _events(conn, oid, EVENT)[0]["detail"]
    assert ev["source"]["kind"] == "file" and ev["changes"] == {"designated_buyer_name": [None, "环境-DEMO-A"]}


def test_first_link_of_another_kind_does_not_override_an_existing_designation(conn, workspace):
    """陷阱：旧文件重传不能悄悄盖掉飞书里的指定。飞书为主、已指定 A；文件第一次关联写了 B → 不采用，给说明。"""
    ws = workspace["id"]
    oid = _ingest(conn, ws, f.feishu_source(conn, ws, "recF02"), "PO-DEMO-F02", cid=ID_A).order_id
    out = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F02", buyer="环境-DEMO-B")
    assert out.verdict == "unchanged" and out.changes == {}
    assert out.issues == [f"文件来源的买家号「环境-DEMO-B」与任务现有的指定「A1DEMO0000」不同，{ADVICE}"]
    assert _des(conn, ws, oid) == (ID_A, None) and _events(conn, oid, EVENT) == []
    # 写的是同一个指定：没有什么可说的，也不留「未采用」的事件
    same = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F02", cid=ID_A)
    assert same.verdict == "unchanged" and same.issues == []
    assert len(_events(conn, oid, NOT_ADOPTED)) == 1


def test_feishu_first_link_does_not_override_a_file_primary_designation(conn, workspace):
    ws = workspace["id"]
    oid = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F03", buyer="环境-DEMO-A").order_id
    out = _ingest(conn, ws, f.feishu_source(conn, ws, "recF03"), "PO-DEMO-F03", cid=ID_B)
    assert out.issues == [f"飞书来源的买家号「A1DEMO0001」与任务现有的指定「环境-DEMO-A」不同，{ADVICE}"]
    assert _des(conn, ws, oid) == (None, "环境-DEMO-A")


def test_a_designation_that_is_not_adopted_leaves_one_event(conn, workspace):
    """ING-4：首次关联的买家号没被采用时，除了问题里的说明，还要在任务的时间线上留一条 order.designation_not_adopted
    （定时读取没有人看预览，之后这条关联就「已关联、没有变化」，再也没有地方能看到这句话）。同一条关联只记一次；
    预演不记。"""
    ws = workspace["id"]
    oid = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F06", cid=ID_A).order_id      # 文件为主，指定 A
    feishu = f.feishu_source(conn, ws, "recF06")
    dry = _ingest(conn, ws, feishu, "PO-DEMO-F06", buyer="环境-DEMO-B", dry_run=True)
    assert dry.verdict == "unchanged" and len(dry.issues) == 1 and _events(conn, oid, NOT_ADOPTED) == []
    out = _ingest(conn, ws, feishu, "PO-DEMO-F06", buyer="环境-DEMO-B")
    assert out.verdict == "unchanged" and out.issues == dry.issues
    ev = _events(conn, oid, NOT_ADOPTED)
    assert len(ev) == 1 and ev[0]["actor_kind"] == "source"
    assert ev[0]["detail"] == {
        "source": {"kind": "feishu", "connection_id": feishu.connection_id, "record_id": "recF06"},
        "stated": {"customer_id": None, "name": "环境-DEMO-B"}, "previous": None,
        "current": {"customer_id": ID_A, "name": None}}
    row = _row(conn, ws, oid)
    assert row["needs_attention"] is False and row["conflict"] is None and _des(conn, ws, oid) == (ID_A, None)
    # 第二次读取：这条关联说的与上次相同 → 没有新的说明，也没有第二条事件
    again = _ingest(conn, ws, feishu, "PO-DEMO-F06", buyer="环境-DEMO-B")
    assert again.verdict == "unchanged" and again.issues == []
    assert len(_events(conn, oid, NOT_ADOPTED)) == 1 and _events(conn, oid, EVENT) == []
    assert designation.EVENT_NOT_ADOPTED == NOT_ADOPTED


def test_first_link_that_agrees_on_the_columns_it_read_changes_nothing(conn, workspace):
    """ALLOC-1 的另一半保险：首次关联只读到其中一列、读到的这一列与任务现有的指定相同 → 不处理，没读到的那一列保持
    任务现值（不会把「ID + 名称」的指定改写成只有名称）。读到的列不同 → 主来源种类照常整组替换。"""
    ws = workspace["id"]
    oid = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F07", cid=ID_A, buyer="环境-DEMO-A").order_id

    def name_only(row_no):                                                     # 一份没有「买家号ID」列的文件
        src = f.file_source(conn, ws, row_no)
        return SourceRef(kind="file", snapshot=src.snapshot, batch_id=src.batch_id, row_no=src.row_no,
                         buyer_unread=(K_ID,))

    same = _ingest(conn, ws, name_only(2), "PO-DEMO-F07", buyer="环境-DEMO-A")
    assert same.verdict == "unchanged" and same.changes == {} and same.issues == []
    assert _des(conn, ws, oid) == (ID_A, "环境-DEMO-A") and _events(conn, oid, EVENT) == []
    blank = _ingest(conn, ws, name_only(3), "PO-DEMO-F07")                     # 读到的这一列空着：首次关联的空不清除
    assert blank.verdict == "unchanged" and _des(conn, ws, oid) == (ID_A, "环境-DEMO-A")
    moved = _ingest(conn, ws, name_only(4), "PO-DEMO-F07", buyer="环境-DEMO-B")  # 读到的这一列不同：主来源整组替换
    assert moved.changes == {"designated_customer_id": [ID_A, None],
                             "designated_buyer_name": ["环境-DEMO-A", "环境-DEMO-B"]}
    assert _des(conn, ws, oid) == (None, "环境-DEMO-B")
    # 另一种来源同样只读到一列、而且不同 → 不采用；相同 → 没有什么可说的
    feishu = f.feishu_source(conn, ws, "recF07")
    one = SourceRef(kind="feishu", snapshot={}, connection_id=feishu.connection_id, record_id="recF07",
                    buyer_unread=(K_ID,))
    out = _ingest(conn, ws, one, "PO-DEMO-F07", buyer="环境-DEMO-C")
    assert out.issues == [f"飞书来源的买家号「环境-DEMO-C」与任务现有的指定「环境-DEMO-B」不同，{ADVICE}"]
    assert _des(conn, ws, oid) == (None, "环境-DEMO-B")


def test_new_file_batch_of_the_primary_kind_replaces_but_blank_does_not_clear(conn, workspace):
    """文件为主：新批次（首次关联）写了新的买家号 → 采用；空着重新上传不会清除指定。"""
    ws = workspace["id"]
    oid = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F04", buyer="环境-DEMO-A").order_id
    out = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F04", buyer="环境-DEMO-B")
    assert out.verdict == "update" and _des(conn, ws, oid) == (None, "环境-DEMO-B")
    blank = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F04")
    assert blank.verdict == "unchanged" and blank.issues == []
    assert _des(conn, ws, oid) == (None, "环境-DEMO-B")
    # 只有一列的文件：两列作为一组替换（ID 没写就是没有 ID）
    one = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F04", cid=ID_A)
    assert one.changes == {"designated_customer_id": [None, ID_A], "designated_buyer_name": ["环境-DEMO-B", None]}
    assert _des(conn, ws, oid) == (ID_A, None)


def test_a_linked_record_of_any_kind_applies_its_own_change(conn, workspace):
    """已关联过的来源记录这次说的与它上次不同 → 应用，不分主次（这是表里刚发生的修改）。"""
    ws = workspace["id"]
    file_src = f.file_source(conn, ws, 2)
    oid = _ingest(conn, ws, file_src, "PO-DEMO-F05", buyer="环境-DEMO-A").order_id        # 文件为主
    feishu = f.feishu_source(conn, ws, "recF05")
    assert _ingest(conn, ws, feishu, "PO-DEMO-F05").verdict == "unchanged"                 # 飞书首次关联：空，不处理
    out = _ingest(conn, ws, feishu, "PO-DEMO-F05", cid=ID_B)                               # 飞书这条记录后来填了
    assert out.verdict == "update" and _des(conn, ws, oid) == (ID_B, None)
    out = _ingest(conn, ws, feishu, "PO-DEMO-F05")                                         # 又清空了
    assert out.verdict == "update" and _des(conn, ws, oid) == (None, None)
    # 两条来源各说各的、都没再变：不会来回抖动
    assert _ingest(conn, ws, feishu, "PO-DEMO-F05").verdict == "unchanged"
    assert _ingest(conn, ws, file_src, "PO-DEMO-F05", buyer="环境-DEMO-A").verdict == "unchanged"
    assert _des(conn, ws, oid) == (None, None)
    assert _events(conn, oid, NOT_ADOPTED) == []                                           # 撤回的是它自己写的：照常清空


def test_withdrawing_a_value_that_was_never_adopted_does_not_clear_the_designation(conn, workspace):
    """ING-3：次要来源写的买家号当初没被采用（任务的指定是主来源写的）；它把这个格子清空，不能把主来源的指定清掉。
    说明里写明保持不变，记一条 order.designation_not_adopted（previous = 它上次写的）。之后它再写别的买家号，
    按「这条来源自己改了」应用——首次关联的说明里已经这样告知。"""
    ws = workspace["id"]
    oid = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F08", cid=ID_A).order_id      # 文件为主，指定 A
    feishu = f.feishu_source(conn, ws, "recF08")
    first = _ingest(conn, ws, feishu, "PO-DEMO-F08", buyer="环境-DEMO-B")                      # 没被采用
    assert first.issues == [f"飞书来源的买家号「环境-DEMO-B」与任务现有的指定「A1DEMO0000」不同，{ADVICE}"]

    dry = _ingest(conn, ws, feishu, "PO-DEMO-F08", dry_run=True)
    out = _ingest(conn, ws, feishu, "PO-DEMO-F08")                                             # 清空了那个没被采用的 B
    assert (dry.verdict, dry.changes, dry.issues) == (out.verdict, out.changes, out.issues)
    assert out.verdict == "unchanged" and out.changes == {}
    assert out.issues == ["飞书来源清空了买家号「环境-DEMO-B」，任务现有的指定「A1DEMO0000」不是这条来源写的，保持不变"]
    assert _des(conn, ws, oid) == (ID_A, None) and _events(conn, oid, EVENT) == []
    ev = _events(conn, oid, NOT_ADOPTED)
    assert len(ev) == 2
    assert ev[1]["detail"] == {
        "source": {"kind": "feishu", "connection_id": feishu.connection_id, "record_id": "recF08"},
        "stated": {"customer_id": None, "name": None}, "previous": {"customer_id": None, "name": "环境-DEMO-B"},
        "current": {"customer_id": ID_A, "name": None}}
    again = _ingest(conn, ws, feishu, "PO-DEMO-F08")                                           # 再读：没有变化，只记一次
    assert again.verdict == "unchanged" and again.issues == [] and len(_events(conn, oid, NOT_ADOPTED)) == 2

    out = _ingest(conn, ws, feishu, "PO-DEMO-F08", cid=ID_B)                                   # 之后改成别的：应用
    assert out.verdict == "update" and _des(conn, ws, oid) == (ID_B, None)
    out = _ingest(conn, ws, feishu, "PO-DEMO-F08")                                             # 撤回它自己写的：清空
    assert out.verdict == "update" and _des(conn, ws, oid) == (None, None)
    assert len(_events(conn, oid, NOT_ADOPTED)) == 2 and len(_events(conn, oid, EVENT)) == 2


def test_clearing_the_cell_this_link_itself_set_still_clears(conn, workspace):
    """对照：任务现有的指定正是这条来源上次写的 → 清空照常生效（不分主次来源）。"""
    ws = workspace["id"]
    oid = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-F09").order_id               # 文件为主，没有指定
    feishu = f.feishu_source(conn, ws, "recF09")
    assert _ingest(conn, ws, feishu, "PO-DEMO-F09", cid=ID_B).verdict == "update"             # 任务还没有指定：采用
    out = _ingest(conn, ws, feishu, "PO-DEMO-F09")
    assert out.verdict == "update" and out.issues == [] and _des(conn, ws, oid) == (None, None)
    assert _events(conn, oid, NOT_ADOPTED) == []


# ── 人工改派 ─────────────────────────────────────────────────────────────────


def test_manual_assignment_keeps_its_account_and_gets_an_issue(conn, workspace):
    """验收 修订二 第 5 条（接收部分）：人工改派的任务照样写两列，但留在改派的账号上，并提示来源的指定暂不生效。"""
    ws, uid = workspace["id"], workspace["user_id"]
    f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    b = f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B)
    src = f.feishu_source(conn, ws, "recM01")
    oid = _ingest(conn, ws, src, "PO-DEMO-M01").order_id
    orders.assign_manual(conn, ws, oid, b["id"], uid)
    out = _ingest(conn, ws, src, "PO-DEMO-M01", cid=ID_A)
    assert out.verdict == "update" and out.status == "assigned"
    assert out.issues == ["任务已人工改派到 环境-DEMO-B，来源的指定暂不生效；解除分配后按来源指定分配"]
    row = _row(conn, ws, oid)
    assert (row["assigned_account_id"], row["assignment_kind"], row["status"]) == (b["id"], "manual", "assigned")
    assert row["designated_customer_id"] == ID_A
    assert "order.unassigned" not in _kinds(conn, oid)
    detail = orders.get_detail(conn, ws, oid)
    assert detail["assigned_account"]["name"] == "环境-DEMO-B"
    assert detail["designated_buyer"]["account"]["name"] == "环境-DEMO-A"     # 两边都看得到
    # 来源清空指定：照样写，不再提示
    out = _ingest(conn, ws, src, "PO-DEMO-M01")
    assert out.verdict == "update" and out.issues == []
    assert _row(conn, ws, oid)["assigned_account_id"] == b["id"]


def test_ingest_unassigns_an_auto_task_that_no_longer_matches_but_keeps_one_that_does(conn, workspace):
    """§E.6：来源指定了买家号，非人工分配已不在指定的账号上 → 解除分配；本来就在指定的账号上 → 保留。"""
    ws = workspace["id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    b = f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B)
    src1, src2 = f.feishu_source(conn, ws, "recM02"), f.feishu_source(conn, ws, "recM03")
    on_b = _ingest(conn, ws, src1, "PO-DEMO-M02").order_id
    on_a = _ingest(conn, ws, src2, "PO-DEMO-M03").order_id
    auto = dict(assignment_kind="auto", assigned_at=NOW, assignment_note="DEMO 分配")
    f.set_status(conn, on_b, "assigned", assigned_account_id=b["id"], **auto)
    f.set_status(conn, on_a, "assigned", assigned_account_id=a["id"], **auto)
    out = _ingest(conn, ws, src1, "PO-DEMO-M02", buyer="环境-DEMO-A")
    assert out.status == "queued"
    row = _row(conn, ws, on_b)
    assert row["assigned_account_id"] is None and row["assignment_kind"] is None
    assert row["status_reason"] == "来源指定的买家号已不是账号 环境-DEMO-B，已解除分配"
    out = _ingest(conn, ws, src2, "PO-DEMO-M03", buyer="环境-demo-a")          # 宽松匹配到同一个账号
    assert out.status == "assigned" and _row(conn, ws, on_a)["assigned_account_id"] == a["id"]


# ── 写回读回、同意重采 ────────────────────────────────────────────────────────


def test_own_writeback_read_back_stays_unchanged_and_repurchase_keeps_the_designation(conn, workspace):
    """验收 修订二 第 3 条：本系统写回（买家号、买家号 ID、AMZ 订单号、已拍单）读回来 → unchanged，指定不变；
    亚马逊取消并同意重采后，任务仍带着原来的指定，下一次分配还给原来指定的账号。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B, priority=9)
    src = f.feishu_source(conn, ws, "recW01")
    oid = _ingest(conn, ws, src, "PO-DEMO-W01", buyer="环境-DEMO-A").order_id
    allocation.allocate(conn, ws, trigger="intake")
    assert _row(conn, ws, oid)["assigned_account_id"] == a["id"]
    att = f.make_attempt(conn, ws, oid, a["id"], status="succeeded", simulated=False, submitted=True,
                         amazon_order_no="111-0000000-0000301")
    f.set_status(conn, oid, "purchased", purchased_attempt_id=att["id"])
    written = dict(buyer="环境-DEMO-A", cid=ID_A, external_amz_order_no="111-0000000-0000301",
                   purchase_status_value="已拍单")
    dry = _ingest(conn, ws, src, "PO-DEMO-W01", dry_run=True, **written)
    out = _ingest(conn, ws, src, "PO-DEMO-W01", **written)
    assert dry.verdict == out.verdict == "unchanged" and out.changes == {}
    assert _des(conn, ws, oid) == (None, "环境-DEMO-A") and _events(conn, oid, EVENT) == []
    assert _ingest(conn, ws, src, "PO-DEMO-W01", **written).verdict == "unchanged"

    orders.mark_amazon_cancelled(conn, ws, oid, None, uid)
    orders.decide_repurchase(conn, ws, oid, "approve", None, uid)
    row = _row(conn, ws, oid)
    assert row["status"] == "queued" and row["assigned_account_id"] is None
    assert (row["designated_customer_id"], row["designated_buyer_name"]) == (None, "环境-DEMO-A")
    assert _ingest(conn, ws, src, "PO-DEMO-W01", **written).verdict == "unchanged"      # 行上还带着旧单号：不表态
    allocation.allocate(conn, ws, trigger="manual")
    row = _row(conn, ws, oid)
    assert (row["status"], row["assigned_account_id"], row["assignment_kind"]) == ("assigned", a["id"], "source")


def test_auto_task_stays_auto_after_writeback_and_repurchase(conn, workspace):
    """之前是自动分配的任务：写回把买家号写到了行上（带单号，不表态）→ 同意重采后还是自动分配。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    src = f.feishu_source(conn, ws, "recW02")
    oid = _ingest(conn, ws, src, "PO-DEMO-W02").order_id
    att = f.make_attempt(conn, ws, oid, a["id"], status="succeeded", simulated=False, submitted=True,
                         amazon_order_no="111-0000000-0000302")
    f.set_status(conn, oid, "purchased", purchased_attempt_id=att["id"])
    out = _ingest(conn, ws, src, "PO-DEMO-W02", buyer="环境-DEMO-A", cid=ID_A,
                  external_amz_order_no="111-0000000-0000302")
    assert out.verdict == "unchanged" and _des(conn, ws, oid) == (None, None)
    orders.mark_amazon_cancelled(conn, ws, oid, None, uid)
    orders.decide_repurchase(conn, ws, oid, "approve", None, uid)
    allocation.allocate(conn, ws, trigger="manual")
    row = _row(conn, ws, oid)
    assert row["assignment_kind"] == "auto" and _des(conn, ws, oid) == (None, None)


def test_buyer_written_back_without_an_order_number_is_not_a_designation(conn, workspace):
    """写回对应里没有 AMZ 订单号时，行上只多了买家号：本系统已真实采购成功的任务不把它当成指定。"""
    ws = workspace["id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    src = f.feishu_source(conn, ws, "recW03")
    oid = _ingest(conn, ws, src, "PO-DEMO-W03").order_id
    att = f.make_attempt(conn, ws, oid, a["id"], status="succeeded", simulated=False, submitted=True,
                         amazon_order_no="111-0000000-0000303")
    f.set_status(conn, oid, "purchased", purchased_attempt_id=att["id"])
    out = _ingest(conn, ws, src, "PO-DEMO-W03", buyer="环境-DEMO-A", cid=ID_A)
    assert out.verdict == "unchanged" and _des(conn, ws, oid) == (None, None)
    assert _events(conn, oid, EVENT) == []
    # 模拟成功不算真实采购：指定照常写入（模拟结果不写回，行上的买家号只能是人填的）
    sim_src = f.feishu_source(conn, ws, "recW04")
    sim = _ingest(conn, ws, sim_src, "PO-DEMO-W04").order_id
    satt = f.make_attempt(conn, ws, sim, a["id"], status="succeeded", simulated=True, submitted=True,
                          amazon_order_no="SIM-20261118-9")
    f.set_status(conn, sim, "purchased", purchased_attempt_id=satt["id"])
    assert _ingest(conn, ws, sim_src, "PO-DEMO-W04", cid=ID_A).verdict == "update"
    assert _des(conn, ws, sim) == (ID_A, None)


def test_existing_task_gaining_a_credible_order_number_keeps_its_designation(conn, workspace):
    """已有任务的行补上可信 AMZ 订单号：任务按外部单号转已采购，指定既不设置也不清除。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recW05")
    oid = _ingest(conn, ws, src, "PO-DEMO-W05", cid=ID_A).order_id
    out = _ingest(conn, ws, src, "PO-DEMO-W05", cid=ID_B, buyer="环境-DEMO-B",
                  external_amz_order_no="111-0000000-0000305")
    assert out.verdict == "historical" and out.status == "purchased"
    assert _des(conn, ws, oid) == (ID_A, None)                               # 行上的 B 只是「谁买的」
    assert "designated_customer_id" not in out.changes


def test_blank_after_a_silent_reading_does_not_clear(conn, workspace):
    """ING-6（上次不表态的那一半）：这条来源上次带可信单号（不表态），这次单号没了、买家号两列空着 → 不是「清空」，
    指定保持不变。陷阱：把「上次不表态」当成「上次说的是空」的反面处理，会把指定清掉。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recS1")
    oid = _ingest(conn, ws, src, "PO-DEMO-S1", cid=ID_A).order_id
    silent = _ingest(conn, ws, src, "PO-DEMO-S1", cid=ID_B, buyer="环境-DEMO-B",
                     external_amz_order_no="111-0000000-0000777")
    assert silent.verdict == "historical" and _des(conn, ws, oid) == (ID_A, None)
    out = _ingest(conn, ws, src, "PO-DEMO-S1")                                # 单号没了，买家号空着
    assert "designated_customer_id" not in out.changes and "designated_buyer_name" not in out.changes
    assert _des(conn, ws, oid) == (ID_A, None) and _events(conn, oid, EVENT) == []


def test_named_buyer_after_a_silent_reading_applies(conn, workspace):
    """ING-6（另一半）：原来按历史采购登记的行（带可信单号、不表态），后来单号去掉并写了买家号 → 指定生效。
    陷阱：「上次不表态」一律不处理的话，这笔任务会被自动分配。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recS2")
    hist = _ingest(conn, ws, src, "PO-DEMO-S2", buyer="环境-DEMO-B", cid=ID_B,
                   external_amz_order_no="111-0000000-0000778")
    oid = hist.order_id
    assert hist.verdict == "historical" and _des(conn, ws, oid) == (None, None)
    out = _ingest(conn, ws, src, "PO-DEMO-S2", cid=ID_A)
    assert out.changes["designated_customer_id"] == [None, ID_A]
    assert _des(conn, ws, oid) == (ID_A, None) and len(_events(conn, oid, EVENT)) == 1


# ── 来源读不到买家号列 ────────────────────────────────────────────────────────


def _unreadable(src: SourceRef) -> SourceRef:
    return SourceRef(kind=src.kind, snapshot=src.snapshot, connection_id=src.connection_id, record_id=src.record_id,
                     buyer_readable=False)


def _without(src: SourceRef, *keys: str) -> SourceRef:
    """同一条飞书来源，这次没读到 keys 这些买家号字段（列被改名、删除，或取消了对应）。"""
    return SourceRef(kind=src.kind, snapshot=src.snapshot, connection_id=src.connection_id, record_id=src.record_id,
                     buyer_unread=keys)


def _stored(conn, oid, src: SourceRef) -> dict:
    return conn.execute("SELECT normalized FROM procure.order_sources WHERE purchase_order_id = %s "
                        "AND feishu_record_id = %s", (oid, src.record_id)).fetchone()["normalized"]


def test_unreadable_buyer_columns_neither_set_nor_clear_and_remember_the_last_reading(conn, workspace):
    """验收 修订二 第 4 条（没读到不等于清空）：来源读不到买家号列的那次读取不动指定，来源关联里保留上次读到的买家号；
    列恢复后，期间清空的格子照样清空指定。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recU01")
    oid = _ingest(conn, ws, src, "PO-DEMO-U01", cid=ID_A, buyer="环境-DEMO-A").order_id
    out = _ingest(conn, ws, _unreadable(src), "PO-DEMO-U01")                  # 列找不到：记录里两列是空的
    assert out.verdict == "unchanged" and out.changes == {}
    assert _des(conn, ws, oid) == (ID_A, "环境-DEMO-A")
    link = conn.execute("SELECT normalized FROM procure.order_sources WHERE purchase_order_id = %s", (oid,)).fetchone()
    assert link["normalized"]["external_buyer_customer_id"] == ID_A
    assert link["normalized"]["external_buyer_account"] == "环境-DEMO-A"
    # 列恢复、值没变 → 无变化；期间格子被清空 → 清除指定
    assert _ingest(conn, ws, src, "PO-DEMO-U01", cid=ID_A, buyer="环境-DEMO-A").verdict == "unchanged"
    _ingest(conn, ws, _unreadable(src), "PO-DEMO-U01")
    out = _ingest(conn, ws, src, "PO-DEMO-U01")
    assert out.verdict == "update" and _des(conn, ws, oid) == (None, None)


def test_unreadable_buyer_columns_on_new_and_first_linked_records(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recU02")
    new = _ingest(conn, ws, _unreadable(src), "PO-DEMO-U02", cid=ID_A)        # 不表态：新任务不带指定
    assert new.verdict == "new" and _des(conn, ws, new.order_id) == (None, None)
    assert _stored(conn, new.order_id, src)["buyer_unread"] == [K_ID, K_NAME]  # 这条关联两列都还没读到过
    out = _ingest(conn, ws, src, "PO-DEMO-U02", cid=ID_A)                     # 之后读得到了：任务没有指定 → 采用
    assert out.verdict == "update" and _des(conn, ws, new.order_id) == (ID_A, None)
    assert "buyer_unread" not in _stored(conn, new.order_id, src)             # 都读到过了：标记去掉


def test_one_unread_column_keeps_its_part_while_the_other_column_still_applies(conn, workspace):
    """ING-2 / DOC-1(b)：读没读到按列算。「买家号 ID」这一列没读到（被改名、删除，或取消了对应）时，它那一半指定保持
    上次读到的内容，「买家号」那一列的修改照常应用；反过来也一样。列恢复后，期间清空的格子照样清除。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recU03")
    oid = _ingest(conn, ws, src, "PO-DEMO-U03", cid=ID_A, buyer="环境-DEMO-A").order_id
    out = _ingest(conn, ws, _without(src, K_ID), "PO-DEMO-U03", buyer="环境-DEMO-A")    # ID 列没读到，名称没变
    assert out.verdict == "unchanged" and out.changes == {}
    assert _des(conn, ws, oid) == (ID_A, "环境-DEMO-A")
    stored = _stored(conn, oid, src)
    assert (stored[K_ID], stored[K_NAME]) == (ID_A, "环境-DEMO-A") and "buyer_unread" not in stored
    out = _ingest(conn, ws, _without(src, K_ID), "PO-DEMO-U03", buyer="环境-DEMO-B")    # 读得到的那一列改了
    assert out.verdict == "update" and out.changes == {"designated_buyer_name": ["环境-DEMO-A", "环境-DEMO-B"]}
    assert _des(conn, ws, oid) == (ID_A, "环境-DEMO-B")                                 # ID 那一半既不设置也不清除
    out = _ingest(conn, ws, _without(src, K_ID), "PO-DEMO-U03")                         # 读得到的那一列清空了
    assert out.changes == {"designated_buyer_name": ["环境-DEMO-B", None]} and _des(conn, ws, oid) == (ID_A, None)
    out = _ingest(conn, ws, _without(src, K_NAME), "PO-DEMO-U03", cid=ID_B)             # 换成另一列没读到
    assert out.changes == {"designated_customer_id": [ID_A, ID_B]} and _des(conn, ws, oid) == (ID_B, None)
    out = _ingest(conn, ws, _without(src, K_ID), "PO-DEMO-U03")                         # ID 列又没读到：不清除
    assert out.verdict == "unchanged" and _des(conn, ws, oid) == (ID_B, None)
    out = _ingest(conn, ws, src, "PO-DEMO-U03")                                         # 都恢复了，格子已清空
    assert out.verdict == "update" and _des(conn, ws, oid) == (None, None)
    assert len(_events(conn, oid, EVENT)) == 4 and _events(conn, oid, NOT_ADOPTED) == []


def test_link_marker_is_absent_unless_a_column_was_never_read(conn, workspace):
    """关联上的「从来没读到过」标记：两列都读到过的关联不写这个键（已有关联的规范化记录逐字不变，升级不引发整表更新，
    飞书的「与上次完全相同」快速路径照旧命中）；首次关联时没读到的字段记进去，读到一次就去掉，之后再读不到也不会回来。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recU04")
    rec = f.make_record(upstream_order_no="PO-DEMO-U04", external_buyer_customer_id=ID_A)
    oid = orders.ingest(conn, ws, rec, src).order_id
    stored = _stored(conn, oid, src)
    assert stored == rec.to_json() and "buyer_unread" not in stored
    link = {"normalized": stored}
    assert orders.link_normalized(rec, src, link) == stored                             # 快速路径用的比较
    assert orders.link_normalized(rec, _without(src, K_NAME), link) == stored           # 没读到的那一列本来就是空
    blank = f.make_record(upstream_order_no="PO-DEMO-U04")
    assert orders.link_normalized(blank, _without(src, K_ID), link) == stored           # 没读到的 ID 沿用上次读到的
    assert orders.link_normalized(blank, _unreadable(src), link) == stored
    assert orders.link_normalized(blank, src, link) != stored                           # 读到了、而且清空了

    first = orders.link_normalized(rec, _without(src, K_ID), None)                      # 首次关联：没读到的记进标记
    assert first[K_ID] is None and first["buyer_unread"] == [K_ID]
    still = orders.link_normalized(rec, _without(src, K_ID, K_NAME), {"normalized": first})
    assert still["buyer_unread"] == [K_ID]                                              # 旧标记 ∩ 这次仍没读到的
    assert orders.link_normalized(rec, _without(src, K_ID), {"normalized": first}) == first
    seen = orders.link_normalized(rec, src, {"normalized": first})
    assert seen[K_ID] == ID_A and "buyer_unread" not in seen
    later = orders.link_normalized(blank, _without(src, K_ID), {"normalized": seen})
    assert later[K_ID] == ID_A and "buyer_unread" not in later                          # 读到过：不再算「从来没读到」


def test_first_reading_of_a_column_on_a_known_link_follows_the_first_link_rule(conn, workspace):
    """ING-3 变体：飞书关联第一次建立时读不到买家号列（存下的两列是空的）。列恢复后第一次读到的买家号不是「这条来源
    自己改了」，要按首次关联对待：任务以文件为主、已有指定 → 不采用并说明；不能凭「与上次不同」盖掉主来源的指定。"""
    ws = workspace["id"]
    oid = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-U05", cid=ID_A).order_id      # 文件为主，指定 A
    feishu = f.feishu_source(conn, ws, "recU05")
    first = _ingest(conn, ws, _unreadable(feishu), "PO-DEMO-U05", cid=ID_B)                    # 关联时读不到
    assert first.verdict == "unchanged" and first.issues == [] and _des(conn, ws, oid) == (ID_A, None)
    out = _ingest(conn, ws, feishu, "PO-DEMO-U05", cid=ID_B)                                   # 第一次读到
    assert out.verdict == "unchanged" and out.changes == {}
    assert out.issues == [f"飞书来源的买家号「A1DEMO0001」与任务现有的指定「A1DEMO0000」不同，{ADVICE}"]
    assert _des(conn, ws, oid) == (ID_A, None) and _events(conn, oid, EVENT) == []
    assert len(_events(conn, oid, NOT_ADOPTED)) == 1
    again = _ingest(conn, ws, feishu, "PO-DEMO-U05", cid=ID_B)                                 # 已经读到过：不再提
    assert again.issues == [] and len(_events(conn, oid, NOT_ADOPTED)) == 1
    out = _ingest(conn, ws, feishu, "PO-DEMO-U05", cid="A1DEMO0002")                           # 之后它自己改了：应用
    assert out.verdict == "update" and _des(conn, ws, oid) == ("A1DEMO0002", None)

    # 只有一列当时没读到：那一列第一次读到同样按首次关联；读到过的那一列的修改照常是「自己改了」
    oid2 = _ingest(conn, ws, f.file_source(conn, ws, 3), "PO-DEMO-U06", buyer="环境-DEMO-A").order_id
    f2 = f.feishu_source(conn, ws, "recU06")
    _ingest(conn, ws, _without(f2, K_ID), "PO-DEMO-U06")                                       # 关联时名称读到了（空）
    out = _ingest(conn, ws, f2, "PO-DEMO-U06", cid=ID_B)                                       # ID 列第一次读到
    assert out.verdict == "unchanged" and len(out.issues) == 1 and _des(conn, ws, oid2) == (None, "环境-DEMO-A")
    out = _ingest(conn, ws, f2, "PO-DEMO-U06", cid=ID_B, buyer="环境-DEMO-B")                  # 读到过的名称列改了
    assert out.verdict == "update" and _des(conn, ws, oid2) == (ID_B, "环境-DEMO-B")
    # 对照：首次关联读得到、而且任务还没有指定 → 第一次读到就采用
    oid3 = _ingest(conn, ws, f.file_source(conn, ws, 4), "PO-DEMO-U07").order_id
    f3 = f.feishu_source(conn, ws, "recU07")
    _ingest(conn, ws, _unreadable(f3), "PO-DEMO-U07", cid=ID_B)
    assert _ingest(conn, ws, f3, "PO-DEMO-U07", cid=ID_B).verdict == "update" and _des(conn, ws, oid3) == (ID_B, None)


# ── 不该动指定的动作 ──────────────────────────────────────────────────────────


def test_unassign_transition_resolve_conflict_and_simulation_reset_keep_the_designation(conn, workspace):
    """解除分配、转待核实 / 取消等清分配的迁移、处理冲突（接受来源新值）、清除模拟结果都不动指定两列。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    src = f.feishu_source(conn, ws, "recK01")
    oid = _ingest(conn, ws, src, "PO-DEMO-K01", cid=ID_A, buyer="环境-DEMO-A").order_id
    want = (ID_A, "环境-DEMO-A")
    allocation.allocate(conn, ws, trigger="intake")
    assert _row(conn, ws, oid)["assignment_kind"] == "source"
    orders.unassign(conn, ws, oid, uid)
    assert _des(conn, ws, oid) == want and _row(conn, ws, oid)["status"] == "queued"

    # 另一种来源的字段冲突 → 待核实（解除分配）→ 接受来源新值
    allocation.allocate(conn, ws, trigger="manual")
    out = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-K01", cid=ID_B, asin="B0DEMO0009")
    assert out.verdict == "conflict" and out.status == "conflict"
    row = _row(conn, ws, oid)
    assert set(row["conflict"]["incoming"]) == {"asin"}                       # 指定不进冲突载荷
    assert _des(conn, ws, oid) == want                                        # 文件（非主来源）的 B 没有采用
    orders.resolve_conflict(conn, ws, oid, "accept", None, uid)
    row = _row(conn, ws, oid)
    assert row["asin"] == "B0DEMO0009" and _des(conn, ws, oid) == want

    # 模拟成功 → 清除模拟结果：指定与来源指定的分配都保留
    allocation.allocate(conn, ws, trigger="manual")
    att = f.make_attempt(conn, ws, oid, a["id"], status="succeeded", simulated=True, submitted=True,
                         amazon_order_no="SIM-20261118-2")
    f.set_status(conn, oid, "purchased", purchased_attempt_id=att["id"])
    conn.execute("UPDATE procure.purchase_attempts SET status = 'aborted', resolution = 'simulation_reset' "
                 "WHERE id = %s", (att["id"],))
    orders.revert_simulation(conn, ws, oid)
    row = _row(conn, ws, oid)
    assert (row["status"], row["assigned_account_id"], row["assignment_kind"]) == ("assigned", a["id"], "source")
    assert _des(conn, ws, oid) == want
    orders.cancel(conn, ws, oid, None, uid)
    assert _des(conn, ws, oid) == want and _row(conn, ws, oid)["assignment_kind"] is None


def test_same_record_moved_to_another_order_number_leaves_the_old_task_alone(conn, workspace):
    """同一条来源记录改了平台订单号且带买家号：原任务转待核实，不处理它的指定。"""
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recK02")
    oid = _ingest(conn, ws, src, "PO-DEMO-K02", cid=ID_A).order_id
    out = _ingest(conn, ws, src, "PO-DEMO-K02-NEW", cid=ID_B)
    assert out.verdict == "conflict" and out.order_id == oid
    assert _des(conn, ws, oid) == (ID_A, None)
    assert f.count(conn, "purchase_orders", "workspace_id = %s", (ws,)) == 1


def test_fingerprint_and_record_shape_do_not_carry_the_designation(conn, workspace):
    """指定不进执行指纹、不加记录字段：带不带买家号指纹相同，领取后只改买家号不算来源变化。"""
    with_buyer = f.make_record(upstream_order_no="PO-DEMO-K03", external_buyer_customer_id=ID_A,
                               external_buyer_account="环境-DEMO-A")
    without = f.make_record(upstream_order_no="PO-DEMO-K03")
    assert with_buyer.execution_fingerprint() == without.execution_fingerprint()
    assert not any(k.startswith("designated") for k in with_buyer.to_json())
    plain = SourceRef(kind="file", snapshot={}, batch_id=1, row_no=2)
    assert plain.buyer_readable is True and plain.buyer_unread == () and designation.unread_keys(plain) == ()


def test_dry_run_gives_the_same_verdict_without_writing(conn, workspace):
    ws = workspace["id"]
    src = f.feishu_source(conn, ws, "recK04")
    oid = _ingest(conn, ws, src, "PO-DEMO-K04").order_id
    before = _row(conn, ws, oid)
    dry = _ingest(conn, ws, src, "PO-DEMO-K04", cid=ID_A, buyer="环境-DEMO-A", dry_run=True)
    assert dry.verdict == "update"
    assert dry.changes == {"designated_customer_id": [None, ID_A], "designated_buyer_name": [None, "环境-DEMO-A"]}
    assert _row(conn, ws, oid) == before and _events(conn, oid, EVENT) == []
    other = _ingest(conn, ws, f.file_source(conn, ws, 2), "PO-DEMO-K04", cid=ID_A, dry_run=True)
    assert other.verdict == "update"                                          # 预览：任务还没有指定，文件的会被采用
    new = _ingest(conn, ws, f.file_source(conn, ws, 3), "PO-DEMO-K05", cid=ID_A, dry_run=True)
    assert new.verdict == "new" and new.order_id is None
    assert f.count(conn, "purchase_orders", "workspace_id = %s", (ws,)) == 1
