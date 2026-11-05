"""services/normalize：类型转换（NULL 不是 0）、北京时间解析、人审与取消判定、新建跳过原因。"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from services.models import UpstreamRecord
from services.normalize import (
    DEFAULT_ADMISSION, ISSUE_NO_ORDER_NO, SYSTEM_FIELD_KEYS, SYSTEM_FIELDS, admission_skip_reason,
    effective_admission, normalize, parse_datetime_text,
)

UTC = timezone.utc
BJ = ZoneInfo("Asia/Shanghai")


def rec(**values) -> UpstreamRecord:
    values.setdefault("upstream_order_no", "PO-DEMO-1")
    return normalize(values)


# ── 字段清单 ─────────────────────────────────────────────────────────────────


def test_system_fields_cover_every_upstream_input_field():
    derived = {"review", "upstream_cancelled", "issues"}
    record_fields = [f.name for f in dataclasses.fields(UpstreamRecord) if f.name not in derived]
    assert sorted(SYSTEM_FIELD_KEYS) == sorted(record_fields)
    assert len(set(SYSTEM_FIELD_KEYS)) == len(SYSTEM_FIELD_KEYS)
    required = [f["key"] for f in SYSTEM_FIELDS if f["required"]]
    assert required == ["upstream_order_no"]
    assert {f["kind"] for f in SYSTEM_FIELDS} <= {"text", "int", "money", "datetime", "date", "enum"}
    for f in SYSTEM_FIELDS:
        assert set(f) == {"key", "label", "required", "kind"} and f["label"]


# ── NULL 不是 0 ──────────────────────────────────────────────────────────────


def test_blank_values_stay_none_without_issues():
    r = rec(quantity="", unit_price="  ", price_cap=None, ship_days=[], order_time="", suggested_purchase_date=None,
            asin=" ", postal_code="", recipient_name="   ")
    assert r.quantity is None and r.unit_price is None and r.price_cap is None and r.ship_days is None
    assert r.order_time is None and r.suggested_purchase_date is None and r.asin is None
    assert r.postal_code is None and r.recipient_name is None
    assert r.issues == []


def test_explicit_zero_is_kept_where_allowed():
    r = rec(ship_days=0, price_cap="0", unit_price=0)
    assert r.ship_days == 0 and r.price_cap == Decimal("0.00") and r.unit_price == Decimal("0.00")
    assert r.issues == []


def test_missing_order_no_is_flagged_first():
    r = normalize({"asin": "bad"})
    assert r.upstream_order_no == ""
    assert r.issues[0] == ISSUE_NO_ORDER_NO
    assert normalize({"upstream_order_no": "   "}).issues == [ISSUE_NO_ORDER_NO]


def test_text_trimmed_and_numeric_cells_become_integer_text():
    r = rec(upstream_order_no=1234567890.0, recipient_name="  Demo Buyer  ", sku=12345, phone=5550100123.0,
            store_name=["STORE-DEMO"], ship_method=["FBA", "FBM"])
    assert r.upstream_order_no == "1234567890"
    assert r.recipient_name == "Demo Buyer"
    assert r.sku == "12345" and r.phone == "5550100123"
    assert r.store_name == "STORE-DEMO" and r.ship_method == "FBA, FBM"


def test_unknown_keys_ignored():
    r = normalize({"upstream_order_no": "PO-DEMO-2", "not_a_field": 1})
    assert r.upstream_order_no == "PO-DEMO-2" and r.issues == []


# ── ASIN / 数量 / 金额 / 天数 ─────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [("B0DEMO0001", "B0DEMO0001"), (" b0demo0001 ", "B0DEMO0001"),
                                          ("b0 demo 0001", "B0DEMO0001"), ("1234567890", "1234567890")])
def test_asin_ok(raw, expected):
    r = rec(asin=raw)
    assert r.asin == expected and r.issues == []


@pytest.mark.parametrize("raw", ["B0DEMO", "B0DEMO00011", "B0-DEMO-001", "商品码B0DEMO"])
def test_asin_invalid_is_none_with_issue(raw):
    r = rec(asin=raw)
    assert r.asin is None
    assert len(r.issues) == 1 and r.issues[0].startswith("ASIN 格式不对")


@pytest.mark.parametrize("raw,expected", [(1, 1), ("2", 2), (3.0, 3), ("4.0", 4), (" 5 ", 5), ("6件", 6),
                                          ([2], 2), (Decimal("7"), 7)])
def test_quantity_ok(raw, expected):
    r = rec(quantity=raw)
    assert r.quantity == expected and r.issues == []


@pytest.mark.parametrize("raw", [0, "0", -1, 2.5, "two", True, [1, 2]])
def test_quantity_must_be_positive_int(raw):
    r = rec(quantity=raw)
    assert r.quantity is None
    assert len(r.issues) == 1 and r.issues[0].startswith("数量不是正整数")


@pytest.mark.parametrize("raw,expected", [(4, 4), ("4", 4), ("4天", 4), (0, 0), (3.0, 3)])
def test_ship_days_ok(raw, expected):
    r = rec(ship_days=raw)
    assert r.ship_days == expected and r.issues == []


@pytest.mark.parametrize("raw", [-1, "1.5", "很快"])
def test_ship_days_invalid(raw):
    r = rec(ship_days=raw)
    assert r.ship_days is None and r.issues[0].startswith("配送天数不是非负整数")


@pytest.mark.parametrize("raw,expected", [
    (41.97, "41.97"), ("41.97", "41.97"), ("$1,041.9", "1041.90"), ("USD 52", "52.00"), ("52 usd", "52.00"),
    (Decimal("78.686"), "78.69"), (78.685, "78.69"), (45, "45.00"), ([19.5], "19.50"),
])
def test_money_ok(raw, expected):
    """普通金额四舍五入到分（采购单价、销售总额）。"""
    r = rec(unit_price=raw)
    assert r.unit_price == Decimal(expected) and r.issues == []


@pytest.mark.parametrize("raw,expected", [
    (78.685, "78.68"), ("78.685", "78.68"), (Decimal("78.686"), "78.68"), (78.69, "78.69"), (78.68999999, "78.69"),
    ("$1,041.9", "1041.90"), (45, "45.00"),
])
def test_price_cap_rounds_down_after_float_noise_removal(raw, expected):
    """限价是上限：先抹浮点噪声，再向下取整到分（宁可少放行 1 美分）。"""
    r = rec(price_cap=raw)
    assert r.price_cap == Decimal(expected) and r.issues == []


@pytest.mark.parametrize("raw", ["12,5", "1.234,50", "1e3", "1E+25", "1e30", "99999999999999999999999", 1e25,
                                 Decimal("1E+30"), "12,34", "1,23,456"])
def test_unreliable_or_absurd_money_is_none_with_issue(raw):
    """逗号只能是千分位；指数写法不认；超出 numeric(12,2) 的数一律当没读到（不抛异常、不当 0）。"""
    r = rec(price_cap=raw, unit_price=raw)
    assert r.price_cap is None and r.unit_price is None
    assert any(i.startswith("限价不是有效金额") for i in r.issues)
    assert any(i.startswith("采购单价不是有效金额") for i in r.issues)


@pytest.mark.parametrize("raw,prefix", [("abc", "限价不是有效金额"), (True, "限价不是有效金额"),
                                        ("-5", "限价不能为负数"), ([1, 2], "限价不是有效金额")])
def test_money_invalid(raw, prefix):
    r = rec(price_cap=raw)
    assert r.price_cap is None and r.issues[0].startswith(prefix)


# ── 时刻与日期（北京时间）─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [
    "2026/10/27 21:31", "2026-10-27 21:31:00", "2026-10-27T21:31:00", "2026/10/27 21:31:00", "2026.10.27 21:31",
    "2026年10月27日 21:31", "20261027 2131", datetime(2026, 10, 27, 21, 31),
    datetime(2026, 10, 27, 13, 31, tzinfo=UTC), "2026-10-27T13:31:00Z", "2026-10-27T13:31:00+00:00",
    "2026-10-27 21:31:00+08:00", int(datetime(2026, 10, 27, 13, 31, tzinfo=UTC).timestamp() * 1000),
    str(int(datetime(2026, 10, 27, 13, 31, tzinfo=UTC).timestamp() * 1000)),
])
def test_order_time_beijing(raw):
    r = rec(order_time=raw)
    assert r.issues == []
    assert r.order_time == datetime(2026, 10, 27, 13, 31, tzinfo=UTC)
    assert r.order_time.tzinfo is not None


def test_order_time_date_only_is_beijing_midnight():
    for raw in ("2026-10-27", "2026/10/27", date(2026, 10, 27)):
        r = rec(order_time=raw)
        assert r.order_time == datetime(2026, 10, 26, 16, 0, tzinfo=UTC), raw


def test_beijing_late_evening_crosses_utc_date():
    # 北京 10-28 00:30 = UTC 10-27 16:30：日期以北京为准由下游 clock.bj_date 计算，这里只保证时刻正确
    r = rec(order_time="2026-10-28 00:30")
    assert r.order_time == datetime(2026, 10, 27, 16, 30, tzinfo=UTC)
    assert r.order_time.astimezone(BJ).date() == date(2026, 10, 28)


@pytest.mark.parametrize("raw", ["昨天", "2026-13-01", "10/27/2026", "2026-02-30 10:00", True])
def test_order_time_invalid(raw):
    r = rec(order_time=raw)
    assert r.order_time is None and r.issues[0].startswith("下单时间无法识别")


def test_suggested_date_variants():
    serial = (date(2026, 10, 31) - date(1899, 12, 30)).days
    feishu_ms = int(datetime(2026, 10, 31, tzinfo=BJ).timestamp() * 1000)   # 飞书日期列：北京当天 00:00
    for raw in ("2026-10-31", "2026/10/31", "2026/10/31 00:00", date(2026, 10, 31), datetime(2026, 10, 31),
                datetime(2026, 10, 30, 16, 0, tzinfo=UTC), feishu_ms, serial, float(serial)):
        r = rec(suggested_purchase_date=raw)
        assert r.suggested_purchase_date == date(2026, 10, 31), raw
        assert r.issues == []


def test_suggested_date_invalid():
    r = rec(suggested_purchase_date="下周")
    assert r.suggested_purchase_date is None and r.issues[0].startswith("建议采购日期无法识别")


def test_parse_datetime_text_helper():
    assert parse_datetime_text("2026/10/27 21:31") == datetime(2026, 10, 27, 13, 31, tzinfo=UTC)
    assert parse_datetime_text("nonsense") is None


# ── 收件信息 ─────────────────────────────────────────────────────────────────


def test_postal_code_keeps_leading_zero_and_flags_short():
    assert rec(postal_code="02134").postal_code == "02134"
    assert rec(postal_code="32960-1234").issues == []
    assert rec(postal_code=32960.0).postal_code == "32960"
    r = rec(postal_code="2134")
    assert r.postal_code == "2134"
    assert r.issues == ["邮编不足 5 位：2134"]


def test_numeric_us_zip_gets_leading_zero_back():
    """Excel 数字格式 / 飞书数字列把 02134 变成 2134：美国地址补回前导 0 并记问题；非美国不补。"""
    r = rec(postal_code=2134)
    assert r.postal_code == "02134" and r.issues == ["邮编 2134 是数字格式，已补前导 0 为 02134"]
    assert rec(postal_code=501.0).postal_code == "00501"
    assert rec(postal_code=2134, country="CA").postal_code == "2134"
    assert rec(postal_code=12).postal_code == "12"


def test_country_and_state():
    assert rec().country == "US"
    assert rec(country="usa").country == "US"
    assert rec(country="美国").country == "US"
    assert rec(country="United States").country == "US"
    assert rec(country="ca").country == "CA"
    assert rec(state="fl").state == "FL"
    assert rec(state="Florida").state == "Florida"


# ── 人审与取消（准入规则）────────────────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [
    ("通过", "approved"), (" 通过 ", "approved"), ("通过了", "unknown"), ("拒绝", "rejected"),
    ("建议拒绝", "rejected"), ("钓鱼", "rejected"), (None, "unknown"), ("", "unknown"), ("待审", "unknown"),
])
def test_review_default_admission(value, expected):
    assert rec(review_value=value).review == expected


def test_review_custom_admission():
    adm = {"approved_values": ["OK", " 可采 "], "rejected_values": ["NO"]}
    assert normalize({"upstream_order_no": "P", "review_value": "可采"}, adm).review == "approved"
    assert normalize({"upstream_order_no": "P", "review_value": "通过"}, adm).review == "unknown"
    assert normalize({"upstream_order_no": "P", "review_value": "NO"}, adm).review == "rejected"
    assert normalize({"upstream_order_no": "P", "review_value": "通过"}, {"approved_values": []}).review == "unknown"


def test_upstream_cancelled_from_after_sale_values():
    assert rec(after_sale_value="已取消").upstream_cancelled is False          # 缺省不认任何取消取值
    adm = {"cancel_after_sale_values": ["已取消", "退款"]}
    assert normalize({"upstream_order_no": "P", "after_sale_value": " 已取消 "}, adm).upstream_cancelled is True
    assert normalize({"upstream_order_no": "P", "after_sale_value": "已取消订单"}, adm).upstream_cancelled is False
    assert normalize({"upstream_order_no": "P"}, adm).upstream_cancelled is False


def test_effective_admission_defaults_and_overrides():
    assert effective_admission(None) == DEFAULT_ADMISSION
    adm = effective_admission({"purchaser_values": "自采购DEMO", "approved_values": [" 通过 ", ""],
                               "import_historical": 1, "rejected_values": None})
    assert adm["purchaser_values"] == ["自采购DEMO"]
    assert adm["approved_values"] == ["通过"]
    assert adm["rejected_values"] == DEFAULT_ADMISSION["rejected_values"]
    assert adm["import_historical"] is True
    assert effective_admission({"skip_purchase_status_values": []})["skip_purchase_status_values"] == []


# ── 新建跳过原因 ─────────────────────────────────────────────────────────────


def skip(admission=None, **values):
    values.setdefault("upstream_order_no", "PO-DEMO-9")
    return admission_skip_reason(normalize(values, admission), admission)


def test_skip_none_for_normal_rows_even_if_not_approved():
    assert skip(review_value="通过", purchase_status_value="待拍单") is None
    assert skip(review_value="拒绝") is None          # 新建后停在「待确认」，不在这里挡
    assert skip() is None


def test_skip_purchaser_filter():
    adm = {"purchaser_values": ["自采购DEMO"]}
    assert skip(adm, purchaser="自采购DEMO") is None
    assert "采购方「其他采购DEMO」" in skip(adm, purchaser="其他采购DEMO")
    assert "采购方「（空）」" in skip(adm)
    assert skip(None, purchaser="任何人") is None    # 列表为空不过滤


def test_skip_upstream_cancelled():
    adm = {"cancel_after_sale_values": ["已取消"]}
    assert "上游已取消" in skip(adm, after_sale_value="已取消")


def test_skip_historical_matrix():
    amz = "111-0000000-0000001"
    # 有 AMZ 订单号：只看 import_historical
    assert "历史采购" in skip(None, external_amz_order_no=amz)
    assert "历史采购" in skip(None, external_amz_order_no=amz, purchase_status_value="已拍单")
    assert skip({"import_historical": True}, external_amz_order_no=amz) is None
    assert skip({"import_historical": True}, external_amz_order_no=amz, purchase_status_value="已拍单") is None
    # 没有 AMZ 订单号但采购状态为「已拍单」「拿回」：一律跳过，导入历史也不能新建
    for status in ("已拍单", "拿回", " 已拍单 "):
        assert "没有 AMZ 订单号" in skip(None, purchase_status_value=status)
        assert "没有 AMZ 订单号" in skip({"import_historical": True}, purchase_status_value=status)
    # 跳过清单可配置
    assert skip({"skip_purchase_status_values": []}, purchase_status_value="已拍单") is None
    assert skip({"skip_purchase_status_values": ["拍单异常"]}, purchase_status_value="拍单异常") is not None


def test_skip_order_purchaser_before_history():
    adm = {"purchaser_values": ["自采购DEMO"], "import_historical": True}
    reason = skip(adm, purchaser="别人DEMO", external_amz_order_no="111-0000000-0000002")
    assert reason.startswith("采购方")


def test_normalized_record_roundtrips_json():
    r = normalize({"upstream_order_no": "PO-DEMO-1", "asin": "b0demo0001", "quantity": "1", "unit_price": "41.97",
                   "price_cap": 78.69, "order_time": "2026/10/27 21:31", "suggested_purchase_date": "2026-10-31",
                   "ship_days": 4, "review_value": "通过", "postal_code": "02134"})
    back = UpstreamRecord.from_json(r.to_json())
    assert back == r
    assert back.order_time - datetime(2026, 10, 27, 13, 31, tzinfo=UTC) == timedelta(0)


def test_compact_numeric_date_and_scalar_admission_values():
    assert rec(suggested_purchase_date=20261031).suggested_purchase_date == date(2026, 10, 31)
    assert rec(suggested_purchase_date="20261031").suggested_purchase_date == date(2026, 10, 31)
    assert effective_admission({"approved_values": 1})["approved_values"] == ["1"]


# ── 多选、表格错误值、不可用的平台订单号、离谱的天数与年份、AMZ 订单号（D4-12 / D4-07 / D4-02 / D4-04）────────


def test_multi_select_values_match_admission_by_membership():
    adm = {"cancel_after_sale_values": ["已取消"]}
    assert normalize({"upstream_order_no": "P", "after_sale_value": ["已取消", "退款中"]}, adm).upstream_cancelled
    assert not normalize({"upstream_order_no": "P", "after_sale_value": ["退款中"]}, adm).upstream_cancelled
    assert normalize({"upstream_order_no": "P", "review_value": ["通过", "钓鱼"]}).review == "rejected"
    assert normalize({"upstream_order_no": "P", "review_value": ["通过", "待定"]}).review == "unknown"
    assert normalize({"upstream_order_no": "P", "review_value": ["通过", "通过"]}).review == "approved"


def test_multi_select_purchase_status_hits_skip_values():
    from services.normalize import admission_hold, admission_skip_reason

    r = normalize({"upstream_order_no": "P", "purchase_status_value": ["待拍单", "已拍单"], "review_value": "通过"})
    assert "已拍单" in admission_skip_reason(r, None)
    assert admission_hold(r, None)["kind"] == "purchase_status"


@pytest.mark.parametrize("raw,needle", [
    ("#N/A", "表格错误值"), ("#REF!", "表格错误值"), (["PO-DEMO-1", "PO-DEMO-2"], "多个值"),
    ("1.23457E+12", "科学计数法"),
])
def test_unusable_order_numbers_are_invalid_with_reason(raw, needle):
    r = normalize({"upstream_order_no": raw})
    assert r.upstream_order_no == "" and needle in r.issues[0]


def test_same_order_number_twice_in_a_list_is_one_value():
    assert normalize({"upstream_order_no": ["PO-DEMO-1", " PO-DEMO-1 "]}).upstream_order_no == "PO-DEMO-1"


def test_excel_error_values_in_text_fields_count_as_missing():
    r = normalize({"upstream_order_no": "P", "recipient_name": "#N/A", "city": "#VALUE!"})
    assert r.recipient_name is None and r.city is None
    assert sum("表格错误值" in i for i in r.issues) == 2


@pytest.mark.parametrize("field,raw,needle", [
    ("ship_days", 20261031, "配送天数超出合理范围"), ("ship_days", 61, "配送天数超出合理范围"),
    ("order_time", "9999-12-31", "下单时间年份不合理"), ("suggested_purchase_date", "1900-01-01", "建议采购日期年份不合理"),
])
def test_absurd_days_and_years_are_none_with_issue(field, raw, needle):
    r = normalize({"upstream_order_no": "P", field: raw})
    assert getattr(r, field) is None and any(needle in i for i in r.issues)
    assert normalize({"upstream_order_no": "P", "ship_days": 60}).ship_days == 60


@pytest.mark.parametrize("raw", ["—", "-", "无", "N/A", "null", "0"])
def test_placeholder_amz_numbers_become_none(raw):
    r = normalize({"upstream_order_no": "P", "external_amz_order_no": raw})
    assert r.external_amz_order_no is None and any("不是订单号" in i for i in r.issues)


def test_malformed_amz_number_is_kept_but_flagged_and_not_historical():
    from services.normalize import admission_skip_reason, amazon_order_no_ok

    r = normalize({"upstream_order_no": "P", "external_amz_order_no": "缺货待补", "review_value": "通过"})
    assert r.external_amz_order_no == "缺货待补" and any("格式不对" in i for i in r.issues)
    assert admission_skip_reason(r, None) is None            # 不当历史采购跳过：交给 ingest 建成待核实
    assert amazon_order_no_ok("111-0000000-0000001") and not amazon_order_no_ok("111-000-1")
