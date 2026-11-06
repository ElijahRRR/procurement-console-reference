"""services/feishu_fields：链接解析、单元格取值、默认字段对应（真实表头）、写回值构造。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from services import errors
from services import feishu_fields as ff

FIXTURES = Path(__file__).parent / "fixtures"
UTC = timezone.utc
BJ = ZoneInfo("Asia/Shanghai")
TOKEN = "basDEMO0000000000000000001"


@pytest.fixture(scope="module")
def headers() -> list[str]:
    return json.loads((FIXTURES / "feishu_sample_headers.json").read_text(encoding="utf-8"))["headers"]


@pytest.fixture(scope="module")
def demo() -> dict:
    return json.loads((FIXTURES / "feishu_demo_records.json").read_text(encoding="utf-8"))


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# ── 链接 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("link,expected", [
    (f"https://demo.feishu.cn/base/{TOKEN}?table=tblDEMO000000001&view=vewDEMO0001",
     {"kind": "base", "token": TOKEN, "table_id": "tblDEMO000000001", "view_id": "vewDEMO0001"}),
    (f"https://demo.feishu.cn/base/{TOKEN}?from=from_copylink",
     {"kind": "base", "token": TOKEN, "table_id": None, "view_id": None}),
    (f"  demo.feishu.cn/base/{TOKEN}?table=tblDEMO000000001  ",
     {"kind": "base", "token": TOKEN, "table_id": "tblDEMO000000001", "view_id": None}),
    ("https://demo.feishu.cn/wiki/wikDEMO000000000000000001?table=tblDEMO000000001",
     {"kind": "wiki", "token": "wikDEMO000000000000000001", "table_id": "tblDEMO000000001", "view_id": None}),
    (f"https://demo.larksuite.com/base/{TOKEN}?table=tblDEMO000000002&view=vewDEMO0002",
     {"kind": "base", "token": TOKEN, "table_id": "tblDEMO000000002", "view_id": "vewDEMO0002"}),
    (f"https://feishu.cn/base/{TOKEN}", {"kind": "base", "token": TOKEN, "table_id": None, "view_id": None}),
])
def test_parse_link_ok(link, expected):
    assert ff.parse_link(link) == expected


@pytest.mark.parametrize("link", [
    "", "   ", f"https://demo.feishu.cn.evil.example/base/{TOKEN}", f"https://evilfeishu.cn/base/{TOKEN}",
    f"ftp://demo.feishu.cn/base/{TOKEN}", f"https://demo.feishu.cn/sheets/{TOKEN}", "https://demo.feishu.cn/base/",
    "https://demo.feishu.cn/base/abc", f"https://demo.feishu.cn/base/{TOKEN}?table=notatable",
    f"https://demo.feishu.cn/base/{TOKEN}?table=tblDEMO000000001&view=bad-view",
    "https://demo.feishu.cn/share/base/view/shrDEMO000000000000000001", "not a link at all",
])
def test_parse_link_rejects(link):
    with pytest.raises(errors.DomainError) as ei:
        ff.parse_link(link)
    assert ei.value.code == "VALIDATION"
    assert TOKEN not in ei.value.message


# ── 单元格取值 ───────────────────────────────────────────────────────────────


def test_cell_text_segments_and_plain():
    segs = [{"type": "text", "text": "100 Demo "}, {"type": "url", "text": "St", "link": "https://example.invalid"}]
    assert ff.cell_to_python(segs, ff.TEXT) == "100 Demo St"
    mention = [{"type": "text", "text": "请"}, {"type": "mention", "mentionType": "User", "text": "@Demo", "token": "ou_x"}]
    assert ff.cell_to_python(mention, ff.TEXT) == "请@Demo"
    assert ff.cell_to_python("plain", ff.TEXT) == "plain"
    assert ff.cell_to_python([], ff.TEXT) is None
    assert ff.cell_to_python(None, ff.TEXT) is None
    assert ff.cell_to_python([{"type": "text", "text": ""}], ff.TEXT) is None


def test_cell_numbers_selects_checkbox_phone():
    assert ff.cell_to_python(41.97, ff.NUMBER) == 41.97
    assert ff.cell_to_python(2, ff.NUMBER) == 2
    assert ff.cell_to_python("通过", ff.SINGLE_SELECT) == "通过"
    assert ff.cell_to_python(["A", "B"], ff.MULTI_SELECT) == ["A", "B"]
    assert ff.cell_to_python(["A"], ff.MULTI_SELECT) == ["A"]
    assert ff.cell_to_python(True, ff.CHECKBOX) is True
    assert ff.cell_to_python(False, ff.CHECKBOX) is False
    assert ff.cell_to_python("555-0100", ff.PHONE) == "555-0100"
    assert ff.cell_to_python("001", ff.AUTO_NUMBER) == "001"


def test_cell_dates_are_aware_utc():
    t = datetime(2026, 10, 27, 21, 31, tzinfo=BJ)
    for ftype in (ff.DATETIME, ff.CREATED_TIME, ff.MODIFIED_TIME):
        v = ff.cell_to_python(ms(t), ftype)
        assert v == t and v.tzinfo == UTC
    assert ff.cell_to_python(str(ms(t)), ff.DATETIME) == t
    assert ff.cell_to_python(ms(t), ff.NUMBER) == ms(t)          # 数字列不当日期


def test_cell_people_attachments_links_location_url():
    people = [{"id": "ou_demo1", "name": "Demo 甲", "en_name": "Demo A", "email": "a@example.invalid"},
              {"id": "ou_demo2", "name": "Demo 乙"}]
    assert ff.cell_to_python(people, ff.USER) == "Demo 甲, Demo 乙"
    files = [{"file_token": "boxDEMO1", "name": "demo.png", "size": 10, "type": "image/png",
              "url": "https://example.invalid/a"}]
    assert ff.cell_to_python(files, ff.ATTACHMENT) == "demo.png"
    assert ff.cell_to_python({"link_record_ids": ["recA", "recB"]}, ff.SINGLE_LINK) == "recA, recB"
    linked = [{"record_ids": ["recA"], "table_id": "tblDEMO", "text": "主单 DEMO", "text_arr": ["主单 DEMO"],
               "type": "text"}]
    assert ff.cell_to_python(linked, ff.DUPLEX_LINK) == "主单 DEMO"
    assert ff.cell_to_python({"link": "https://example.invalid/p", "text": "商品页"}, ff.URL) == "商品页"
    assert ff.cell_to_python({"link": "https://example.invalid/p", "text": ""}, ff.URL) == "https://example.invalid/p"
    loc = {"location": "-80.1,26.1", "name": "Demo Plaza", "full_address": "1 Demo Plaza, Demo City"}
    assert ff.cell_to_python(loc, ff.LOCATION) == "1 Demo Plaza, Demo City"
    assert ff.cell_to_python([{"id": "oc_demo", "name": "采购群 DEMO"}], ff.GROUP_CHAT) == "采购群 DEMO"


def test_cell_formula_and_lookup_recursive():
    t = datetime(2026, 10, 31, tzinfo=BJ)
    assert ff.cell_to_python({"type": 1, "value": [{"type": "text", "text": "PO-DEMO-1"},
                                                   {"type": "text", "text": "SKU-A"}]}, ff.FORMULA) == "PO-DEMO-1SKU-A"
    assert ff.cell_to_python({"type": 2, "value": [44.49]}, ff.FORMULA) == 44.49
    assert ff.cell_to_python({"type": 5, "value": [ms(t)]}, ff.FORMULA) == t
    assert ff.cell_to_python({"type": 5, "value": []}, ff.FORMULA) is None
    assert ff.cell_to_python({"type": 2, "value": [1, 2]}, ff.LOOKUP) == [1, 2]
    assert ff.cell_to_python({"type": 3, "value": ["通过"]}, ff.LOOKUP) == "通过"
    assert ff.cell_to_python({"type": 11, "value": [{"id": "ou_1", "name": "Demo"}]}, ff.LOOKUP) == "Demo"
    assert ff.cell_to_python({"type": 20, "value": [{"type": 2, "value": [3]}]}, ff.LOOKUP) == 3


# ── 默认字段对应 ─────────────────────────────────────────────────────────────


def test_default_field_map_uses_only_real_headers(headers):
    used = []
    for key, cols in ff.DEFAULT_FIELD_MAP.items():
        assert key in ff.SYSTEM_FIELD_KEYS
        for c in ([cols] if isinstance(cols, str) else cols):
            assert c in headers, c
            used.append(c)
    assert len(used) == len(set(used))            # 每列至多对应一个系统字段
    m = ff.DEFAULT_FIELD_MAP
    assert m["upstream_order_no"] == "平台订单号"
    assert m["asin"] == ["ASIN", "来源码"] and m["quantity"] == ["数量", "数量1"]
    assert m["address1"] == ["收货人地址", "地址1"]
    assert m["price_cap"] == "限价" and m["review_value"] == "人工审核"
    assert m["external_amz_order_no"] == "AMZ订单号"


def test_suggest_field_map_on_real_headers_equals_default(headers):
    assert ff.suggest_field_map(headers) == ff.DEFAULT_FIELD_MAP


def test_suggest_field_map_loose_and_missing():
    got = ff.suggest_field_map(["平台订单号", "asin", "数量 ", "配送天数", "收件人", "买家号ID", "无关列"])
    assert got == {"upstream_order_no": "平台订单号", "asin": "asin", "quantity": "数量 ", "ship_days": "配送天数",
                   "recipient_name": "收件人", "external_buyer_customer_id": "买家号ID"}


def test_used_columns(headers):
    cols = ff.used_columns(ff.DEFAULT_FIELD_MAP)
    assert cols[0] == "平台订单号" and "来源码" in cols and "数量1" in cols
    assert len(cols) == len(set(cols)) and set(cols) <= set(headers)
    assert ff.used_columns({"asin": ["ASIN", ""], "bogus": "X"}) == ["ASIN"]


# ── 记录 → 系统字段 → UpstreamRecord ──────────────────────────────────────────


def test_record_to_values_fallbacks(demo):
    types = ff.field_types_of(demo["fields"])
    rec2 = demo["records"][1]["fields"]
    vals = ff.record_to_values(rec2, ff.DEFAULT_FIELD_MAP, types)
    assert vals["quantity"] == 2                   # 「数量」为空 → 公式列「数量1」
    assert vals["asin"] == "B0DEMO0002"            # 「ASIN」为空 → 「来源码」
    assert vals["address1"] == "200 Sample Ave"    # 「收货人地址」为空 → 「地址1」
    assert vals["suggested_purchase_date"] is None  # 公式结果为空
    assert vals["external_buyer_account"] is None
    assert vals["review_value"] is None


def test_normalize_demo_records(demo):
    types = ff.field_types_of(demo["fields"])
    r1, r2, r3 = (ff.normalize_record(r["fields"], ff.DEFAULT_FIELD_MAP, types, None) for r in demo["records"])

    assert r1.upstream_order_no == "PO-DEMO-0001SKU-DEMO-A"
    assert r1.order_line_id == "OL-DEMO-0001" and r1.store_name == "STORE-DEMO"
    assert r1.asin == "B0DEMO0001" and r1.quantity == 1
    assert r1.unit_price == Decimal("41.97") and r1.price_cap == Decimal("78.69") and r1.sales_total == Decimal("59.99")
    assert r1.order_time == datetime(2026, 10, 27, 13, 31, tzinfo=UTC)
    assert r1.suggested_purchase_date == date(2026, 10, 31)       # 北京日期，不是 UTC 的 10-30
    assert (r1.recipient_name, r1.address1, r1.city, r1.state, r1.postal_code, r1.phone, r1.country) == \
        ("Demo Buyer", "100 Demo St", "Demo City", "FL", "02134", "555-0100", "US")
    assert r1.review == "approved" and r1.purchaser == "自采购DEMO" and r1.purchase_status_value == "待拍单"
    assert r1.issues == []

    assert r2.review == "unknown" and r2.quantity == 2 and r2.asin == "B0DEMO0002"
    assert r2.address2 == "Apt 3" and r2.suggested_purchase_date is None and r2.issues == []

    assert r3.external_amz_order_no == "111-0000000-0000003"
    assert r3.external_buyer_account == "BUYER-DEMO-1" and r3.external_buyer_customer_id == "A1DEMO0000"
    assert r3.purchase_status_value == "已拍单"


def test_snapshot_fields_is_json(demo):
    types = ff.field_types_of(demo["fields"])
    snap = ff.snapshot_fields(demo["records"][0]["fields"], types)
    json.dumps(snap, ensure_ascii=False)
    assert snap["下单时间"] == "2026-10-27T13:31:00+00:00"
    assert snap["备注"] == "请@Demo 同事 跟进"
    assert snap["产品截图"] == "demo.png"
    assert snap["总成本USD"] == 44.49


# ── 写回 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ftype,ui,expected", [
    (1, "Text", True), (2, "Currency", True), (3, "SingleSelect", True), (4, "MultiSelect", True),
    (5, "DateTime", True), (7, "Checkbox", True), (13, "Phone", True), (15, "Url", True),
    (11, "User", False), (17, "Attachment", False), (18, "SingleLink", False), (19, "Lookup", False),
    (20, "Formula", False), (21, "DuplexLink", False), (1001, "CreatedTime", False), (1002, "ModifiedTime", False),
    (1003, "CreatedUser", False), (1004, "ModifiedUser", False), (1005, "AutoNumber", False), (None, None, False),
])
def test_writable(ftype, ui, expected):
    assert ff.writable({"field_name": "x", "type": ftype, "ui_type": ui}) is expected


def test_non_writable_types_constant():
    assert {17, 18, 19, 20, 21, 1001, 1002, 1003, 1004, 1005} <= ff.NON_WRITABLE_TYPES
    assert not ({1, 2, 3, 5} & ff.NON_WRITABLE_TYPES)


def test_default_writeback_map(headers):
    for key, col in ff.DEFAULT_WRITEBACK_MAP.items():
        assert key in ff.RESULT_FIELD_KEYS and col in headers
    assert "total" not in ff.DEFAULT_WRITEBACK_MAP           # 「总成本USD」不默认写
    assert [f["key"] for f in ff.RESULT_FIELDS] == ["buyer_account", "buyer_customer_id", "amazon_order_no",
                                                    "purchase_status", "total_before_tax", "shipping", "tax",
                                                    "total", "card_last4"]


def test_suggest_writeback_map_skips_formula_columns(demo):
    got = ff.suggest_writeback_map(demo["fields"])
    assert got == ff.DEFAULT_WRITEBACK_MAP
    fields = [dict(f) for f in demo["fields"]]
    for f in fields:
        if f["field_name"] == "AMZ订单号":
            f["type"], f["ui_type"] = 20, "Formula"
    got = ff.suggest_writeback_map(fields)
    assert "amazon_order_no" not in got
    assert ff.suggest_writeback_map([]) == {}


def test_build_writeback_fields_by_type(demo):
    types = ff.field_types_of(demo["fields"])
    types["采购状态"] = ff.SINGLE_SELECT
    result = {"buyer_account": "BUYER-DEMO-1", "buyer_customer_id": None, "amazon_order_no": "111-0000000-0000009",
              "purchase_status": "succeeded", "total_before_tax": Decimal("41.97"), "shipping": Decimal("0.00"),
              "tax": Decimal("2.52"), "total": Decimal("44.49"), "card_last4": "4111 0000 0000 7860"}
    out = ff.build_writeback_fields(result, ff.DEFAULT_WRITEBACK_MAP, types)
    assert out == {"买家号": "BUYER-DEMO-1", "AMZ订单号": "111-0000000-0000009", "采购状态": "已拍单",
                   "税前总额USD": 41.97, "运费USD": 0.0, "税费USD": 2.52, "付款卡号": "7860"}
    assert isinstance(out["税前总额USD"], float)


def test_build_writeback_fields_conversions():
    wb = {"amazon_order_no": "订单", "total": "总额文本", "tax": "税费数字", "purchase_status": "状态多选",
          "shipping": "日期列", "card_last4": "卡"}
    types = {"订单": ff.TEXT, "总额文本": ff.TEXT, "税费数字": ff.NUMBER, "状态多选": ff.MULTI_SELECT,
             "日期列": ff.DATETIME, "卡": ff.TEXT}
    when = datetime(2026, 10, 31, 14, 0, tzinfo=BJ)
    out = ff.build_writeback_fields({"amazon_order_no": "SIM-NEVER", "total": Decimal("44.5"), "tax": "2.52",
                                     "purchase_status": "已拍单", "shipping": when, "card_last4": "12"},
                                    wb, types)
    assert out == {"订单": "SIM-NEVER", "总额文本": "44.50", "税费数字": 2.52, "状态多选": ["已拍单"],
                   "日期列": ms(when)}
    out = ff.build_writeback_fields({"shipping": date(2026, 10, 31)}, wb, types)
    assert out == {"日期列": ms(datetime(2026, 10, 31, tzinfo=BJ))}
    assert ff.build_writeback_fields({}, wb, types) == {}
    assert ff.build_writeback_fields({"amazon_order_no": "X"}, {"amazon_order_no": ""}, types) == {}
    assert ff.build_writeback_fields({"unknown_key": "X"}, {"unknown_key": "订单"}, types) == {}


def test_build_writeback_rejects_bad_targets():
    with pytest.raises(errors.DomainError) as ei:
        ff.build_writeback_fields({"total": Decimal("44.49")}, {"total": "总成本USD"}, {"总成本USD": ff.FORMULA})
    assert ei.value.code == "VALIDATION" and "公式" in ei.value.message
    with pytest.raises(errors.DomainError):
        ff.build_writeback_fields({"tax": "abc"}, {"tax": "税费USD"}, {"税费USD": ff.NUMBER})


def test_lookup_of_several_records_gives_several_values_not_a_glued_string():
    """D4-07：查找引用列引用了多条记录 → 多个值（平台订单号因此不可用、ASIN 判缺），不拼成 B0DEMO0001B0DEMO0002。"""
    v = {"type": 1, "value": [{"type": "text", "text": "B0DEMO0001"}, {"type": "text", "text": "B0DEMO0002"}]}
    assert ff.cell_to_python(v, ff.LOOKUP) == ["B0DEMO0001", "B0DEMO0002"]
    assert ff.cell_to_python({"type": 1, "value": [{"type": "text", "text": "PO-DEMO-1"}]}, ff.LOOKUP) == "PO-DEMO-1"
    rec = ff.normalize_record({"单号": v}, {"upstream_order_no": "单号"}, {"单号": ff.LOOKUP}, None)
    assert rec.upstream_order_no == "" and "多个值" in rec.issues[0]
