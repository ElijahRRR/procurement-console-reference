"""来源指定买家号，从来源到分配的整条路（方案 v2 修订二）：飞书读取（改买家号、清空、找不到买家号列时新记录暂不
接收且另一列照常读取、取消一个字段的对应、没采用时留事件、占位写法、表格错误值）、文件导入与预览、导出再导入
（指定一列不丢）、规范化规则、待登记的买家号并入来源指定的 ID、概览计数。全部数据虚构。"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import openpyxl
import pytest

from services import accounts, allocation, clock, exports, feishu_intake, file_mapping, imports, normalize, orders
from services import feishu_connections as fc
from services import overview as overview_svc
from services import sheet_io
from tests import factories as f
from tests.fake_feishu import (DEMO_APP, DEMO_PURCHASER, DEMO_TABLE, ORDER_TABLE_FIELDS, TEXT, FakeFeishuClient,
                               create_demo_connection, order_fields, text)

NOW = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)   # 北京 2026-11-18 10:00
ID_A, ID_B = "A1DEMO0000", "A1DEMO0001"
ADVICE = "未采用；要改请在主来源里改，或在控制台改派（这条来源之后再改买家号，会按改后的应用）"


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


@pytest.fixture()
def fake(monkeypatch):
    client = FakeFeishuClient.with_order_table()
    monkeypatch.setattr(fc, "make_client", lambda: client)
    return client


def _add(fake, rid, no, **kw):
    fake.add_record(DEMO_APP, DEMO_TABLE, rid, order_fields(no, **kw))


def _edit(fake, rid, **kw):
    fake.edit_record(DEMO_APP, DEMO_TABLE, rid, **kw)


def _order(conn, ws, no):
    return conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = %s",
                        (ws, no)).fetchone()


def _where(conn, ws, no):
    o = _order(conn, ws, no)
    return o["status"], o["assigned_account_id"], o["assignment_kind"]


def _run(conn, ws, cid, fake):
    clock.freeze(clock.now() + timedelta(minutes=10))
    return feishu_intake.run_connection(conn, ws, cid, trigger="schedule", client=fake)


def _rename_column(fake, old, new):
    """模拟有人在飞书里把一列改了名：字段清单与每条记录里的键都换掉。"""
    table = fake.apps[DEMO_APP][DEMO_TABLE]
    for fdef in table["fields"]:
        if fdef["field_name"] == old:
            fdef["field_name"] = new
    for rec in table["records"].values():
        if old in rec:
            rec[new] = rec.pop(old)


def _delete_column(fake, name):
    """模拟有人在飞书里把一列删了：字段清单与每条记录里都没有它了。"""
    table = fake.apps[DEMO_APP][DEMO_TABLE]
    table["fields"] = [fdef for fdef in table["fields"] if fdef["field_name"] != name]
    for rec in table["records"].values():
        rec.pop(name, None)


def _accounts(conn, ws):
    a = f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id=ID_A)
    b = f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id=ID_B, priority=9)
    return a, b


def _events(conn, ws, no, kind):
    return conn.execute("SELECT detail FROM procure.events WHERE workspace_id = %s AND order_id = %s AND kind = %s "
                        "ORDER BY id", (ws, _order(conn, ws, no)["id"], kind)).fetchall()


def _connection(conn, ws, cid):
    return conn.execute("SELECT last_error, last_stats FROM procure.feishu_connections WHERE workspace_id = %s "
                        "AND id = %s", (ws, cid)).fetchone()


def _missing(col):
    """连接级问题与新记录跳过原因（契约 §6；{cols} = 「列名」用顿号连接）。"""
    return (f"来源表里找不到买家号列「{col}」，这一列的指定保持上次读到的内容，新记录暂不接收",
            f"来源表里找不到买家号列「{col}」，读不出这一行有没有指定买家号，暂不接收；恢复这一列或改字段对应后会自动接收")


# ── 飞书 ─────────────────────────────────────────────────────────────────────


def test_feishu_buyer_edit_moves_the_task_and_clearing_returns_it_to_auto(conn, workspace, fake):
    """验收 修订二 第 4 条：飞书里改了买家号 → 下次读取后任务改到新账号；清空 → 回到自动分配。
    只改买家号的一行也计为 update，所以读取后会分配。"""
    ws = workspace["id"]
    a, b = _accounts(conn, ws)
    _add(fake, "recDEMO0001", "PO-DEMO-G01", 买家号id=text(ID_A))
    _add(fake, "recDEMO0002", "PO-DEMO-G02")
    c = create_demo_connection(conn, ws, fake)
    first = _run(conn, ws, c["id"], fake)
    assert first["new"] == 2 and first["allocation"]["stats"]["designated"] == 1
    assert _where(conn, ws, "PO-DEMO-G01") == ("assigned", a["id"], "source")
    assert _where(conn, ws, "PO-DEMO-G02") == ("assigned", b["id"], "auto")          # 没写 → 自动（B 优先级高）

    _edit(fake, "recDEMO0001", 买家号id=text(ID_B), 买家号=text("环境-DEMO-B"))
    second = _run(conn, ws, c["id"], fake)
    assert (second["update"], second["unchanged"]) == (1, 1) and second["allocation"] is not None
    assert _where(conn, ws, "PO-DEMO-G01") == ("assigned", b["id"], "source")
    o = _order(conn, ws, "PO-DEMO-G01")
    assert (o["designated_customer_id"], o["designated_buyer_name"]) == (ID_B, "环境-DEMO-B")
    assert o["needs_attention"] is False
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM procure.events WHERE order_id = %s ORDER BY id",
                                             (o["id"],)).fetchall()]
    assert kinds.count("order.designation_changed") == 1 and "order.unassigned" in kinds

    _edit(fake, "recDEMO0001", 买家号id=None, 买家号=None)
    third = _run(conn, ws, c["id"], fake)
    assert third["update"] == 1 and third["allocation"] is not None
    status, _, kind = _where(conn, ws, "PO-DEMO-G01")
    assert (status, kind) == ("assigned", "auto")
    o = _order(conn, ws, "PO-DEMO-G01")
    assert (o["designated_customer_id"], o["designated_buyer_name"]) == (None, None)

    fourth = _run(conn, ws, c["id"], fake)                                           # 没有变化：不逐条接收、不分配
    assert fourth["unchanged"] == 2 and fourth["allocation"] is None


def test_feishu_missing_buyer_column_does_not_clear_designations_and_is_reported(conn, workspace, fake, monkeypatch):
    """验收 修订二 第 4 条：来源表里找不到买家号列 → 不改指定并提示（没读到不等于清空）；列恢复后，期间清空的格子照样
    清除指定。"""
    ws = workspace["id"]
    a, b = _accounts(conn, ws)
    _add(fake, "recDEMO0001", "PO-DEMO-J01", 买家号id=text(ID_A))
    _add(fake, "recDEMO0002", "PO-DEMO-J02", 买家号=text("环境-DEMO-A"))
    c = create_demo_connection(conn, ws, fake)
    _run(conn, ws, c["id"], fake)
    assert _where(conn, ws, "PO-DEMO-J01") == _where(conn, ws, "PO-DEMO-J02") == ("assigned", a["id"], "source")

    _rename_column(fake, "买家号id", "买家号ID（亚马逊）")
    runs = f.count(conn, "allocation_runs", "workspace_id = %s", (ws,))
    out = _run(conn, ws, c["id"], fake)
    notice = {"record_id": None, "upstream_order_no": "买家号列", "verdict": "skipped",
              "reason": _missing("买家号id")[0], "scope": "connection"}
    assert out["problems"] == [notice]
    assert (out["read"], out["unchanged"], out["update"], out["skipped"]) == (2, 2, 0, 0)     # 不计入跳过
    # 连接卡片要看得到（WEB-1）：连接级问题带在 last_stats.problems 里，计数照旧，last_error 不置位
    stored = _connection(conn, ws, c["id"])
    assert stored["last_error"] is None and stored["last_stats"]["problems"] == [notice]
    assert {k: v for k, v in stored["last_stats"].items() if k != "problems"} == {
        k: out[k] for k in feishu_intake.STAT_KEYS}
    assert out["allocation"] is None and f.count(conn, "allocation_runs", "workspace_id = %s", (ws,)) == runs
    for no in ("PO-DEMO-J01", "PO-DEMO-J02"):
        assert _where(conn, ws, no) == ("assigned", a["id"], "source")
    assert _order(conn, ws, "PO-DEMO-J01")["designated_customer_id"] == ID_A
    stored = conn.execute("SELECT stats FROM procure.feishu_sync_runs WHERE id = %s", (out["run_id"],)).fetchone()
    assert stored["stats"]["problems"][0]["reason"].startswith("来源表里找不到买家号列")
    # 列还没恢复时别的字段变了：照常更新，指定不动；预演的结论与正式读取一致
    _edit(fake, "recDEMO0001", SKU=text("SKU-DEMO-2"))
    preview = fc.preview(conn, ws, app_token=DEMO_APP, table_id=DEMO_TABLE, field_map=c["field_map"],
                         admission=c["admission"], client=fake)
    assert {r["upstream_order_no"]: r["verdict"] for r in preview["records"]} == {
        "PO-DEMO-J01": "update", "PO-DEMO-J02": "unchanged"}
    out = _run(conn, ws, c["id"], fake)
    assert (out["update"], out["unchanged"]) == (1, 1) and out["problems"][0]["scope"] == "connection"
    assert _order(conn, ws, "PO-DEMO-J01")["designated_customer_id"] == ID_A
    assert _where(conn, ws, "PO-DEMO-J01") == ("assigned", a["id"], "source")

    # 列改回来：J01 的格子在这期间被清空了 → 清除指定、回到自动；J02 没变
    _rename_column(fake, "买家号ID（亚马逊）", "买家号id")
    _edit(fake, "recDEMO0001", 买家号id=None)
    out = _run(conn, ws, c["id"], fake)
    assert all(p.get("scope") != "connection" for p in out["problems"])
    assert "problems" not in _connection(conn, ws, c["id"])["last_stats"]          # 列找回来了：卡片上的提醒随之消失
    assert out["update"] == 1 and _order(conn, ws, "PO-DEMO-J01")["designated_customer_id"] is None
    assert _where(conn, ws, "PO-DEMO-J01")[2] == "auto"
    assert _where(conn, ws, "PO-DEMO-J02") == ("assigned", a["id"], "source")


def test_missing_buyer_column_never_turns_a_new_row_into_an_auto_task(conn, workspace, fake):
    """ING-1 / DOC-1（铁律 12）：来源表里找不到配着的买家号列时，读不出一条**新记录**有没有指定买家号——不能建成
    没有指定的任务让它落进自动分配（别的账号的插件就能领走）。新记录暂不接收并写明原因，列恢复后自动接收、按指定分配。
    读没读到按列算：还在的「买家号」列里的修改照常应用。带可信 AMZ 订单号的行照常按历史采购接收。"""
    ws = workspace["id"]
    a, b = _accounts(conn, ws)                                                     # B 优先级高：自动分配会落到 B
    _add(fake, "recDEMO0001", "PO-DEMO-K01", 买家号=text("环境-DEMO-A"))
    c = create_demo_connection(conn, ws, fake, admission={"purchaser_values": [DEMO_PURCHASER],
                                                          "import_historical": True})
    _run(conn, ws, c["id"], fake)
    assert _where(conn, ws, "PO-DEMO-K01") == ("assigned", a["id"], "source")

    _rename_column(fake, "买家号id", "买家号ID（亚马逊）")
    _add(fake, "recDEMO0002", "PO-DEMO-K02", 买家号=text("环境-DEMO-A"))             # 指定写在还读得到的那一列
    _add(fake, "recDEMO0003", "PO-DEMO-K03", **{"买家号ID（亚马逊）": text(ID_A)})    # 指定写在改了名的那一列
    _add(fake, "recDEMO0004", "PO-DEMO-K04")                                        # 两列都没写（读不出来有没有写）
    _add(fake, "recDEMO0005", "PO-DEMO-K05", AMZ订单号=text("111-0000000-0000905"), 买家号=text("环境-DEMO-B"),
         采购状态="已拍单")                                                          # 历史采购：不涉及指定
    _edit(fake, "recDEMO0001", 买家号=text("环境-DEMO-B"))                           # 已有任务：读得到的那一列改了
    connection_reason, row_reason = _missing("买家号id")

    preview = fc.preview(conn, ws, app_token=DEMO_APP, table_id=DEMO_TABLE, field_map=c["field_map"],
                         admission=c["admission"], client=fake)                     # 样本预览给出同样的结论
    assert {r["upstream_order_no"]: (r["verdict"], r["reason"]) for r in preview["records"]
            if r["upstream_order_no"] in ("PO-DEMO-K02", "PO-DEMO-K03", "PO-DEMO-K04")} == {
        no: ("skipped", row_reason) for no in ("PO-DEMO-K02", "PO-DEMO-K03", "PO-DEMO-K04")}

    out = _run(conn, ws, c["id"], fake)
    assert out["problems"][0]["scope"] == "connection" and out["problems"][0]["reason"] == connection_reason
    assert (out["read"], out["new"], out["update"], out["historical"], out["skipped"]) == (5, 0, 1, 1, 3)
    assert {p["upstream_order_no"]: (p["verdict"], p["reason"]) for p in out["problems"][1:]} == {
        no: ("skipped", row_reason) for no in ("PO-DEMO-K02", "PO-DEMO-K03", "PO-DEMO-K04")}
    for no in ("PO-DEMO-K02", "PO-DEMO-K03", "PO-DEMO-K04"):
        assert _order(conn, ws, no) is None, no                                     # 没有建任务
    assert _where(conn, ws, "PO-DEMO-K01") == ("assigned", b["id"], "source")       # 读得到的那一列照常应用
    assert _order(conn, ws, "PO-DEMO-K05")["status"] == "purchased"
    assert f.count(conn, "purchase_orders", "workspace_id = %s AND assignment_kind = 'auto'", (ws,)) == 0
    assert _connection(conn, ws, c["id"])["last_stats"]["problems"] == [out["problems"][0]]

    again = _run(conn, ws, c["id"], fake)                                           # 列还没恢复：照样不接收
    assert (again["skipped"], again["new"], again["allocation"]) == (3, 0, None)

    _rename_column(fake, "买家号ID（亚马逊）", "买家号id")                            # 列改回来：下一次读取自动接收
    out = _run(conn, ws, c["id"], fake)
    assert out["new"] == 3 and all(p.get("scope") != "connection" for p in out["problems"])
    assert _where(conn, ws, "PO-DEMO-K02") == _where(conn, ws, "PO-DEMO-K03") == ("assigned", a["id"], "source")
    assert _where(conn, ws, "PO-DEMO-K04")[2] == "auto"                             # 确实没写的才自动分配


def test_deleting_only_a_fallback_buyer_column_also_holds_new_rows(conn, workspace, monkeypatch):
    """ING-1 的变体：字段对应里「买家号」配了回退列（买家号、买家号1），只删掉回退列「买家号1」。少了一列候选就说不准
    「买家号」该取哪个值：这个字段算没读到，新记录暂不接收（之前会被建成没有指定的任务自动分给别的账号）；
    已有任务的指定保持不变，「买家号 ID」那一列照常读取。"""
    ws = workspace["id"]
    a, b = _accounts(conn, ws)
    fake = FakeFeishuClient()
    fake.add_table(DEMO_APP, DEMO_TABLE, "采购订单DEMO", [*ORDER_TABLE_FIELDS, ("买家号1", TEXT)])
    monkeypatch.setattr(fc, "make_client", lambda: fake)
    _add(fake, "recDEMO0001", "PO-DEMO-K11", 买家号=text("环境-DEMO-A"))
    _add(fake, "recDEMO0002", "PO-DEMO-K12", 买家号1=text("环境-DEMO-A"))             # 写在回退列里
    c = create_demo_connection(conn, ws, fake)
    assert c["field_map"]["external_buyer_account"] == ["买家号", "买家号1"]
    _run(conn, ws, c["id"], fake)
    assert _where(conn, ws, "PO-DEMO-K11") == _where(conn, ws, "PO-DEMO-K12") == ("assigned", a["id"], "source")

    _delete_column(fake, "买家号1")
    _add(fake, "recDEMO0003", "PO-DEMO-K13", 买家号=text("环境-DEMO-A"), 买家号id=text(ID_A))   # 两个真正的列都在
    _edit(fake, "recDEMO0001", 买家号id=text(ID_B))                                  # 还读得到的「买家号 ID」列改了
    connection_reason, row_reason = _missing("买家号1")
    out = _run(conn, ws, c["id"], fake)
    assert out["problems"] == [
        {"record_id": None, "upstream_order_no": "买家号列", "verdict": "skipped", "reason": connection_reason,
         "scope": "connection"},
        {"record_id": "recDEMO0003", "upstream_order_no": "PO-DEMO-K13", "verdict": "skipped", "reason": row_reason}]
    assert _order(conn, ws, "PO-DEMO-K13") is None
    assert f.count(conn, "purchase_orders", "workspace_id = %s AND assignment_kind = 'auto'", (ws,)) == 0
    k12 = _order(conn, ws, "PO-DEMO-K12")                                           # 回退列里写的指定：没读到不等于清空
    assert (k12["designated_buyer_name"], k12["assigned_account_id"]) == ("环境-DEMO-A", a["id"])
    k11 = _order(conn, ws, "PO-DEMO-K11")                                           # ID 列照常应用，名称那一半保持
    assert (k11["designated_customer_id"], k11["designated_buyer_name"]) == (ID_B, "环境-DEMO-A")
    assert k11["status"] == "queued" and "不是同一个账号" in k11["status_reason"]     # 两列指向不同账号：不分配

    # 所有者改了字段对应（不再读「买家号1」）：提示消失，新记录下一次读取接收并按指定分配
    fixed = {**c["field_map"], "external_buyer_account": "买家号"}
    fc.update(conn, ws, c["id"], {"field_map": fixed}, None, client=fake)
    out = _run(conn, ws, c["id"], fake)
    assert out["new"] == 1 and all(p.get("scope") != "connection" for p in out["problems"])
    assert _where(conn, ws, "PO-DEMO-K13") == ("assigned", a["id"], "source")


def test_feishu_unmapping_both_buyer_fields_does_not_clear_designations(conn, workspace, fake):
    """验收 修订二 第 4 条（没读到不等于清空）：所有者在字段对应里把「买家号」「买家号 ID」都取消 → 之后的读取对来源指定
    不表态，之前写下的指定原样保留，也不报「找不到买家号列」；重新对应之后，期间清空的格子照样清除指定。"""
    ws = workspace["id"]
    a, b = _accounts(conn, ws)
    _add(fake, "recDEMO0001", "PO-DEMO-U01", 买家号id=text(ID_A))
    _add(fake, "recDEMO0002", "PO-DEMO-U02", 买家号=text("环境-DEMO-A"))
    c = create_demo_connection(conn, ws, fake)
    _run(conn, ws, c["id"], fake)
    assert _where(conn, ws, "PO-DEMO-U01") == _where(conn, ws, "PO-DEMO-U02") == ("assigned", a["id"], "source")

    full_map = dict(c["field_map"])
    bare = {k: v for k, v in full_map.items() if k not in ("external_buyer_account", "external_buyer_customer_id")}
    assert len(bare) == len(full_map) - 2
    fc.update(conn, ws, c["id"], {"field_map": bare}, None, client=fake)
    _edit(fake, "recDEMO0001", 买家号id=None, SKU=text("SKU-DEMO-2"))            # 清空了格子，但这一列已经不读
    out = _run(conn, ws, c["id"], fake)
    assert all(p.get("scope") != "connection" for p in out["problems"])            # 所有者自己取消的对应，不报问题
    assert out["update"] == 1
    for no in ("PO-DEMO-U01", "PO-DEMO-U02"):
        assert _where(conn, ws, no) == ("assigned", a["id"], "source")
    assert _order(conn, ws, "PO-DEMO-U01")["designated_customer_id"] == ID_A
    assert f.count(conn, "events", "workspace_id = %s AND kind = 'order.designation_changed'", (ws,)) == 0
    again = _run(conn, ws, c["id"], fake)                                           # 没有变化：仍走快速路径
    assert again["unchanged"] == 2 and again["allocation"] is None

    fc.update(conn, ws, c["id"], {"field_map": full_map}, None, client=fake)        # 重新对应：清空的格子现在读得到了
    out = _run(conn, ws, c["id"], fake)
    assert out["update"] == 1 and _order(conn, ws, "PO-DEMO-U01")["designated_customer_id"] is None
    assert _where(conn, ws, "PO-DEMO-U01")[2] == "auto"
    assert _where(conn, ws, "PO-DEMO-U02") == ("assigned", a["id"], "source")


def test_feishu_unmapping_one_buyer_field_keeps_designations_made_through_it(conn, workspace, fake):
    """ING-2：只取消「买家号 ID」一个字段的对应（「买家号id」列被删之后，这是唯一能让提示消失的做法）。之前经这一列
    写下的指定不能被整批清掉：那个字段算没读到，沿用上次读到的值；还对应着的「买家号」列照常读取。取消对应是所有者
    自己的设置：不报问题，也不挡新记录。"""
    ws = workspace["id"]
    a, b = _accounts(conn, ws)
    _add(fake, "recDEMO0001", "PO-DEMO-W01", 买家号id=text(ID_A))                     # 只经 ID 列指定
    _add(fake, "recDEMO0002", "PO-DEMO-W02", 买家号id=text(ID_A), 买家号=text("环境-DEMO-A"))
    c = create_demo_connection(conn, ws, fake)
    _run(conn, ws, c["id"], fake)
    assert _where(conn, ws, "PO-DEMO-W01") == _where(conn, ws, "PO-DEMO-W02") == ("assigned", a["id"], "source")

    _delete_column(fake, "买家号id")                                                  # 先是列被删：提示，任务不动
    out = _run(conn, ws, c["id"], fake)
    assert out["problems"][0]["reason"] == _missing("买家号id")[0] and out["unchanged"] == 2
    name_only = {k: v for k, v in c["field_map"].items() if k != "external_buyer_customer_id"}
    wb_map = {k: v for k, v in c["writeback_map"].items() if v != "买家号id"}
    fc.update(conn, ws, c["id"], {"field_map": name_only, "writeback_map": wb_map}, None, client=fake)
    _add(fake, "recDEMO0003", "PO-DEMO-W03", 买家号=text("环境-DEMO-A"))               # 新记录：照常接收
    out = _run(conn, ws, c["id"], fake)
    assert out["problems"] == [] and (out["new"], out["update"], out["unchanged"]) == (1, 0, 2)
    for no in ("PO-DEMO-W01", "PO-DEMO-W02", "PO-DEMO-W03"):
        assert _where(conn, ws, no) == ("assigned", a["id"], "source"), no
    w1, w2 = _order(conn, ws, "PO-DEMO-W01"), _order(conn, ws, "PO-DEMO-W02")
    assert (w1["designated_customer_id"], w1["designated_buyer_name"]) == (ID_A, None)
    assert (w2["designated_customer_id"], w2["designated_buyer_name"]) == (ID_A, "环境-DEMO-A")
    assert f.count(conn, "events", "workspace_id = %s AND kind = 'order.designation_changed'", (ws,)) == 0
    assert f.count(conn, "purchase_orders", "workspace_id = %s AND assignment_kind = 'auto'", (ws,)) == 0

    _edit(fake, "recDEMO0002", 买家号=text("环境-DEMO-B"))                            # 还对应着的那一列改了：照常应用
    out = _run(conn, ws, c["id"], fake)
    assert out["update"] == 1
    w2 = _order(conn, ws, "PO-DEMO-W02")
    assert (w2["designated_customer_id"], w2["designated_buyer_name"]) == (ID_A, "环境-DEMO-B")
    assert _where(conn, ws, "PO-DEMO-W01") == ("assigned", a["id"], "source")


def test_withdrawing_a_never_adopted_feishu_value_keeps_the_primary_designation(conn, workspace, fake):
    """ING-3 / ING-4（整条路）：文件为主的任务指定了 A；飞书里同一笔订单写了 B → 不采用，任务时间线上留一条
    order.designation_not_adopted（定时读取没有人看预览）。之后飞书把这个没被采用的 B 清空 → 不能把文件写的 A 清掉、
    让任务落进自动分配。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _accounts(conn, ws)
    bid, _ = _upload(conn, ws, uid, [_file_row("PO-DEMO-N01", 买家号ID=ID_A)])
    imports.commit(conn, ws, bid, uid)
    assert _where(conn, ws, "PO-DEMO-N01") == ("assigned", a["id"], "source")

    _add(fake, "recDEMO0001", "PO-DEMO-N01", 买家号=text("环境-DEMO-B"))
    c = create_demo_connection(conn, ws, fake)
    out = _run(conn, ws, c["id"], fake)
    assert (out["read"], out["unchanged"]) == (1, 1) and out["allocation"] is None
    assert _where(conn, ws, "PO-DEMO-N01") == ("assigned", a["id"], "source")
    not_adopted = _events(conn, ws, "PO-DEMO-N01", "order.designation_not_adopted")
    assert len(not_adopted) == 1
    assert not_adopted[0]["detail"] == {
        "source": {"kind": "feishu", "connection_id": c["id"], "record_id": "recDEMO0001"},
        "stated": {"customer_id": None, "name": "环境-DEMO-B"}, "previous": None,
        "current": {"customer_id": ID_A, "name": None}}
    assert _order(conn, ws, "PO-DEMO-N01")["needs_attention"] is False
    out = _run(conn, ws, c["id"], fake)                                             # 再读一次：不重复记
    assert out["unchanged"] == 1 and len(_events(conn, ws, "PO-DEMO-N01", "order.designation_not_adopted")) == 1

    _edit(fake, "recDEMO0001", 买家号=None)                                          # 撤回那个没被采用的 B
    preview = fc.preview(conn, ws, app_token=DEMO_APP, table_id=DEMO_TABLE, field_map=c["field_map"],
                         admission=c["admission"], client=fake)
    assert preview["records"][0]["issues"] == [
        "飞书来源清空了买家号「环境-DEMO-B」，任务现有的指定「A1DEMO0000」不是这条来源写的，保持不变"]
    out = _run(conn, ws, c["id"], fake)
    assert (out["update"], out["unchanged"]) == (0, 1) and out["allocation"] is None
    o = _order(conn, ws, "PO-DEMO-N01")
    assert (o["designated_customer_id"], o["designated_buyer_name"]) == (ID_A, None)
    assert _where(conn, ws, "PO-DEMO-N01") == ("assigned", a["id"], "source")
    events = _events(conn, ws, "PO-DEMO-N01", "order.designation_not_adopted")
    assert len(events) == 2 and events[1]["detail"]["previous"] == {"customer_id": None, "name": "环境-DEMO-B"}
    assert events[1]["detail"]["stated"] == {"customer_id": None, "name": None}
    assert _events(conn, ws, "PO-DEMO-N01", "order.designation_changed") == []
    out = _run(conn, ws, c["id"], fake)                                             # 之后没有变化：走快速路径
    assert out["unchanged"] == 1 and len(_events(conn, ws, "PO-DEMO-N01", "order.designation_not_adopted")) == 2


def test_feishu_link_first_read_without_the_buyer_column_does_not_override_later(conn, workspace, fake):
    """ING-3 变体（整条路）：文件为主的任务指定了 A；飞书记录第一次关联时「买家号」列读不到。列恢复后第一次读到的 B
    按首次关联对待（不采用、留事件），不能凭「与上次读到的空不同」盖掉主来源的指定。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _accounts(conn, ws)
    bid, _ = _upload(conn, ws, uid, [_file_row("PO-DEMO-N02", 买家号ID=ID_A)])
    imports.commit(conn, ws, bid, uid)
    _add(fake, "recDEMO0001", "PO-DEMO-N02", 买家号=text("环境-DEMO-B"))
    c = create_demo_connection(conn, ws, fake)
    _rename_column(fake, "买家号", "买家号（旧）")
    out = _run(conn, ws, c["id"], fake)                                             # 关联建立时读不到这一列
    assert out["unchanged"] == 1 and out["problems"][0]["scope"] == "connection"
    link = conn.execute("SELECT normalized FROM procure.order_sources WHERE workspace_id = %s AND kind = 'feishu'",
                        (ws,)).fetchone()["normalized"]
    assert link["external_buyer_account"] is None and link["buyer_unread"] == ["external_buyer_account"]
    assert _events(conn, ws, "PO-DEMO-N02", "order.designation_not_adopted") == []

    _rename_column(fake, "买家号（旧）", "买家号")
    out = _run(conn, ws, c["id"], fake)                                             # 第一次读到 B
    assert (out["update"], out["unchanged"]) == (0, 1) and out["allocation"] is None
    assert _where(conn, ws, "PO-DEMO-N02") == ("assigned", a["id"], "source")
    assert _order(conn, ws, "PO-DEMO-N02")["designated_customer_id"] == ID_A
    events = _events(conn, ws, "PO-DEMO-N02", "order.designation_not_adopted")
    assert len(events) == 1 and events[0]["detail"]["stated"] == {"customer_id": None, "name": "环境-DEMO-B"}
    link = conn.execute("SELECT normalized FROM procure.order_sources WHERE workspace_id = %s AND kind = 'feishu'",
                        (ws,)).fetchone()["normalized"]
    assert link["external_buyer_account"] == "环境-DEMO-B" and "buyer_unread" not in link
    out = _run(conn, ws, c["id"], fake)
    assert out["unchanged"] == 1 and len(_events(conn, ws, "PO-DEMO-N02", "order.designation_not_adopted")) == 1


def test_feishu_placeholders_are_not_designations_and_error_values_are_not_blanks(conn, workspace, fake):
    """验收 修订二 第 4 条：占位写法（无、—）按没写处理 → 自动分配；表格错误值不当成留空 → 留在待分配，不自动分配。"""
    ws = workspace["id"]
    a, b = _accounts(conn, ws)
    _add(fake, "recDEMO0001", "PO-DEMO-L01", 买家号=text("无"), 买家号id=text("—"))
    _add(fake, "recDEMO0002", "PO-DEMO-L02", 买家号=text("#N/A"))
    _add(fake, "recDEMO0003", "PO-DEMO-L03", 买家号id=text("#REF!"), 买家号=text("环境-DEMO-A"))
    c = create_demo_connection(conn, ws, fake)
    out = _run(conn, ws, c["id"], fake)
    assert out["new"] == 3
    assert _where(conn, ws, "PO-DEMO-L01") == ("assigned", b["id"], "auto")
    o1 = _order(conn, ws, "PO-DEMO-L01")
    assert (o1["designated_customer_id"], o1["designated_buyer_name"]) == (None, None)
    o2, o3 = _order(conn, ws, "PO-DEMO-L02"), _order(conn, ws, "PO-DEMO-L03")
    assert (o2["status"], o2["assigned_account_id"], o2["designated_buyer_name"]) == ("queued", None, "#N/A")
    assert o2["status_reason"] == "来源的买家号是表格错误值 #N/A，读不出指定的买家号；在来源里改正后会重新分配"
    assert (o3["status"], o3["assigned_account_id"], o3["designated_customer_id"]) == ("queued", None, "#REF!")
    assert o3["status_reason"] == "来源的买家号 ID 是表格错误值 #REF!，读不出指定的买家号；在来源里改正后会重新分配"
    assert out["allocation"]["stats"] == {"considered": 3, "assigned": 1, "coverage": 1, "fill": 0, "designated": 0,
                                          "unassigned": 2}
    # WEB-8：原因不再建议「新建同名账号」；真有一个叫 #N/A 的账号也接不走读不出指定的任务
    assert "同名账号" not in o2["status_reason"]
    f.make_account(conn, ws, name="#N/A", amazon_customer_id="A1DEMO0008")
    again = allocation.allocate(conn, ws, trigger="manual")
    assert again["stats"]["unassigned"] == 2
    assert _where(conn, ws, "PO-DEMO-L02") == ("queued", None, None)
    assert _order(conn, ws, "PO-DEMO-L02")["status_reason"] == o2["status_reason"]


# ── 规范化 ───────────────────────────────────────────────────────────────────


def test_normalize_buyer_columns():
    def norm(**kw):
        return normalize.normalize({"upstream_order_no": "PO-DEMO-N01", **kw})

    r = norm(external_buyer_account="—", external_buyer_customer_id=" n/a ")
    assert (r.external_buyer_account, r.external_buyer_customer_id) == (None, None)
    assert r.issues == ["买家号「—」不是买家号，按没有指定处理", "买家号 ID「n/a」不是买家号，按没有指定处理"]

    r = norm(external_buyer_account="#N/A", external_buyer_customer_id="#VALUE!")
    assert (r.external_buyer_account, r.external_buyer_customer_id) == ("#N/A", "#VALUE!")      # 保留错误值原文
    assert r.issues == ["买家号是表格错误值 #N/A，读不出指定的买家号", "买家号 ID 是表格错误值 #VALUE!，读不出指定的买家号"]

    r = norm(external_buyer_customer_id="demo-buyer")
    assert r.external_buyer_customer_id == "demo-buyer"                                         # 值原样保留
    assert r.issues == ["买家号 ID「demo-buyer」格式不对，这笔任务会停在待分配"]
    assert norm(external_buyer_customer_id=" a1demo 0000 ").issues == []                         # 清洗后合格
    assert norm(external_buyer_customer_id=["A1DEMO0000", "A1DEMO0001"]).issues == [
        "买家号 ID「A1DEMO0000, A1DEMO0001」格式不对，这笔任务会停在待分配"]                       # 多个值：解析不了的指定

    # 行带可信 AMZ 订单号：两列只是历史采购记录——错误值照旧按没读到，ID 形状不提示；格式不对的单号不算可信
    hist = norm(external_amz_order_no="111-0000000-0000001", external_buyer_account="#N/A",
                external_buyer_customer_id="demo-buyer")
    assert (hist.external_buyer_account, hist.external_buyer_customer_id) == (None, "demo-buyer")
    assert hist.issues == ["买家号是表格错误值 #N/A，按没读到处理"]
    bad = norm(external_amz_order_no="重拍", external_buyer_customer_id="demo-buyer")
    assert "买家号 ID「demo-buyer」格式不对，这笔任务会停在待分配" in bad.issues
    assert normalize.is_placeholder(" 无 ") and not normalize.is_placeholder("环境-DEMO-A")
    assert not normalize.is_placeholder(None) and normalize.is_error_value("#n/a") and not normalize.is_error_value("—")
    # 没给 UpstreamRecord 加字段，系统字段清单的键也没动
    assert set(normalize.SYSTEM_FIELDS[0]) == {"key", "label", "required", "kind"}


def test_template_notes_explain_the_two_columns():
    notes = {h: d for h, _, d in file_mapping.STANDARD_COLUMNS}
    assert notes["买家号"] == ("指定由哪个买家号采购，须与控制台的账号名称一致；留空由系统自动分配。"
                              "已有 AMZ 订单号的行表示历史采购使用的买家号。"
                              "文件来源要改指定请重新上传并填上新的买家号；空着重新上传不会清除指定")
    assert notes["买家号ID"] == ("指定采购的买家号 ID（亚马逊 customerId，A 开头）；与「买家号」同时填写时以它为准。"
                                "留空由系统自动分配")


# ── 文件：预览、提交、导出再导入 ────────────────────────────────────────────────

HEADERS = ["平台订单号", "收货人姓名", "收货人地址", "收货人城市", "收货人州/省", "邮编", "收货人电话", "ASIN", "数量",
           "人工审核", "限价", "采购单价", "建议采购日期", "AMZ订单号", "买家号", "买家号ID"]


def _file_row(no, **kw):
    r = {"平台订单号": no, "收货人姓名": "Demo Buyer", "收货人地址": "100 Demo Street", "收货人城市": "Demo City",
         "收货人州/省": "FL", "邮编": "32960", "收货人电话": "555-0100", "ASIN": "B0DEMO0001", "数量": 1,
         "人工审核": "通过", "限价": 40, "采购单价": 25, "建议采购日期": "2026-11-18"}
    r.update(kw)
    return r


def _upload(conn, ws, uid, rows, headers=HEADERS, filename="demo-orders.xlsx"):
    up = imports.upload(conn, ws, filename=filename, data=sheet_io.write_xlsx([("订单", headers, rows)]),
                        actor_user_id=uid)
    pv = imports.preview(conn, ws, up["batch_id"])
    return up["batch_id"], pv


def test_file_import_designates_and_preview_warns_about_a_bad_id(conn, workspace):
    """验收 修订二 第 1 条（文件来源）：文件里填了买家号 / 买家号 ID 的行按指定分配，没填的自动分配；预览里能看到
    买家号 ID 格式不对的提示。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _accounts(conn, ws)
    bid, _ = _upload(conn, ws, uid, [
        _file_row("PO-DEMO-V01", 买家号ID="a1demo0000"),
        _file_row("PO-DEMO-V02", 买家号="环境-DEMO-A"),
        _file_row("PO-DEMO-V03"),
        _file_row("PO-DEMO-V04", 买家号ID="demo-buyer"),
    ])
    rows = {r["row_no"]: r for r in imports.rows(conn, ws, bid, page_size=50)["rows"]}
    assert [rows[n]["verdict"] for n in (2, 3, 4, 5)] == ["new"] * 4
    assert rows[5]["issues"] == ["买家号 ID「demo-buyer」格式不对，这笔任务会停在待分配"]
    assert rows[2]["issues"] == [] and rows[4]["issues"] == []
    out = imports.commit(conn, ws, bid, uid)
    assert out["allocation"]["stats"] == {"considered": 4, "assigned": 3, "coverage": 1, "fill": 0, "designated": 2,
                                          "unassigned": 1}
    assert _where(conn, ws, "PO-DEMO-V01") == _where(conn, ws, "PO-DEMO-V02") == ("assigned", a["id"], "source")
    assert _where(conn, ws, "PO-DEMO-V03") == ("assigned", b["id"], "auto")
    bad = _order(conn, ws, "PO-DEMO-V04")
    assert bad["status"] == "queued" and bad["status_reason"].startswith("来源指定的买家号 ID「DEMO-BUYER」格式不对")
    assert out["allocation"]["unassigned"] == [{"order_id": bad["id"], "upstream_order_no": "PO-DEMO-V04",
                                                "reason": bad["status_reason"], "designated": True}]


def test_second_upload_previews_the_designation_change_and_blank_keeps_it(conn, workspace):
    """文件来源：重新上传并填上新的买家号 → 预览就写明「由…改为…」，提交后任务换到新账号；空着重新上传不会清除指定；
    飞书来源写了别的买家号（任务以文件为主）→ 不采用，问题里说明。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _accounts(conn, ws)
    bid, _ = _upload(conn, ws, uid, [_file_row("PO-DEMO-R01", 买家号="环境-DEMO-A")])
    imports.commit(conn, ws, bid, uid)
    assert _where(conn, ws, "PO-DEMO-R01") == ("assigned", a["id"], "source")

    bid2, _ = _upload(conn, ws, uid, [_file_row("PO-DEMO-R01", 买家号="环境-DEMO-B")], filename="demo-orders-2.xlsx")
    row = imports.rows(conn, ws, bid2, page_size=50)["rows"][0]
    assert row["verdict"] == "update" and row["reason"] == "来源指定的买家号由「环境-DEMO-A」改为「环境-DEMO-B」"
    assert row["issues"] == [] and row["status"] == "queued"            # 预演：原分配会解除，等提交后的分配
    assert _where(conn, ws, "PO-DEMO-R01") == ("assigned", a["id"], "source")          # 预览不写库
    out = imports.commit(conn, ws, bid2, uid)
    assert out["stats"]["update"] == 1 and _where(conn, ws, "PO-DEMO-R01") == ("assigned", b["id"], "source")

    bid3, _ = _upload(conn, ws, uid, [_file_row("PO-DEMO-R01")], filename="demo-orders-3.xlsx")
    row = imports.rows(conn, ws, bid3, page_size=50)["rows"][0]
    assert row["verdict"] == "unchanged" and row["issues"] == []
    imports.commit(conn, ws, bid3, uid)
    assert _where(conn, ws, "PO-DEMO-R01") == ("assigned", b["id"], "source")
    assert _order(conn, ws, "PO-DEMO-R01")["designated_buyer_name"] == "环境-DEMO-B"

    rec = f.make_record(upstream_order_no="PO-DEMO-R01", unit_price=25, price_cap=40, sku=None, store_name=None,
                        sales_po_no=None, order_line_id=None, sales_total=None, ship_method=None, order_time=None,
                        ship_days=None, suggested_purchase_date=clock.bj_today(), external_buyer_customer_id=ID_A)
    out = orders.ingest(conn, ws, rec, f.feishu_source(conn, ws, "recDEMO-R01"))
    assert out.issues == [f"飞书来源的买家号「A1DEMO0000」与任务现有的指定「环境-DEMO-B」不同，{ADVICE}"]
    assert _order(conn, ws, "PO-DEMO-R01")["designated_customer_id"] is None


def test_file_without_one_buyer_column_leaves_that_half_of_the_designation_alone(conn, workspace):
    """文件来源读没读到也按列算：批次的列对应里没有「买家号ID」这一列 → 那个字段没读到，不等于清空。任务指定是
    「ID + 名称」，重新上传一份只有「买家号」列、名称相同的文件 → 不变（之前会被改写成只有名称）。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _accounts(conn, ws)
    bid, _ = _upload(conn, ws, uid, [_file_row("PO-DEMO-R02", 买家号ID=ID_A, 买家号="环境-DEMO-A"),
                                     _file_row("PO-DEMO-R03", 买家号ID=ID_A, 买家号="环境-DEMO-A")])
    imports.commit(conn, ws, bid, uid)
    name_only = [h for h in HEADERS if h != "买家号ID"]
    bid2, pv = _upload(conn, ws, uid, [_file_row("PO-DEMO-R02", 买家号="环境-DEMO-A"),
                                       _file_row("PO-DEMO-R03", 买家号="环境-DEMO-B"),
                                       _file_row("PO-DEMO-R04", 买家号="环境-DEMO-B")],
                       headers=name_only, filename="demo-orders-name-only.xlsx")
    assert "external_buyer_customer_id" not in pv["batch"]["mapping"].values()
    rows = {r["upstream_order_no"]: r for r in imports.rows(conn, ws, bid2, page_size=50)["rows"]}
    assert (rows["PO-DEMO-R02"]["verdict"], rows["PO-DEMO-R02"]["issues"]) == ("unchanged", [])
    assert rows["PO-DEMO-R03"]["verdict"] == "update" and rows["PO-DEMO-R04"]["verdict"] == "new"
    imports.commit(conn, ws, bid2, uid)
    r2, r3, r4 = (_order(conn, ws, no) for no in ("PO-DEMO-R02", "PO-DEMO-R03", "PO-DEMO-R04"))
    assert (r2["designated_customer_id"], r2["designated_buyer_name"]) == (ID_A, "环境-DEMO-A")       # 一列不丢
    assert (r3["designated_customer_id"], r3["designated_buyer_name"]) == (None, "环境-DEMO-B")       # 改了：整组替换
    assert (r4["designated_customer_id"], r4["designated_buyer_name"]) == (None, "环境-DEMO-B")       # 新任务：读到的那一列
    assert _where(conn, ws, "PO-DEMO-R02") == ("assigned", a["id"], "source")
    assert _where(conn, ws, "PO-DEMO-R03") == _where(conn, ws, "PO-DEMO-R04") == ("assigned", b["id"], "source")
    assert _events(conn, ws, "PO-DEMO-R02", "order.designation_changed") == []


def _sheet(data):
    values = list(openpyxl.load_workbook(io.BytesIO(data))["采购结果"].values)
    col = {h: i for i, h in enumerate(values[0])}
    return values, col


def test_export_keeps_auto_and_designated_apart_and_reimport_does_not_designate(conn, workspace):
    """§E.10：导出的「买家号」= 采购账号（有结果）或来源指定的买家号（没有结果），自动分配的任务留空；已分配的账号在
    「分配账号」一列，这个表头不会被自动识别成买家号列。导出文件原样再上传，自动分配的任务不会变成来源指定。"""
    ws, uid = workspace["id"], workspace["user_id"]
    a, b = _accounts(conn, ws)
    bid, _ = _upload(conn, ws, uid, [
        _file_row("PO-DEMO-X01"),                                   # 自动
        _file_row("PO-DEMO-X02", 买家号="环境-DEMO-A"),               # 按名称指定
        _file_row("PO-DEMO-X03", 买家号ID=ID_A),                     # 只指定了 ID
        _file_row("PO-DEMO-X04"),                                   # 自动，随后模拟采购成功
    ])
    imports.commit(conn, ws, bid, uid)
    done = _order(conn, ws, "PO-DEMO-X04")
    att = f.make_attempt(conn, ws, done["id"], b["id"], status="succeeded", simulated=True, submitted=True,
                         amazon_order_no="SIM-20261118-4")
    f.set_status(conn, done["id"], "purchased", purchased_attempt_id=att["id"])
    data = exports.orders_xlsx(conn, ws, batch_id=bid)
    values, col = _sheet(data)
    assert list(values[0]) == exports.EXPORT_HEADERS
    assert list(values[0][col["买家号"]:col["买家号"] + 3]) == ["买家号", "买家号ID", "分配账号"]
    by_no = {v[0]: v for v in values[1:]}
    assert [(by_no[n][col["买家号"]], by_no[n][col["买家号ID"]], by_no[n][col["分配账号"]]) for n in
            ("PO-DEMO-X01", "PO-DEMO-X02", "PO-DEMO-X03", "PO-DEMO-X04")] == [
        (None, None, "环境-DEMO-B"), ("环境-DEMO-A", None, "环境-DEMO-A"), (None, ID_A, "环境-DEMO-A"),
        ("环境-DEMO-B", ID_B, "环境-DEMO-B")]                       # 有采购结果：采购账号的名称与它登记的买家号 ID

    mapping = file_mapping.suggest_mapping(exports.EXPORT_HEADERS)
    assert mapping["买家号"] == "external_buyer_account" and mapping["分配账号"] is None
    assert mapping["买家号ID"] == "external_buyer_customer_id"

    before = {r["upstream_order_no"]: (r["designated_customer_id"], r["designated_buyer_name"], r["assignment_kind"])
              for r in conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s", (ws,)).fetchall()}
    up = imports.upload(conn, ws, filename="demo-export-back.xlsx", data=data, actor_user_id=uid)
    imports.preview(conn, ws, up["batch_id"])
    again = imports.commit(conn, ws, up["batch_id"], uid)
    assert again["stats"]["update"] == 0 and again["stats"]["new"] == 0
    after = {r["upstream_order_no"]: (r["designated_customer_id"], r["designated_buyer_name"], r["assignment_kind"])
             for r in conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s", (ws,)).fetchall()}
    assert after == before
    assert before["PO-DEMO-X01"] == (None, None, "auto") and before["PO-DEMO-X03"] == (ID_A, None, "source")


_KEPT = ("designated_customer_id", "designated_buyer_name", "status", "status_reason", "assigned_account_id",
         "assignment_kind", "assignment_note")


@pytest.mark.parametrize("cid, name, where", [
    (ID_B, "店铺小号-DEMO", "assigned"),         # ID 命中 B，名称谁也对不上：按 ID 分给 B，说明里注明名称不同
    (ID_B, "环境-DEMO-A", "queued"),            # ID 与名称指向不同账号：不分配
    (ID_B, "环境-DEMO-B", "assigned"),          # 最平常的写法：ID 与名称都是 B
    (ID_B, None, "assigned"),                   # 只有 ID
    (None, "环境-DEMO-B", "assigned"),          # 只有名称
    (None, None, "assigned"),                   # 自动分配
    ("12345", None, "queued"),                  # 格式不对的 ID
    (None, "#N/A", "queued"),                   # 表格错误值
])
def test_export_then_reupload_unchanged_keeps_every_designation(conn, workspace, cid, name, where):
    """ALLOC-1：导出文件原样重新上传，任务的指定、状态、账号、分配方式、说明都不变，不产生 order.designation_changed。
    之前导出没有「买家号ID」列，「ID + 名称」的指定重新上传后被改写成只有名称：任务被从 ID 的账号上拉下来，
    或者落到名称指向的另一个账号上。"""
    ws, uid = workspace["id"], workspace["user_id"]
    _accounts(conn, ws)
    bid, _ = _upload(conn, ws, uid, [_file_row("PO-DEMO-Y01", 买家号ID=cid, 买家号=name)])
    imports.commit(conn, ws, bid, uid)
    before = _order(conn, ws, "PO-DEMO-Y01")
    assert (before["designated_customer_id"], before["designated_buyer_name"], before["status"]) == (cid, name, where)

    data = exports.orders_xlsx(conn, ws, view="all")
    values, col = _sheet(data)
    assert (values[1][col["买家号ID"]], values[1][col["买家号"]]) == (cid, name)
    up = imports.upload(conn, ws, filename="demo-export-back.xlsx", data=data, actor_user_id=uid)
    assert up["suggested_mapping"]["买家号ID"] == "external_buyer_customer_id"
    assert up["suggested_mapping"]["买家号"] == "external_buyer_account"
    imports.preview(conn, ws, up["batch_id"])
    row = imports.rows(conn, ws, up["batch_id"], page_size=50)["rows"][0]
    assert row["verdict"] == "unchanged" and not (row["reason"] or "").startswith("来源指定的买家号由")
    out = imports.commit(conn, ws, up["batch_id"], uid)
    assert out["stats"]["update"] == 0 and out["stats"]["unchanged"] == 1
    after = _order(conn, ws, "PO-DEMO-Y01")
    assert {k: after[k] for k in _KEPT} == {k: before[k] for k in _KEPT}
    assert _events(conn, ws, "PO-DEMO-Y01", "order.designation_changed") == []
    assert _events(conn, ws, "PO-DEMO-Y01", "order.designation_not_adopted") == []


# ── 待登记的买家号、概览 ──────────────────────────────────────────────────────


def test_discovered_is_the_union_of_browser_reports_and_source_designations(conn, workspace):
    """验收 修订二 第 8 条（列表部分）：来源指定但未登记的买家号 ID 与浏览器上报的并在一起；每项带 source_orders。"""
    ws, uid = workspace["id"], workspace["user_id"]
    _accounts(conn, ws)
    f.make_device(conn, ws, name="浏览器-甲", customer_id="A1DEMO0100", last_seen_at=NOW)        # 只来自浏览器
    f.make_device(conn, ws, name="浏览器-乙", customer_id="A1DEMO0200", last_seen_at=NOW)        # 浏览器 + 来源
    gone = f.make_account(conn, ws, name="环境-DEMO-旧", amazon_customer_id="A1DEMO0600")
    accounts.archive(conn, ws, gone["id"], uid)

    def order(**kw):
        return f.ingest_ready_order(conn, ws, **kw)

    order(external_buyer_customer_id="A1DEMO0200", external_buyer_account="环境-DEMO-乙")
    clock.freeze(NOW + timedelta(minutes=1))
    order(external_buyer_customer_id="A1DEMO0300", external_buyer_account="环境-DEMO-丙旧名")
    clock.freeze(NOW + timedelta(minutes=2))
    order(external_buyer_customer_id="A1DEMO0300", external_buyer_account="环境-DEMO-丙")       # 同一 ID 最近写的名字
    order(external_buyer_customer_id="A1DEMO0300", external_buyer_account="#N/A")              # 错误值不当名字
    order(external_buyer_customer_id="A1DEMO0600")                                            # 只登记在已归档账号上 = 没登记
    order(external_buyer_customer_id=ID_A)                                                    # 已登记：不列
    order(external_buyer_customer_id="demo-buyer")                                            # 形状不对：不列
    order(external_buyer_account="环境-DEMO-只有名字")                                          # 没有 ID：不列
    done = order(external_buyer_customer_id="A1DEMO0400")                                     # 已取消的任务：不算
    f.set_status(conn, done["id"], "cancelled")
    hist = order(external_buyer_customer_id="A1DEMO0500", external_amz_order_no="111-0000000-0000500")
    assert hist["status"] == "purchased"                                                      # 历史采购：不是指定

    items = accounts.discovered(conn, ws)
    assert [i["customer_id"] for i in items] == ["A1DEMO0100", "A1DEMO0200", "A1DEMO0300", "A1DEMO0600"]
    by_id = {i["customer_id"]: i for i in items}
    assert all(set(i) == {"customer_id", "ignored", "browsers", "last_reported_at", "name_hint", "source_orders"}
               for i in items)
    assert (by_id["A1DEMO0100"]["source_orders"], [b["name"] for b in by_id["A1DEMO0100"]["browsers"]]) == (
        0, ["浏览器-甲"])
    assert (by_id["A1DEMO0200"]["source_orders"], by_id["A1DEMO0200"]["name_hint"]) == (1, "环境-DEMO-乙")
    assert [b["name"] for b in by_id["A1DEMO0200"]["browsers"]] == ["浏览器-乙"]
    assert by_id["A1DEMO0300"] == {"customer_id": "A1DEMO0300", "ignored": False, "browsers": [],
                                   "last_reported_at": None, "name_hint": "环境-DEMO-丙", "source_orders": 3}
    assert by_id["A1DEMO0600"]["source_orders"] == 1 and by_id["A1DEMO0600"]["name_hint"] is None
    assert accounts.discovered_count(conn, ws) == 4
    assert overview_svc.overview(conn, ws)["discovered_buyers"] == 4
    assert accounts.discovered(conn, f.make_workspace(conn)["id"]) == []                       # 按工作区分开


def test_include_ignored_lists_every_ignored_id_even_without_a_browser(conn, workspace):
    """陷阱：忽略之后浏览器不再登录这个买家号，它就从列表里消失、无处恢复。include_ignored 列出忽略名单里的全部 ID。"""
    ws, uid = workspace["id"], workspace["user_id"]
    f.make_device(conn, ws, name="浏览器-甲", customer_id="A1DEMO0100", last_seen_at=NOW)
    f.ingest_ready_order(conn, ws, external_buyer_customer_id="A1DEMO0300")
    for cid in ("A1DEMO0100", "A1DEMO0300", "A1DEMO0900"):                    # 0900：没有浏览器、也没有任务
        accounts.ignore_buyer_id(conn, ws, cid, uid)
    assert accounts.discovered(conn, ws) == [] and accounts.discovered_count(conn, ws) == 0
    assert overview_svc.overview(conn, ws)["discovered_buyers"] == 0
    listed = accounts.discovered(conn, ws, include_ignored=True)
    assert [(i["customer_id"], i["ignored"], i["source_orders"], len(i["browsers"])) for i in listed] == [
        ("A1DEMO0100", True, 0, 1), ("A1DEMO0300", True, 1, 0), ("A1DEMO0900", True, 0, 0)]
    assert listed[2]["last_reported_at"] is None and listed[2]["name_hint"] is None
    accounts.unignore_buyer_id(conn, ws, "A1DEMO0300", uid)
    assert [i["customer_id"] for i in accounts.discovered(conn, ws)] == ["A1DEMO0300"]
    # 登记之后忽略记录随之清掉，不再出现在任何一组里
    accounts.create(conn, ws, {"name": "环境-DEMO-九", "amazon_customer_id": "A1DEMO0900"}, uid)
    assert [i["customer_id"] for i in accounts.discovered(conn, ws, include_ignored=True)] == [
        "A1DEMO0100", "A1DEMO0300"]


def test_overview_and_exception_rows_carry_designated_buyer(conn, workspace):
    ws = workspace["id"]
    a, _ = _accounts(conn, ws)
    o = f.ingest_ready_order(conn, ws, external_buyer_customer_id=ID_A)
    allocation.allocate(conn, ws, trigger="manual")
    f.make_attempt(conn, ws, o["id"], a["id"], status="failed")
    f.set_status(conn, o["id"], "exception")
    ov = overview_svc.overview(conn, ws)
    assert ov["recent_exceptions"][0]["designated_buyer"] == {"customer_id": ID_A, "name": None,
                                                              "account": {"id": a["id"], "name": "环境-DEMO-A"}}
    assert ov["recent_exceptions"][0]["assignment_kind"] == "source"
    assert ov["last_allocation"]["stats"]["designated"] == 1
    assert orders.get_detail(conn, ws, o["id"])["assignment_kind"] == "source"
