"""服务端护栏（契约 §4；v1 §10「48/52/50 美元」「结算金额等于限价、配送天数等于上限」验收条目）。纯函数，不连库。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from services import guard

TODAY = date(2026, 10, 30)          # 站点（洛杉矶）今天


def _facts(**over):
    base = {
        "items": [{"asin": "B0DEMO0001", "quantity": 1, "unit_price": "48.00"}],
        "subtotal": "48.00", "shipping": "0.00", "tax": "4.00", "total_before_tax": "48.00", "total": "52.00",
        "gift_card_amount": None, "currency": "USD",
        "delivery": {"text": "Arriving Nov 3", "date": (TODAY + timedelta(days=4)).isoformat(), "days": None},
        "ship_to": {"postal_code": "32960"}, "card_last4": "7860", "fulfillment": "amazon",
    }
    base.update(over)
    return base


def _eval(facts, **over):
    kw = dict(asin="B0DEMO0001", quantity=1, postal_code="32960", price_cap=Decimal("60.00"),
              amount_min=Decimal("20.00"), amount_max=Decimal("50.00"), max_delivery_days=10,
              expected_card_last4=None, site_today=TODAY)
    kw.update(over)
    return guard.evaluate(facts, **kw)


def test_all_checks_pass_and_result_shape():
    v = _eval(_facts())
    assert v["verdict"] == "pass" and v["code"] is None and v["reasons"] == []
    assert set(v["checks"]) == {"item", "zip", "subtotal", "price_cap", "delivery", "data", "card"}
    assert all(c["ok"] is True for c in v["checks"].values())
    assert v["delivery_days"] == 4 and v["total_for_cap"] == "52.00" and v["fulfillment"] == "amazon"
    assert v["checks"]["subtotal"]["expected"] == {"min": "20.00", "max": "50.00"}


def test_48_52_50_example_range_passes_but_total_over_cap_blocks():
    """商品小计 48、账号范围 20–50、结算 52、限价 50：账号匹配通过，最终付款校验拦截。"""
    v = _eval(_facts(), price_cap=Decimal("50.00"))
    assert v["checks"]["subtotal"]["ok"] is True
    assert v["checks"]["price_cap"] == {"ok": False, "expected": "50.00", "actual": "52.00"}
    assert v["verdict"] == "block" and v["code"] == "PRICE_OVER_CAP"
    assert "超过限价 $50.00" in v["reasons"][0]


@pytest.mark.parametrize("cap,verdict", [("52.00", "pass"), ("51.99", "block"), ("52.01", "pass")])
def test_total_equal_to_cap_passes_one_cent_over_blocks(cap, verdict):
    v = _eval(_facts(), price_cap=Decimal(cap))
    assert v["verdict"] == verdict
    if verdict == "block":
        assert v["code"] == "PRICE_OVER_CAP"


@pytest.mark.parametrize("sub,ok", [("20.00", True), ("50.00", True), ("19.99", False), ("50.01", False)])
def test_subtotal_range_endpoints_inclusive(sub, ok):
    v = _eval(_facts(subtotal=sub, total=sub))
    assert v["checks"]["subtotal"]["ok"] is ok
    assert (v["code"] is None) if ok else (v["code"] == "SUBTOTAL_OUT_OF_RANGE")


def test_no_max_amount_means_unlimited():
    v = _eval(_facts(subtotal="500.00", total="500.00"), amount_max=None, price_cap=Decimal("600"))
    assert v["verdict"] == "pass" and v["checks"]["subtotal"]["expected"]["max"] is None


@pytest.mark.parametrize("days,verdict", [(10, "pass"), (11, "block"), (0, "pass")])
def test_delivery_days_equal_to_max_passes_plus_one_blocks(days, verdict):
    v = _eval(_facts(delivery={"text": "x", "date": (TODAY + timedelta(days=days)).isoformat(), "days": None}))
    assert v["verdict"] == verdict and v["delivery_days"] == days
    if verdict == "block":
        assert v["code"] == "DELIVERY_TOO_LONG" and "超过配送上限 10 天" in v["reasons"][0]


def test_delivery_date_takes_precedence_and_days_used_without_date():
    v = _eval(_facts(delivery={"text": "x", "date": (TODAY + timedelta(days=3)).isoformat(), "days": 30}))
    assert v["delivery_days"] == 3 and v["verdict"] == "pass"
    v = _eval(_facts(delivery={"text": "x", "date": None, "days": 11}))
    assert v["delivery_days"] == 11 and v["code"] == "DELIVERY_TOO_LONG"
    v = _eval(_facts(delivery={"text": "x", "date": None, "days": "10"}))
    assert v["delivery_days"] == 10 and v["verdict"] == "pass"


@pytest.mark.parametrize("delivery", [
    {"text": "Arriving soon", "date": None, "days": None},
    {"text": None, "date": "not-a-date", "days": None},
    None,
    {"date": (TODAY - timedelta(days=1)).isoformat()},
])
def test_missing_or_unreliable_delivery_blocks_as_data_unreliable(delivery):
    """读不到配送天数不当 0 天放行。"""
    v = _eval(_facts(delivery=delivery))
    assert v["verdict"] == "block" and v["code"] == "DATA_UNRELIABLE"
    assert v["checks"]["delivery"]["ok"] is None and v["delivery_days"] is None
    assert v["checks"]["data"]["ok"] is False


@pytest.mark.parametrize("over", [
    {"total": None}, {"total": "abc"}, {"subtotal": None}, {"subtotal": ""}, {"total": "-1.00"},
    {"currency": "CAD"}, {"currency": None}, {"gift_card_amount": "lots"},
])
def test_unparseable_amounts_or_currency_block_as_data_unreliable(over):
    v = _eval(_facts(**over))
    assert v["verdict"] == "block" and v["code"] == "DATA_UNRELIABLE"
    assert v["reasons"] and v["reasons"][0].startswith("结算数据不可靠")


def test_unparseable_total_never_treated_as_zero():
    v = _eval(_facts(total=None), price_cap=Decimal("1.00"))
    assert v["checks"]["price_cap"]["ok"] is None and v["total_for_cap"] is None
    assert v["code"] == "DATA_UNRELIABLE"


def test_gift_card_amount_added_back_before_cap_comparison():
    # 结算页 total 已扣掉 10 美元礼品卡：42 + 10 = 52 > 50 → 拦截
    v = _eval(_facts(total="42.00", gift_card_amount="10.00"), price_cap=Decimal("50.00"))
    assert v["total_for_cap"] == "52.00" and v["code"] == "PRICE_OVER_CAP"
    assert "礼品卡抵扣 $10.00" in v["reasons"][0]
    # 负数写法也按绝对值加回，不会把总额算小
    v = _eval(_facts(total="42.00", gift_card_amount="-10.00"), price_cap=Decimal("50.00"))
    assert v["total_for_cap"] == "52.00" and v["code"] == "PRICE_OVER_CAP"
    v = _eval(_facts(total="40.00", gift_card_amount="10.00"), price_cap=Decimal("50.00"))
    assert v["total_for_cap"] == "50.00" and v["verdict"] == "pass"


def test_card_mismatch_only_when_expected_set():
    assert _eval(_facts(card_last4="1234"))["verdict"] == "pass"
    v = _eval(_facts(card_last4="1234"), expected_card_last4="7860")
    assert v["code"] == "CARD_MISMATCH" and v["checks"]["card"] == {"ok": False, "expected": "7860", "actual": "1234"}
    assert _eval(_facts(card_last4="•••• 7860"), expected_card_last4="7860")["verdict"] == "pass"
    assert _eval(_facts(card_last4=None), expected_card_last4="7860")["code"] == "CARD_MISMATCH"


@pytest.mark.parametrize("items", [
    [], None,
    [{"asin": "B0DEMO0001", "quantity": 1}, {"asin": "B0DEMO0002", "quantity": 1}],
    [{"asin": "B0DEMO0002", "quantity": 1}],
    [{"asin": "B0DEMO0001", "quantity": 2}],
    [{"asin": "B0DEMO0001", "quantity": "x"}],
])
def test_item_mismatch(items):
    v = _eval(_facts(items=items))
    assert v["code"] == "ITEM_MISMATCH" and v["checks"]["item"]["ok"] is False


def test_item_asin_case_insensitive_and_string_quantity():
    v = _eval(_facts(items=[{"asin": "b0demo0001", "quantity": "1"}]))
    assert v["checks"]["item"]["ok"] is True


@pytest.mark.parametrize("zip_, ok", [("32960", True), ("32960-1234", True), ("32961", False), (None, False)])
def test_zip_first_five_digits(zip_, ok):
    v = _eval(_facts(ship_to={"postal_code": zip_}))
    assert v["checks"]["zip"]["ok"] is ok
    if not ok:
        assert v["code"] == "ZIP_MISMATCH"


def test_code_is_first_failing_check_in_contract_order_and_reasons_list_all():
    v = _eval(_facts(items=[{"asin": "B0DEMO0009", "quantity": 1}], ship_to={"postal_code": "99999"},
                     total="99.00", currency="CAD"), price_cap=Decimal("50.00"))
    assert v["code"] == "ITEM_MISMATCH"
    assert len(v["reasons"]) == 4          # 商品、邮编、限价、数据可靠
    v = _eval(_facts(total="99.00", delivery=None), price_cap=Decimal("50.00"))
    assert v["code"] == "PRICE_OVER_CAP"   # 限价排在数据可靠之前


def test_fulfillment_recorded_only():
    v = _eval(_facts(fulfillment="third_party"))
    assert v["verdict"] == "pass" and v["fulfillment"] == "third_party"


def test_non_dict_facts_block():
    v = _eval("garbage")
    assert v["verdict"] == "block" and v["code"] == "ITEM_MISMATCH"


def test_missing_price_cap_blocks():
    v = _eval(_facts(), price_cap=None)
    assert v["code"] == "PRICE_OVER_CAP" and "没有有效限价" in v["reasons"][0]


def test_price_cap_reason_spacing_and_gift_card_wording():
    v = _eval(_facts(), price_cap=Decimal("50.00"))
    assert v["reasons"][0] == "结算总额 $52.00 超过限价 $50.00"
    v = _eval(_facts(total="42.00", gift_card_amount="10.00"), price_cap=Decimal("50.00"))
    assert v["reasons"][0] == "结算总额 $52.00（结算页 $42.00 + 礼品卡抵扣 $10.00）超过限价 $50.00"


@pytest.mark.parametrize("cap,verdict", [("52.009", "pass"), ("52.005", "pass"), ("51.999", "block")])
def test_price_cap_is_rounded_down_to_cents(cap, verdict):
    """限价按 money_cap 向下取两位：51.999 → 51.99（52.00 超出，拦截），52.009 → 52.00（相等放行）。"""
    v = _eval(_facts(), price_cap=Decimal(cap))
    assert v["verdict"] == verdict
    assert v["checks"]["price_cap"]["expected"] == f"{Decimal(cap).quantize(Decimal('0.01'), rounding='ROUND_DOWN')}"


# ── 规范化（入库前）─────────────────────────────────────────────────────────


@pytest.mark.parametrize("facts", [
    _facts(),
    _facts(card_last4="4111 1111 1111 1111"),
    _facts(card_last4={"pan": "4111111111111111"}),
    _facts(card_last4=7860),
    _facts(total=True), _facts(total=["52.00"]), _facts(total="5" * 19), _facts(total=float("nan")),
    _facts(total=float("inf")), _facts(total="  52.00  "), _facts(total=52),
    _facts(gift_card_amount={"x": 1}), _facts(gift_card_amount=False),
    _facts(currency={"code": "USD"}), _facts(currency="USD" + " " * 20 + "X"), _facts(currency=" usd "),
    _facts(items="B0DEMO0001"), _facts(items=[{"asin": "B0DEMO0001", "quantity": 1}] * 30),
    _facts(items=[["B0DEMO0001", 1]]), _facts(items=[{"asin": 123, "quantity": 1.0}]),
    _facts(items=[{"asin": "B0DEMO0001", "quantity": {"n": 1}}]),
    _facts(items=[{"asin": "B0DEMO0001" + " " * 15 + "Z", "quantity": 1}]),
    _facts(delivery="Nov 3"), _facts(delivery={"date": 20261103, "days": "4"}),
    _facts(delivery={"date": None, "days": True}), _facts(delivery={"text": "x" * 5000, "days": 4}),
    _facts(ship_to="32960"), _facts(ship_to={"postal_code": 32960}), _facts(ship_to={"postal_code": ["32960"]}),
    _facts(fulfillment={"by": "amazon"}),
    "garbage", None,
])
def test_sanitize_keeps_verdict(facts):
    raw = _eval(facts)
    clean = guard.sanitize_facts(facts)
    assert set(clean) == set(guard.FACT_KEYS)
    v = _eval(clean)
    assert (v["verdict"], v["code"]) == (raw["verdict"], raw["code"])
    assert v["checks"]["card"]["actual"] == raw["checks"]["card"]["actual"]


def test_sanitize_drops_unknown_keys_trims_card_and_text():
    clean = guard.sanitize_facts(_facts(card_last4="4111-1111-1111-1111", billing={"cvv": "123"},
                                        ship_to={"postal_code": "32960", "name": "Demo Buyer"},
                                        delivery={"text": "y" * 300, "date": "2026-11-03", "days": None, "x": 1},
                                        items=[{"asin": "B0DEMO0001", "quantity": 1, "unit_price": "48.00",
                                                "title": "DEMO"}]))
    assert "billing" not in clean and clean["card_last4"] == "1111"
    assert clean["ship_to"] == {"postal_code": "32960"}
    assert clean["delivery"] == {"text": "y" * 200, "date": "2026-11-03", "days": None}
    assert clean["items"] == [{"asin": "B0DEMO0001", "quantity": 1, "unit_price": "48.00"}]
    assert guard.sanitize_facts({"total": float("inf")})["total"] == guard.INVALID
    assert len(guard.sanitize_facts(_facts(items=[{}] * 50))["items"]) == 20


@pytest.mark.parametrize("total", ["5" * 30, 10 ** 40, 1e300, "9" * 25 + ".99"])
def test_absurd_amounts_never_crash_and_block_as_data_unreliable(total):
    """位数大到无法取两位小数的金额：不报 500，按格式不对拦截（规范化前后都一样）。"""
    for facts in (_facts(total=total), guard.sanitize_facts(_facts(total=total))):
        v = _eval(facts)
        assert v["verdict"] == "block" and v["code"] == "DATA_UNRELIABLE"


@pytest.mark.parametrize("total", ["1e3", "1,234.5,0", "12,50", "10000000000.00"])
def test_exponent_bad_commas_and_overflow_amounts_block_as_data_unreliable(total):
    """指数写法、逗号不是千分位、超出 numeric(12,2)：按读不出拦截（NULL 不是 0）。"""
    v = _eval(_facts(total=total))
    assert v["verdict"] == "block" and v["code"] == "DATA_UNRELIABLE"
    assert v["checks"]["price_cap"]["ok"] is None


def test_usd_renders_none_as_unknown_amount():
    assert guard._usd(None) == "未知金额" and guard._usd(Decimal("5")) == "$5.00"
