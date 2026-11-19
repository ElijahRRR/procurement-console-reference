"""结果导出：按批次 / 按视图导出 xlsx，带采购结果；只导出卡尾号；模拟结果标明；「买家号」「买家号ID」两列合起来
就是来源指定（一列不丢）；跨工作区隔离。全部数据虚构。"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal

import openpyxl
import pytest

from services import clock, exports, imports, sheet_io
from services.errors import DomainError
from tests import factories as f

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
HEADERS = ["平台订单号", "ASIN", "数量", "采购单价", "限价", "建议采购日期", "人工审核", "收货人姓名", "收货人地址",
           "收货人城市", "收货人州/省", "邮编", "收货人电话"]


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _row(no, **kw):
    r = {"平台订单号": no, "ASIN": "B0DEMO0001", "数量": 1, "采购单价": 25, "限价": 40, "建议采购日期": "2026-10-31",
         "人工审核": "通过", "收货人姓名": "Demo Buyer", "收货人地址": "100 Demo Street", "收货人城市": "Demo City",
         "收货人州/省": "FL", "邮编": "32960", "收货人电话": "555-0100"}
    r.update(kw)
    return r


def _import(conn, ws, uid, rows):
    up = imports.upload(conn, ws, filename="demo-export.xlsx",
                        data=sheet_io.write_xlsx([("订单", HEADERS, rows)]), actor_user_id=uid)
    imports.preview(conn, ws, up["batch_id"])
    imports.commit(conn, ws, up["batch_id"], uid)
    return up["batch_id"]


def _order(conn, ws, no):
    return conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = %s",
                        (ws, no)).fetchone()


def _succeed(conn, ws, no, acc, *, simulated, amazon_no, card="7860"):
    o = _order(conn, ws, no)
    att = f.make_attempt(conn, ws, o["id"], acc["id"], status="succeeded", simulated=simulated,
                         amazon_order_no=amazon_no, submitted=True)
    conn.execute("UPDATE procure.purchase_attempts SET total_before_tax = 41.97, shipping = 0.00, tax = 2.52, "
                 "total = 44.49, card_last4 = %s WHERE id = %s", (card, att["id"]))
    f.set_status(conn, o["id"], "purchased", purchased_attempt_id=att["id"], result_version=1)


def _sheet(data):
    return list(openpyxl.load_workbook(io.BytesIO(data))["采购结果"].values)


#: 表头 → 列序号（按表头取值，列序调整不影响断言）
COL = {h: i for i, h in enumerate(exports.EXPORT_HEADERS)}


def _cell(row, header):
    return row[COL[header]]


def test_batch_export_contains_results_and_only_card_last4(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws, name="环境-DEMO-A")
    bid = _import(conn, ws, uid, [_row("PO-DEMO-X01"), _row("PO-DEMO-X02"), _row("PO-DEMO-X03", 人工审核=None)])
    _succeed(conn, ws, "PO-DEMO-X01", acc, simulated=False, amazon_no="111-0000000-0000001")
    _succeed(conn, ws, "PO-DEMO-X02", acc, simulated=True, amazon_no="SIM-20261031-2", card="4111 1111 1111 7860")
    values = _sheet(exports.orders_xlsx(conn, ws, batch_id=bid))
    assert list(values[0]) == exports.EXPORT_HEADERS
    rows = {v[0]: v for v in values[1:]}
    assert list(rows) == ["PO-DEMO-X01", "PO-DEMO-X02", "PO-DEMO-X03"]
    assert exports.EXPORT_HEADERS[:6] == ["平台订单号", "状态", "买家号", "买家号ID", "分配账号", "AMZ订单号"]
    x1 = rows["PO-DEMO-X01"]
    assert [_cell(x1, h) for h in ("状态", "买家号", "AMZ订单号")] == ["已采购", "环境-DEMO-A", "111-0000000-0000001"]
    assert _cell(x1, "买家号ID") is None                                  # 采购账号没有登记买家号 ID：留空
    assert [Decimal(str(_cell(x1, h))) for h in ("税前总额", "运费", "税费", "总额")] == [
        Decimal("41.97"), Decimal("0"), Decimal("2.52"), Decimal("44.49")]
    assert _cell(x1, "卡尾号") == "7860" and _cell(x1, "建议采购日期") == datetime(2026, 10, 31)
    assert _cell(x1, "结果来源") == "本系统"
    assert _cell(x1, "最近更新时间") == datetime(2026, 10, 31, 10, 0)     # 最近更新时间按北京时间
    x2 = rows["PO-DEMO-X02"]
    assert _cell(x2, "AMZ订单号") == "SIM-20261031-2" and _cell(x2, "结果来源") == "模拟"
    assert _cell(x2, "卡尾号") == "7860"                                  # 绝不导出完整卡号
    assert all("4111" not in str(c) for v in values for c in v if c is not None)
    x3 = rows["PO-DEMO-X03"]
    assert _cell(x3, "状态") == "待确认" and _cell(x3, "AMZ订单号") is None and "人审" in _cell(x3, "异常原因")
    assert _cell(x3, "结果来源") is None and _cell(x3, "买家号") is None and _cell(x3, "分配账号") is None
    assert _cell(x3, "买家号ID") is None


def test_buyer_id_column_carries_the_designation_or_the_purchasing_account(conn, workspace):
    """ALLOC-1：「买家号ID」列。没有采购结果 → 任务上来源指定的买家号 ID（与「买家号」一起就是完整的指定，原样写出，
    解析不了的也照写）；有采购结果 → 采购账号登记的买家号 ID（来源已有单号、买家号对得上账号时也是它；对不上账号
    → 留空，不抄来源行里的 ID）。按文本格式写。"""
    ws, uid = workspace["id"], workspace["user_id"]
    f.make_account(conn, ws, name="环境-DEMO-A", amazon_customer_id="A1DEMO0000")
    b = f.make_account(conn, ws, name="环境-DEMO-B", amazon_customer_id="A1DEMO0001", priority=9)
    headers = [*HEADERS, "买家号", "买家号ID", "AMZ订单号"]
    up = imports.upload(conn, ws, filename="demo-export.xlsx", actor_user_id=uid, data=sheet_io.write_xlsx([("订单", headers, [
        _row("PO-DEMO-W01", 买家号ID="A1DEMO0000", 买家号="店铺小号-DEMO"),      # ID + 对不上账号的名称
        _row("PO-DEMO-W02", 买家号ID="a1demo0000"),                           # 只有 ID（清洗后写出）
        _row("PO-DEMO-W03", 买家号="环境-DEMO-A"),                             # 只有名称
        _row("PO-DEMO-W04", 买家号ID="12345"),                                # 格式不对的 ID：照写，不能丢
        _row("PO-DEMO-W05"),                                                  # 自动分配，随后由 B 买到
        _row("PO-DEMO-W06", 买家号="环境-DEMO-A", AMZ订单号="111-0000000-0000066"),   # 历史采购，买家号对得上账号 A
        _row("PO-DEMO-W07", 买家号="店铺小号-DEMO", 买家号ID="A1DEMO0009", AMZ订单号="111-0000000-0000077"),   # 对不上
    ])]))
    imports.preview(conn, ws, up["batch_id"])
    imports.commit(conn, ws, up["batch_id"], uid)
    _succeed(conn, ws, "PO-DEMO-W05", b, simulated=True, amazon_no="SIM-20261031-5")
    data = exports.orders_xlsx(conn, ws, batch_id=up["batch_id"])
    rows = {v[0]: v for v in _sheet(data)[1:]}
    got = {no: (_cell(r, "买家号"), _cell(r, "买家号ID")) for no, r in rows.items()}
    assert got == {
        "PO-DEMO-W01": ("店铺小号-DEMO", "A1DEMO0000"), "PO-DEMO-W02": (None, "A1DEMO0000"),
        "PO-DEMO-W03": ("环境-DEMO-A", None), "PO-DEMO-W04": (None, "12345"),
        "PO-DEMO-W05": ("环境-DEMO-B", "A1DEMO0001"),                          # 采购账号的名称与它登记的 ID
        "PO-DEMO-W06": ("环境-DEMO-A", "A1DEMO0000"),                          # 来源已有单号，买家号就是账号 A
        "PO-DEMO-W07": ("店铺小号-DEMO", None)}                                # 来源已有单号，没有对应的采购账号
    assert _cell(rows["PO-DEMO-W06"], "结果来源") == _cell(rows["PO-DEMO-W07"], "结果来源") == "来源已有单号"
    sheet = openpyxl.load_workbook(io.BytesIO(data))["采购结果"]
    id_col = COL["买家号ID"] + 1
    assert sheet.cell(row=1, column=id_col).value == "买家号ID"
    assert all(sheet.cell(row=r, column=id_col).number_format == "@" for r in range(2, sheet.max_row + 1))
    assert isinstance(_cell(rows["PO-DEMO-W04"], "买家号ID"), str)             # 「12345」是文本，不是数字


def test_view_export_uses_order_views(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws, name="环境-DEMO-A")
    _import(conn, ws, uid, [_row("PO-DEMO-Y01"), _row("PO-DEMO-Y02"), _row("PO-DEMO-Y03", ASIN=None)])
    _succeed(conn, ws, "PO-DEMO-Y01", acc, simulated=False, amazon_no="111-0000000-0000011")
    purchased = _sheet(exports.orders_xlsx(conn, ws, view="purchased"))
    assert [v[0] for v in purchased[1:]] == ["PO-DEMO-Y01"]
    needs = _sheet(exports.orders_xlsx(conn, ws, view="needs_info"))
    assert [v[0] for v in needs[1:]] == ["PO-DEMO-Y03"] and "ASIN" in _cell(needs[1], "异常原因")
    assert len(_sheet(exports.orders_xlsx(conn, ws))) == 4               # 缺省全部 + 表头
    with pytest.raises(DomainError):
        exports.orders_xlsx(conn, ws, view="bogus")


def test_export_scoping_and_uncommitted_batches(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    ws_b = f.make_workspace(conn)["id"]
    bid = _import(conn, ws, uid, [_row("PO-DEMO-Z01")])
    with pytest.raises(DomainError) as e:
        exports.orders_xlsx(conn, ws_b, batch_id=bid)
    assert e.value.code == "NOT_FOUND"
    assert len(_sheet(exports.orders_xlsx(conn, ws_b))) == 1             # 乙工作区只有表头
    up = imports.upload(conn, ws, filename="demo-pending.xlsx",
                        data=sheet_io.write_xlsx([("订单", HEADERS, [_row("PO-DEMO-Z02")])]), actor_user_id=uid)
    with pytest.raises(DomainError) as e:
        exports.orders_xlsx(conn, ws, batch_id=up["batch_id"])
    assert e.value.code == "VALIDATION"
