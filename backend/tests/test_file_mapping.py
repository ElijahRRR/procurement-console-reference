"""services/file_mapping：标准模板列、别名、表头自动识别、行规范化、模板生成与回读。"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from services import errors, file_mapping, sheet_io
from services.normalize import SYSTEM_FIELD_KEYS

FIXTURES = Path(__file__).parent / "fixtures"
UTC = timezone.utc

#: 契约 §7 标准模板列（顺序即列序）
CONTRACT_COLUMNS = [
    "平台订单号", "店铺账号", "收货人姓名", "收货人地址", "地址2", "收货人城市", "收货人州/省", "邮编", "收货人电话",
    "AMZ订单号", "买家号", "买家号ID", "收货人国家", "ASIN", "数量", "收货人区", "人工审核", "限价", "采购单价",
    "下单时间", "配送天数", "建议采购日期", "店铺", "采购订单号", "SKU", "销售总额", "售后状态", "备注",
]

#: 小蜜蜂公开采购导入模板的 15 列
SMALLBEE_15 = ["店铺账号", "平台订单号", "收货人姓名", "收货人地址", "收货人城市", "收货人州/省", "邮编", "收货人电话",
               "AMZ订单号", "买家号", "买家号ID", "收货人国家", "ASIN", "数量", "收货人区"]


@pytest.fixture(scope="module")
def feishu_headers() -> list[str]:
    return json.loads((FIXTURES / "feishu_sample_headers.json").read_text(encoding="utf-8"))["headers"]


# ── 列与别名 ─────────────────────────────────────────────────────────────────


def test_standard_columns_match_contract():
    assert [h for h, _, _ in file_mapping.STANDARD_COLUMNS] == CONTRACT_COLUMNS
    assert file_mapping.STANDARD_HEADERS == CONTRACT_COLUMNS
    keys = [k for _, k, _ in file_mapping.STANDARD_COLUMNS if k]
    assert len(keys) == len(set(keys))
    assert set(keys) <= set(SYSTEM_FIELD_KEYS)
    assert all(note for _, _, note in file_mapping.STANDARD_COLUMNS)
    assert dict((h, k) for h, k, _ in file_mapping.STANDARD_COLUMNS)["平台订单号"] == "upstream_order_no"


def test_aliases_are_unambiguous():
    owner: dict[str, str] = {}
    for key, names in file_mapping.ALIASES.items():
        assert key in SYSTEM_FIELD_KEYS
        for n in names:
            assert owner.setdefault(n, key) == key, f"别名「{n}」同时属于 {owner[n]} 和 {key}"
    standard = {h: k for h, k, _ in file_mapping.STANDARD_COLUMNS if k}
    for n, key in owner.items():
        if n in standard:
            assert standard[n] == key, n
    for n in ("买家号1", "数量1", "AMZ订单号", "亚马逊订单号", "Amazon订单号", "平台订单号", "收件人", "邮编/Zip", "地址1",
              "国家", "买家号id"):
        assert n in owner, n


# ── 自动识别 ─────────────────────────────────────────────────────────────────


def test_suggest_mapping_standard_template_is_one_to_one():
    got = file_mapping.suggest_mapping(CONTRACT_COLUMNS)
    assert got == {h: k for h, k, _ in file_mapping.STANDARD_COLUMNS}


def test_suggest_mapping_smallbee_15_columns():
    got = file_mapping.suggest_mapping(SMALLBEE_15)
    assert got["店铺账号"] == "store_name"          # 没有「店铺」列时店铺账号当店铺
    assert all(v is not None for v in got.values())
    assert len(set(got.values())) == 15


def test_suggest_mapping_feishu_headers(feishu_headers):
    got = file_mapping.suggest_mapping(feishu_headers)
    expected = {
        "平台订单号": "upstream_order_no", "order_line_id": "order_line_id", "下单时间": "order_time",
        "店铺": "store_name", "采购订单号": "sales_po_no", "SKU": "sku", "销售总额": "sales_total",
        "采购单价": "unit_price", "数量": "quantity", "建议采购日期": "suggested_purchase_date",
        "售后状态": "after_sale_value", "人工审核": "review_value", "采购状态": "purchase_status_value",
        "采购方": "purchaser", "买家号": "external_buyer_account", "收货人姓名": "recipient_name",
        "收货人地址": "address1", "收货人城市": "city", "收货人州/省": "state", "邮编": "postal_code",
        "收货人电话": "phone", "AMZ订单号": "external_amz_order_no", "买家号id": "external_buyer_customer_id",
        "国家": "country", "ASIN": "asin", "配送方式": "ship_method", "地址2": "address2", "限价": "price_cap",
    }
    assert {h: k for h, k in got.items() if k} == expected
    # 备用列不抢已被占用的字段
    for h in ("数量1", "买家号1", "地址1", "来源码", "店铺账号", "总成本USD", "订单状态", "备注"):
        assert got[h] is None, h
    assert set(got) == set(feishu_headers)


def test_suggest_mapping_aliases_and_loose():
    got = file_mapping.suggest_mapping(["*平台订单号", " asin ", "收件人", "邮编/Zip", "亚马逊订单号", "数量1",
                                        "收货人州／省", "Upstream_Order_No", "收件人城市"])
    assert got == {"*平台订单号": "upstream_order_no", " asin ": "asin", "收件人": "recipient_name",
                   "邮编/Zip": "postal_code", "亚马逊订单号": "external_amz_order_no", "数量1": "quantity",
                   "收货人州／省": "state", "Upstream_Order_No": None, "收件人城市": "city"}


def test_suggest_mapping_each_key_once_exact_wins():
    got = file_mapping.suggest_mapping(["ASIN (2)", "来源码", "ASIN", "Asin"])
    assert got == {"ASIN (2)": None, "来源码": None, "ASIN": "asin", "Asin": None}
    got = file_mapping.suggest_mapping(["地址1", "收货人地址"])
    assert got == {"地址1": None, "收货人地址": "address1"}


# ── 映射校验与行规范化 ────────────────────────────────────────────────────────


def test_validate_mapping():
    file_mapping.validate_mapping({"平台订单号": "upstream_order_no", "备注": None})
    with pytest.raises(errors.DomainError) as ei:
        file_mapping.validate_mapping({"ASIN": "asin"})
    assert "平台订单号" in ei.value.message
    with pytest.raises(errors.DomainError):
        file_mapping.validate_mapping({"平台订单号": "upstream_order_no", "X": "no_such_key"})
    with pytest.raises(errors.DomainError):
        file_mapping.validate_mapping({"平台订单号": "upstream_order_no", "A": "asin", "B": "asin"})


def test_normalize_row_basic_and_first_non_empty():
    mapping = {"平台订单号": "upstream_order_no", "ASIN": "asin", "来源码": "asin", "数量": "quantity",
               "限价": "price_cap", "人工审核": "review_value", "备注": None}
    raw = {"平台订单号": " PO-DEMO-7 ", "ASIN": "  ", "来源码": "b0demo0007", "数量": "2", "限价": "$50",
           "人工审核": "通过", "备注": "随便写"}
    r = file_mapping.normalize_row(raw, mapping)
    assert r.upstream_order_no == "PO-DEMO-7" and r.asin == "B0DEMO0007" and r.quantity == 2
    assert r.price_cap == Decimal("50.00") and r.review == "approved" and r.issues == []
    r = file_mapping.normalize_row(raw, mapping, {"approved_values": ["OK"]})
    assert r.review == "unknown"


def test_normalize_row_missing_columns_and_unknown_key():
    r = file_mapping.normalize_row({"x": 1}, {"平台订单号": "upstream_order_no"})
    assert r.upstream_order_no == "" and r.issues[0] == "缺少平台订单号"
    with pytest.raises(errors.DomainError):
        file_mapping.normalize_row({"x": 1}, {"x": "bogus"})


# ── 模板 ─────────────────────────────────────────────────────────────────────


def test_template_xlsx_reread_and_mapped_one_to_one():
    data = file_mapping.template_xlsx()
    sheets = sheet_io.read_workbook(data, "采购导入模板.xlsx")
    assert [s.name for s in sheets] == ["采购订单", "说明"]
    orders, notes = sheets
    assert orders.headers == CONTRACT_COLUMNS and orders.rows == []
    assert file_mapping.suggest_mapping(orders.headers) == {h: k for h, k, _ in file_mapping.STANDARD_COLUMNS}

    assert notes.headers == ["列名", "是否必填", "说明"]
    assert [r["列名"] for r in notes.rows] == CONTRACT_COLUMNS
    required = [r["列名"] for r in notes.rows if r["是否必填"] == "必填"]
    assert required == ["平台订单号"]
    assert all(r["说明"] for r in notes.rows)

    ws = openpyxl.load_workbook(io.BytesIO(data))["采购订单"]
    zip_col = openpyxl.utils.get_column_letter(CONTRACT_COLUMNS.index("邮编") + 1)
    assert ws.column_dimensions[zip_col].number_format == "@"
    assert ws.freeze_panes == "A2"


def test_template_filled_and_imported_end_to_end():
    row = {"平台订单号": "PO-DEMO-0001SKU-DEMO-A", "收货人姓名": "Demo Buyer", "收货人地址": "100 Demo St",
           "收货人城市": "Demo City", "收货人州/省": "fl", "邮编": "02134", "收货人电话": "555-0100",
           "ASIN": "b0demo0001", "数量": 1, "人工审核": "通过", "限价": 78.69, "采购单价": 41.97,
           "下单时间": "2026/10/27 21:31", "配送天数": 4, "建议采购日期": datetime(2026, 10, 31), "备注": "DEMO"}
    data = sheet_io.write_xlsx([("采购订单", CONTRACT_COLUMNS, [row])])
    sheet = sheet_io.read_workbook(data, "filled.xlsx")[0]
    mapping = file_mapping.suggest_mapping(sheet.headers)
    file_mapping.validate_mapping(mapping)
    r = file_mapping.normalize_row(sheet.rows[0], mapping)
    assert r.upstream_order_no == "PO-DEMO-0001SKU-DEMO-A"
    assert (r.asin, r.quantity, r.unit_price, r.price_cap) == ("B0DEMO0001", 1, Decimal("41.97"), Decimal("78.69"))
    assert r.order_time == datetime(2026, 10, 27, 13, 31, tzinfo=UTC)
    assert r.suggested_purchase_date == date(2026, 10, 31) and r.ship_days == 4
    assert (r.state, r.postal_code, r.country, r.review) == ("FL", "02134", "US", "approved")
    assert r.store_name is None and r.issues == []
