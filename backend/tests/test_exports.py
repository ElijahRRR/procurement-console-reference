"""结果导出：按批次 / 按视图导出 xlsx，带采购结果；只导出卡尾号；模拟结果标明；跨工作区隔离。全部数据虚构。"""

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
    x1 = rows["PO-DEMO-X01"]
    assert x1[1:4] == ("已采购", "环境-DEMO-A", "111-0000000-0000001")
    assert [Decimal(str(v)) for v in x1[4:8]] == [Decimal("41.97"), Decimal("0"), Decimal("2.52"), Decimal("44.49")]
    assert x1[8] == "7860" and x1[9] == datetime(2026, 10, 31) and x1[12] == "本系统"
    assert x1[10] == datetime(2026, 10, 31, 10, 0)                       # 最近更新时间按北京时间
    x2 = rows["PO-DEMO-X02"]
    assert x2[3] == "SIM-20261031-2" and x2[12] == "模拟" and x2[8] == "7860"   # 绝不导出完整卡号
    assert all("4111" not in str(c) for v in values for c in v if c is not None)
    x3 = rows["PO-DEMO-X03"]
    assert x3[1] == "待确认" and x3[3] is None and "人审" in x3[11] and x3[12] is None


def test_view_export_uses_order_views(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws, name="环境-DEMO-A")
    _import(conn, ws, uid, [_row("PO-DEMO-Y01"), _row("PO-DEMO-Y02"), _row("PO-DEMO-Y03", ASIN=None)])
    _succeed(conn, ws, "PO-DEMO-Y01", acc, simulated=False, amazon_no="111-0000000-0000011")
    purchased = _sheet(exports.orders_xlsx(conn, ws, view="purchased"))
    assert [v[0] for v in purchased[1:]] == ["PO-DEMO-Y01"]
    needs = _sheet(exports.orders_xlsx(conn, ws, view="needs_info"))
    assert [v[0] for v in needs[1:]] == ["PO-DEMO-Y03"] and "ASIN" in needs[1][11]
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
