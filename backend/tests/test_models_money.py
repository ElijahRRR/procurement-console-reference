"""services/models 的金额换算（money / money_cap）：从不抛异常；读不出、指数写法、逗号不是千分位、
超出 numeric(12,2) 的值都是 None（NULL 不是 0）；money 四舍五入、money_cap 向下取整（都先抹 6 位小数的浮点噪声）。"""

from __future__ import annotations

import decimal
from decimal import Decimal

import pytest

from services.models import ERROR_CODES, MONEY_LIMIT, money, money_cap


@pytest.mark.parametrize("raw,expected", [
    ("25", "25.00"), ("25.5", "25.50"), (" 25.50 ", "25.50"), ("$25.50", "25.50"), ("US$25.50", "25.50"),
    ("$ 25.50", "25.50"), ("1,234.50", "1234.50"), ("$1,041.9", "1041.90"), ("1,234,567.891", "1234567.89"),
    ("-5", "-5.00"), ("+5", "5.00"), (".5", "0.50"), ("5.", "5.00"), (45, "45.00"), (Decimal("78.686"), "78.69"),
    (78.685, "78.69"), ("78.685", "78.69"), (78.68999999, "78.69"), (0.1 + 0.2, "0.30"), (Decimal("1E+3"), "1000.00"),
    ("9999999999.99", "9999999999.99"), (Decimal("-9999999999.99"), "-9999999999.99"),
])
def test_money_parses_and_rounds_half_up(raw, expected):
    assert money(raw) == Decimal(expected)


@pytest.mark.parametrize("raw,expected", [
    (78.685, "78.68"), ("78.685", "78.68"), (Decimal("78.686"), "78.68"), (78.68999999, "78.69"), ("51.999", "51.99"),
    ("52.009", "52.00"), ("$1,041.999", "1041.99"), (45, "45.00"), ("9999999999.999", "9999999999.99"),
])
def test_money_cap_rounds_down_after_noise_step(raw, expected):
    assert money_cap(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", [
    None, True, False, "", "   ", "$", "US", "abc", "12,5", "1.234,50", "12,34", "1,23,456", ",123", "1,2345.00",
    "1e3", "1E+3", "1e-2", "2.5e1", "NaN", "Infinity", "-inf", float("nan"), float("inf"), Decimal("NaN"),
    Decimal("-Infinity"), "1 234.50", "12.5.0", "--5", "5-", [19.5], {"v": 1}, object(),
])
def test_unreadable_values_are_none(raw):
    assert money(raw) is None and money_cap(raw) is None


@pytest.mark.parametrize("raw", [
    "5" * 30, "9" * 25 + ".99", 10 ** 40, -(10 ** 40), 1e300, 1e25, Decimal("1E+30"), Decimal("1E+999999"),
    "10000000000", "10000000000.00", 10 ** 10, Decimal("-10000000000"), "99999999999.00", "9,999,999,999,999.00",
    "9999999999.995",
])
def test_values_outside_numeric_12_2_are_none_and_never_raise(raw):
    """|金额| ≥ 10^10（含四舍五入后才进位到 10^10 的）放不进 numeric(12,2)：一律 None，不抛 InvalidOperation。"""
    assert money(raw) is None
    if raw != "9999999999.995":                       # 向下取整时它仍是 9999999999.99
        assert money_cap(raw) is None


def test_rounding_edge_into_limit_only_for_half_up():
    assert money("9999999999.995") is None
    assert money_cap("9999999999.995") == Decimal("9999999999.99")
    assert MONEY_LIMIT == Decimal(10) ** 10


def test_caller_decimal_context_does_not_matter():
    """换算用自己的上下文：调用方把精度调得很低（默认上下文下 quantize 会报 InvalidOperation）也照常换算。"""
    with decimal.localcontext() as ctx:
        ctx.prec = 4
        assert money("123456.785") == Decimal("123456.79")
        assert money_cap(Decimal("98765.4321")) == Decimal("98765.43")
        assert money("5" * 30) is None


def test_tiny_values_round_to_zero_not_none():
    assert money(Decimal("1E-30")) == Decimal("0.00")
    assert money("0.004") == Decimal("0.00") and money("0.005") == Decimal("0.01")
    assert money_cap("0.009") == Decimal("0.00")


def test_aborted_by_server_is_an_abort_code():
    ec = ERROR_CODES["ABORTED_BY_SERVER"]
    assert ec.category == "abort" and "未提交前中止" in ec.zh
