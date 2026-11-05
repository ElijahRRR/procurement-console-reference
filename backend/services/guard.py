"""服务端护栏（契约 §4）：插件在结算页、点击下单前上报读到的事实，由这里逐项判定放行或拦截。

纯函数，不碰数据库、不读时钟：任务与账号的有效值（有效 ASIN、数量、限价、金额区间、有效配送上限、
期望卡尾号）和站点今天由调用方（services/execution.submit_guard）按**最新已确认配置**取好后传进来。

判定顺序与拦截码（全部通过才放行；拦截码取顺序中第一个不通过的检查）：

|检查|规则|拦截码|
|---|---|---|
|item|items 恰好一项，ASIN 与有效 ASIN 相同，数量与有效数量相同|ITEM_MISMATCH|
|zip|ship_to.postal_code 前 5 位与任务邮编前 5 位相同|ZIP_MISMATCH|
|subtotal|商品小计在账号区间内（端点含；上限为空 = 不限）|SUBTOTAL_OUT_OF_RANGE|
|price_cap|total + 礼品卡抵扣额 ≤ 有效限价（相等放行）|PRICE_OVER_CAP|
|delivery|配送天数 = delivery.date − 站点今天（无日期时用 delivery.days）≤ 有效配送上限（相等放行）|DELIVERY_TOO_LONG|
|data|total、subtotal 可解析，配送天数可得，币种为 USD|DATA_UNRELIABLE|
|card|账号设置了期望卡尾号时必须相同|CARD_MISMATCH|

要点：

- **NULL 不是 0**：读不到的小计、总额、配送天数不会被当作 0 放行。依赖它们的检查记为「无法判定」
  （ok=None，不单独拦截），由 data 检查统一拦截为 DATA_UNRELIABLE。
- 礼品卡：结算页显示的 total 已扣掉礼品卡，限价比的是「本单真实价格」，所以把抵扣额加回去
  （按绝对值加回，插件无论报正数还是负数都不会把总额算小）。gift_card_amount 为 null 表示没有礼品卡。
- 配送日期早于站点今天视为读取不可靠（不当作 0 天放行）。
- fulfillment（是否亚马逊配送）只记录，不拦截。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from services.models import money

#: 检查名与拦截码，顺序即契约 §4 的判定顺序
CHECK_ORDER: tuple[tuple[str, str], ...] = (
    ("item", "ITEM_MISMATCH"),
    ("zip", "ZIP_MISMATCH"),
    ("subtotal", "SUBTOTAL_OUT_OF_RANGE"),
    ("price_cap", "PRICE_OVER_CAP"),
    ("delivery", "DELIVERY_TOO_LONG"),
    ("data", "DATA_UNRELIABLE"),
    ("card", "CARD_MISMATCH"),
)

_ABSENT = object()


def _usd(v: Decimal | None) -> str:
    return "—" if v is None else f"${v:.2f}"


def _s(v: Decimal | None) -> str | None:
    return None if v is None else f"{v:.2f}"


def _amount(v: Any, *, signed: bool = False) -> Any:
    """输入：上报的金额（字符串 / 数字 / None）、是否允许负数 → 输出：Decimal；None / 空串 → _ABSENT；
    格式不对（或不允许时为负）→ None。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        return _ABSENT
    if isinstance(v, bool) or not isinstance(v, (str, int, float, Decimal)):
        return None
    d = money(v)
    if d is None or (d < 0 and not signed):
        return None
    return d


def _int(v: Any) -> int | None:
    """输入：上报的整数（int / 数字字符串 / 整数值的 float）→ 输出：int；其他 → None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _date(v: Any) -> date | None:
    """输入：'YYYY-MM-DD'（可带时刻）或 date → 输出：date；读不出 → None。"""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and len(v.strip()) >= 10:
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def _zip5(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:5] if s else None


def _text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _card4(v: Any) -> str | None:
    if v is None:
        return None
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else (digits or None)


def evaluate(facts: dict[str, Any], *, asin: str | None, quantity: int | None, postal_code: str | None,
             price_cap: Decimal | None, amount_min: Decimal, amount_max: Decimal | None,
             max_delivery_days: int | None, expected_card_last4: str | None, site_today: date) -> dict[str, Any]:
    """输入：结算页事实、任务与账号的有效值、站点今天 → 输出：护栏裁决
    {verdict: 'pass'|'block', code, reasons, checks: {检查名: {ok, expected, actual}}, delivery_days, total_for_cap,
    fulfillment}。

    checks 里 ok 为 True（通过）/ False（不通过）/ None（缺数据无法判定，由 data 检查拦截）；
    金额一律两位小数字符串。code 为第一个不通过检查的拦截码（放行时为 None）。
    """
    if not isinstance(facts, dict):
        facts = {}
    checks: dict[str, dict[str, Any]] = {}
    reasons: dict[str, str] = {}
    exp_asin = asin.strip().upper() if isinstance(asin, str) and asin.strip() else None

    # ── 商品 ──
    items = facts.get("items")
    exp_item = {"asin": exp_asin, "quantity": quantity}
    if isinstance(items, list):
        actual_items = [{"asin": _text(it.get("asin")).upper() if isinstance(it, dict) and _text(it.get("asin"))
                         else None,
                         "quantity": _int(it.get("quantity")) if isinstance(it, dict) else None} for it in items]
    else:
        actual_items = None
    if actual_items is None or len(actual_items) != 1:
        n = 0 if actual_items is None else len(actual_items)
        checks["item"] = {"ok": False, "expected": exp_item, "actual": actual_items}
        reasons["item"] = f"结算页应只有 1 种商品，实际读到 {n} 种"
    else:
        got = actual_items[0]
        ok = exp_asin is not None and quantity is not None and got["asin"] == exp_asin and got["quantity"] == quantity
        checks["item"] = {"ok": ok, "expected": exp_item, "actual": got}
        if not ok:
            reasons["item"] = (f"结算商品与任务不一致：任务 {exp_asin or '—'} × {quantity if quantity is not None else '—'}，"
                               f"结算页 {got['asin'] or '—'} × {got['quantity'] if got['quantity'] is not None else '—'}")

    # ── 邮编 ──
    ship_to = facts.get("ship_to") if isinstance(facts.get("ship_to"), dict) else {}
    exp_zip, act_zip = _zip5(postal_code), _zip5(ship_to.get("postal_code"))
    zip_ok = exp_zip is not None and act_zip is not None and exp_zip == act_zip
    checks["zip"] = {"ok": zip_ok, "expected": exp_zip, "actual": act_zip}
    if not zip_ok:
        reasons["zip"] = f"收货邮编 {act_zip or '读取不到'} 与任务邮编 {exp_zip or '—'} 不一致"

    # ── 数据可读性（先解析，后面几项依赖它）──
    problems: list[str] = []
    sub = _amount(facts.get("subtotal"))
    total = _amount(facts.get("total"))
    gift = _amount(facts.get("gift_card_amount"), signed=True)
    if sub is _ABSENT or sub is None:
        problems.append("商品小计读取不到或格式不对")
        sub = None
    if total is _ABSENT or total is None:
        problems.append("结算总额读取不到或格式不对")
        total = None
    if gift is None:
        problems.append("礼品卡金额格式不对")
        gift_ok = False
        gift = None
    else:
        gift_ok = True
        gift = None if gift is _ABSENT else gift

    # ── 金额区间 ──
    exp_range = {"min": _s(amount_min), "max": _s(amount_max)}
    if sub is None:
        checks["subtotal"] = {"ok": None, "expected": exp_range, "actual": None}
    else:
        ok = sub >= amount_min and (amount_max is None or sub <= amount_max)
        checks["subtotal"] = {"ok": ok, "expected": exp_range, "actual": _s(sub)}
        if not ok:
            reasons["subtotal"] = (f"商品小计 {_usd(sub)} 不在账号金额区间 {_usd(amount_min)}–"
                                   f"{_usd(amount_max) if amount_max is not None else '不限'} 内")

    # ── 限价 ──
    total_for_cap: Decimal | None = None
    if total is not None and gift_ok:
        total_for_cap = (total + abs(gift or Decimal("0"))).quantize(Decimal("0.01"))
    if total_for_cap is None:
        checks["price_cap"] = {"ok": None, "expected": _s(price_cap), "actual": None}
    elif price_cap is None:
        checks["price_cap"] = {"ok": False, "expected": None, "actual": _s(total_for_cap)}
        reasons["price_cap"] = "任务没有有效限价，不能放行"
    else:
        ok = total_for_cap <= price_cap
        checks["price_cap"] = {"ok": ok, "expected": _s(price_cap), "actual": _s(total_for_cap)}
        if not ok:
            extra = f"（结算页 {_usd(total)} + 礼品卡抵扣 {_usd(abs(gift))}）" if gift else ""
            reasons["price_cap"] = f"结算总额 {_usd(total_for_cap)}{extra}超过限价 {_usd(price_cap)}"

    # ── 配送时长 ──
    delivery = facts.get("delivery") if isinstance(facts.get("delivery"), dict) else {}
    d = _date(delivery.get("date"))
    days = (d - site_today).days if d is not None else _int(delivery.get("days"))
    if days is not None and days < 0:
        days = None
        problems.append("预计送达日期早于今天，读取不可靠")
    elif days is None:
        problems.append("读取不到预计送达日期或天数")
    if days is None:
        checks["delivery"] = {"ok": None, "expected": max_delivery_days, "actual": None}
    elif max_delivery_days is None:
        checks["delivery"] = {"ok": False, "expected": None, "actual": days}
        reasons["delivery"] = "没有有效的配送上限，不能放行"
    else:
        ok = days <= max_delivery_days
        checks["delivery"] = {"ok": ok, "expected": max_delivery_days, "actual": days}
        if not ok:
            reasons["delivery"] = f"预计 {days} 天送达，超过配送上限 {max_delivery_days} 天"

    # ── 数据可靠 ──
    currency = _text(facts.get("currency"))
    if currency is None or currency.upper() != "USD":
        problems.append(f"币种不是 USD（{currency or '读取不到'}）")
    checks["data"] = {"ok": not problems, "expected": "USD；小计、总额可解析；配送天数可得", "actual": problems}
    if problems:
        reasons["data"] = "结算数据不可靠：" + "；".join(problems)

    # ── 卡尾号 ──
    act_card = _card4(facts.get("card_last4"))
    if expected_card_last4:
        ok = act_card == expected_card_last4
        checks["card"] = {"ok": ok, "expected": expected_card_last4, "actual": act_card}
        if not ok:
            reasons["card"] = f"支付卡尾号 {act_card or '读取不到'} 与账号设置的 {expected_card_last4} 不符"
    else:
        checks["card"] = {"ok": True, "expected": None, "actual": act_card}

    passed = all(checks[name]["ok"] is True for name, _ in CHECK_ORDER)
    code = None
    if not passed:
        code = next((c for name, c in CHECK_ORDER if checks[name]["ok"] is False), "DATA_UNRELIABLE")
    return {
        "verdict": "pass" if passed else "block",
        "code": code,
        "reasons": [reasons[name] for name, _ in CHECK_ORDER if name in reasons],
        "checks": checks,
        "delivery_days": days,
        "total_for_cap": _s(total_for_cap),
        "fulfillment": _text(facts.get("fulfillment")),
    }
