"""上游记录规范化：把「系统字段 → Python 值」转成 models.UpstreamRecord，并给出准入判断。

飞书记录（services/feishu_fields）与文件行（services/file_mapping）都先各自取出
「系统字段 → 原值」，再交给这里的 normalize()。这里只做类型转换与准入规则：

- 转换失败不抛异常：值置 None 并把问题写进 record.issues（「ASIN 格式不对」「数量不是正整数」等）；
- NULL 不是 0：空串、空白、空列表一律视为 None，绝不补 0；
- 文本时刻（如 ``2026/10/27 21:31``）与不带时区的单元格时刻按北京时间解释；带时区的时刻原样保留；
- 人审结论与上游取消由准入规则（admission，契约 §6）判定，取值比较为去首尾空白后的完全相等；多选值逐项比较
  （任一项是取消值即取消；任一项是拒绝值即拒绝；全部是通过值才算通过）；
- 表格错误值（#N/A、#REF! 等）当作没读到并记问题；平台订单号是错误值、多个值或科学计数法文本 → 不可用（invalid）；
- 金额只认「1234.5」「$1,234.50」这类写法（逗号只能是千分位），绝对值须小于 10^10（numeric(12,2)）；限价向下取整到分；
- 配送天数 0–60、年份 2000–2100 之外视为不可靠（None + 问题），不会让日期计算溢出；
- 美国邮编：数字单元格的 3–4 位数补前导 0（并记问题）；文本不足 5 位只记问题（由 orders 判为资料待补齐）；
- AMZ 订单号：明显的占位写法（「—」「无」「N/A」等）当作没有并记问题；其他不符合「3-7-7 位数字」格式的文本原样保留，
  由 orders 判为待核实（不当作可信单号）。

本模块不碰数据库，也不计算建议采购日期（由 services/orders 按「下单北京日期 + 6 − 配送天数」补算）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from services import clock
from services.models import UpstreamRecord, money, money_cap

# ── 系统字段清单 ──────────────────────────────────────────────────────────────
# kind：text 文本 / int 整数 / money 美元金额 / datetime 时刻 / date 日期 / enum 来源取值原文（供准入规则匹配）

SYSTEM_FIELDS: list[dict[str, Any]] = [
    {"key": "upstream_order_no", "label": "平台订单号", "required": True, "kind": "text"},
    {"key": "order_line_id", "label": "订单行 ID", "required": False, "kind": "text"},
    {"key": "store_name", "label": "店铺", "required": False, "kind": "text"},
    {"key": "sales_po_no", "label": "采购订单号", "required": False, "kind": "text"},
    {"key": "sku", "label": "SKU", "required": False, "kind": "text"},
    {"key": "asin", "label": "ASIN", "required": False, "kind": "text"},
    {"key": "quantity", "label": "数量", "required": False, "kind": "int"},
    {"key": "unit_price", "label": "采购单价", "required": False, "kind": "money"},
    {"key": "sales_total", "label": "销售总额", "required": False, "kind": "money"},
    {"key": "price_cap", "label": "限价", "required": False, "kind": "money"},
    {"key": "order_time", "label": "下单时间", "required": False, "kind": "datetime"},
    {"key": "ship_days", "label": "配送天数", "required": False, "kind": "int"},
    {"key": "suggested_purchase_date", "label": "建议采购日期", "required": False, "kind": "date"},
    {"key": "ship_method", "label": "配送方式", "required": False, "kind": "text"},
    {"key": "recipient_name", "label": "收货人姓名", "required": False, "kind": "text"},
    {"key": "address1", "label": "收货人地址", "required": False, "kind": "text"},
    {"key": "address2", "label": "地址2", "required": False, "kind": "text"},
    {"key": "city", "label": "收货人城市", "required": False, "kind": "text"},
    {"key": "state", "label": "收货人州/省", "required": False, "kind": "text"},
    {"key": "postal_code", "label": "邮编", "required": False, "kind": "text"},
    {"key": "phone", "label": "收货人电话", "required": False, "kind": "text"},
    {"key": "country", "label": "收货人国家", "required": False, "kind": "text"},
    {"key": "district", "label": "收货人区", "required": False, "kind": "text"},
    {"key": "review_value", "label": "人工审核", "required": False, "kind": "enum"},
    {"key": "purchaser", "label": "采购方", "required": False, "kind": "enum"},
    {"key": "purchase_status_value", "label": "采购状态", "required": False, "kind": "enum"},
    {"key": "after_sale_value", "label": "售后状态", "required": False, "kind": "enum"},
    {"key": "external_amz_order_no", "label": "AMZ订单号", "required": False, "kind": "text"},
    {"key": "external_buyer_account", "label": "买家号", "required": False, "kind": "text"},
    {"key": "external_buyer_customer_id", "label": "买家号ID", "required": False, "kind": "text"},
]

SYSTEM_FIELD_KEYS: tuple[str, ...] = tuple(f["key"] for f in SYSTEM_FIELDS)
_FIELD_BY_KEY: dict[str, dict[str, Any]] = {f["key"]: f for f in SYSTEM_FIELDS}

# ── 准入规则缺省值（契约 §6）────────────────────────────────────────────────────

DEFAULT_ADMISSION: dict[str, Any] = {
    "approved_values": ["通过"],
    "rejected_values": ["拒绝", "建议拒绝", "钓鱼"],
    "purchaser_values": [],                       # 为空 = 不按采购方过滤
    "skip_purchase_status_values": ["已拍单", "拿回"],
    "cancel_after_sale_values": [],
    "import_historical": False,
}

_ADMISSION_LIST_KEYS = ("approved_values", "rejected_values", "purchaser_values",
                        "skip_purchase_status_values", "cancel_after_sale_values")

#: 文件来源的准入缺省（在 DEFAULT_ADMISSION 之上）：带 AMZ 订单号的行按历史采购登记（v1 §6.2）；
#: 售后状态为「已取消」的行算上游取消（文件是快照，取消要靠更新后的文件表达，v1 §6.2）。工作区可在设置里改。
FILE_ADMISSION_DEFAULTS: dict[str, Any] = {"import_historical": True, "cancel_after_sale_values": ["已取消"]}
#: 准入规则每个取值列表的上限（条数 / 每条字数）
_ADMISSION_MAX_ITEMS, _ADMISSION_MAX_LEN = 50, 50

#: 缺少唯一键时写进 issues 的固定文案（调用方据此把该行判为 invalid）
ISSUE_NO_ORDER_NO = "缺少平台订单号"

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
#: amazon.com 订单号：3-7-7 位数字
AMAZON_ORDER_NO_RE = re.compile(r"^\d{3}-\d{7}-\d{7}$")
#: 明显表示「没有单号」的占位写法（比较前去空白、转小写）
_AMZ_PLACEHOLDERS = frozenset({"-", "--", "---", "—", "——", "–", "－", "/", "无", "没有", "空", "暂无", "n/a", "na",
                               "none", "null", "nil", "0", "#"})
#: Excel / WPS 的错误值（公式出错时单元格的缓存值）
EXCEL_ERRORS = frozenset({"#N/A", "#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!", "#GETTING_DATA",
                          "#SPILL!", "#CALC!", "#FIELD!", "#BLOCKED!", "#CONNECT!", "#BUSY!", "#UNKNOWN!", "#N/A!"})
#: 科学计数法文本（如 CSV 里的 1.23457E+12）：精度已丢失，不能当订单号
_SCI_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?[eE][+-]?\d+$")
#: 文本金额：普通数字，或逗号只作千分位（1,234.50）；可带 $ 与负号
_MONEY_TEXT_RE = re.compile(r"^-?\$?-?\s*(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")
#: numeric(12,2) 能存的最大整数位：绝对值须小于 10^10
_MONEY_LIMIT = Decimal(10) ** 10
#: 配送天数的合理范围（天）
SHIP_DAYS_MAX = 60
#: 时刻与日期的合理年份范围
YEAR_MIN, YEAR_MAX = 2000, 2100
_COUNTRY_US = {"US", "USA", "U.S.", "U.S.A.", "UNITED STATES", "UNITED STATES OF AMERICA", "美国"}
_UTC = timezone.utc


class _Multi:
    """列表里有多个非空值、而字段只要一个值时的标记。"""


_MULTI = _Multi()


# ── 准入规则 ─────────────────────────────────────────────────────────────────


def effective_admission(admission: dict[str, Any] | None) -> dict[str, Any]:
    """输入：连接或批次保存的准入规则（可缺键、可为 None）→ 输出：补齐缺省值、取值去首尾空白后的完整规则。

    显式给出的空列表保留为空（例如 approved_values=[] 表示没有任何取值算「通过」）；
    键缺失或值为 None 时用 DEFAULT_ADMISSION 的缺省值。
    """
    src = admission or {}
    out: dict[str, Any] = {}
    for key in _ADMISSION_LIST_KEYS:
        raw = src.get(key)
        if raw is None:
            raw = DEFAULT_ADMISSION[key]
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        out[key] = [s for s in (_clean_str(v) for v in raw) if s]
    hist = src.get("import_historical")
    out["import_historical"] = bool(hist) if hist is not None else DEFAULT_ADMISSION["import_historical"]
    return out


def check_admission(admission: Any) -> dict[str, Any]:
    """输入：用户提交的准入规则（可缺键）→ 输出：校验后的同一份规则（只含给出的键，取值去首尾空白）。

    未知键、取值不是文本列表（import_historical 不是布尔）、列表过长 → errors.validation（field='admission'）。
    """
    from services.errors import validation

    if admission is None:
        return {}
    if not isinstance(admission, dict):
        raise validation("准入规则格式不对", field="admission")
    unknown = set(admission) - set(DEFAULT_ADMISSION)
    if unknown:
        raise validation(f"准入规则里有未知的项：{'、'.join(sorted(unknown))}", field="admission")
    out: dict[str, Any] = {}
    for k, v in admission.items():
        if k == "import_historical":
            if v is not None and not isinstance(v, bool):
                raise validation("「导入历史采购」只能是是或否", field="admission")
            out[k] = v
        elif v is None:
            out[k] = None
        elif not (isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v)):
            raise validation(f"准入规则「{k}」必须是文本列表", field="admission")
        elif len(v) > _ADMISSION_MAX_ITEMS or any(len(x.strip()) > _ADMISSION_MAX_LEN for x in v):
            raise validation(f"准入规则「{k}」最多 {_ADMISSION_MAX_ITEMS} 项、每项不超过 {_ADMISSION_MAX_LEN} 字",
                             field="admission")
        else:
            out[k] = [x.strip() for x in v if x.strip()]
    return out


def admission_skip_reason(record: UpstreamRecord, admission: dict[str, Any] | None) -> str | None:
    """输入：规范化后的记录、准入规则 → 输出：不得据此**新建**采购任务的中文原因；可以新建时为 None。

    只回答「库里还没有这笔采购单时，要不要新建任务」。已有任务的同键记录一律照常作为更新交给
    orders.ingest（取消、外部单号、人审变化都要传达到已有任务），调用方不要用本函数挡掉更新。
    缺平台订单号属于 invalid，不在这里判断；人审未通过的行不跳过（新建后停在「待确认」）。

    判定顺序（命中即返回）：

    1. 采购方：purchaser_values 非空且 purchaser 不在其中（purchaser 为空也算不在）→ 跳过。
    2. 上游已取消（售后状态命中 cancel_after_sale_values）→ 跳过，不为已取消的单新建任务。
    3. 已有 AMZ 订单号（external_amz_order_no 非空）= 历史采购：
       - import_historical 为 true → None，交给 ingest 按外部单号直接建成「已采购」（不会下单）；
       - 否则跳过（不论采购状态是什么）。
    4. 没有 AMZ 订单号、但采购状态命中 skip_purchase_status_values（默认「已拍单」「拿回」）→
       **一律跳过，与 import_historical 无关**：没有单号无法登记为已采购，新建又可能重复下单。
    5. 其余 → None。
    """
    adm = effective_admission(admission)
    purchasers = adm["purchaser_values"]
    if purchasers and (record.purchaser or "").strip() not in purchasers:
        who = record.purchaser or "（空）"
        return f"采购方「{who}」不在本系统负责的采购方内，不新建采购"
    if record.upstream_cancelled:
        return f"售后状态为「{record.after_sale_value}」，上游已取消，不新建采购"
    if record.external_amz_order_no and amazon_order_no_ok(record.external_amz_order_no):
        if adm["import_historical"]:
            return None
        return "已有 AMZ 订单号（历史采购），未开启导入历史采购，不新建采购"
    status = _skip_status(record, adm)
    if status:
        return f"采购状态为「{status}」但没有 AMZ 订单号，不新建采购"
    # 不可信的 AMZ 订单号（格式不对）不当历史采购：交给 ingest 建成「待核实」
    return None


def admission_hold(record: UpstreamRecord, admission: dict[str, Any] | None) -> dict[str, Any] | None:
    """输入：规范化后的记录、准入规则 → 输出：**已有任务**因准入规则应暂停（转待核实、解除分配）的说明
    {kind: 'purchaser' | 'purchase_status', field, value, reason}；没有 → None。

    与 admission_skip_reason 对应，但只看「这一行现在说的已不归本系统采购」：

    1. 采购方：purchaser_values 非空且 purchaser 不在其中（清空也算不在）；
    2. 采购状态命中 skip_purchase_status_values（如「已拍单」「拿回」）且没有可信 AMZ 订单号（有单号走外部单号规则）。

    上游取消、人审、外部单号由 orders.ingest 各自的规则处理，不在这里判断。是否适用（只限采购开始前、
    本系统写回的「已拍单」不算）由 orders.ingest 决定。
    """
    adm = effective_admission(admission)
    purchasers = adm["purchaser_values"]
    if purchasers and (record.purchaser or "").strip() not in purchasers:
        who = record.purchaser or "（空）"
        return {"kind": "purchaser", "field": "purchaser", "value": record.purchaser,
                "reason": f"采购方变为「{who}」，不在本系统负责的采购方内"}
    if record.external_amz_order_no and amazon_order_no_ok(record.external_amz_order_no):
        return None
    status = _skip_status(record, adm)
    if status:
        return {"kind": "purchase_status", "field": "purchase_status_value", "value": status,
                "reason": f"采购状态变为「{status}」（没有 AMZ 订单号），可能已在别处采购"}
    return None


def amazon_order_no_ok(no: str | None) -> bool:
    """输入：AMZ 订单号文本 → 输出：是否为可信的 amazon.com 订单号（3-7-7 位数字）。"""
    return bool(no) and bool(AMAZON_ORDER_NO_RE.match(str(no).strip()))


def _skip_status(record: UpstreamRecord, adm: dict[str, Any]) -> str | None:
    """采购状态（多选时逐项）命中 skip_purchase_status_values → 命中的原文；否则 None。"""
    status = (record.purchase_status_value or "").strip()
    if not status:
        return None
    parts = [status, *(x.strip() for x in status.split(", "))]
    return status if any(x and x in adm["skip_purchase_status_values"] for x in parts) else None


# ── 规范化 ───────────────────────────────────────────────────────────────────


def normalize(values: dict[str, Any], admission: dict[str, Any] | None = None) -> UpstreamRecord:
    """输入：{系统字段: Python 值}（未知键忽略）、准入规则 → 输出：UpstreamRecord（转换问题记在 issues）。

    缺少平台订单号时 upstream_order_no 为空串，issues 第一条为 ISSUE_NO_ORDER_NO；调用方必须把该行
    判为 invalid，不得交给 ingest。country 缺省 'US'；review / upstream_cancelled 按准入规则判定。
    """
    adm = effective_admission(admission)
    issues: list[str] = []
    out: dict[str, Any] = {}
    enums: dict[str, list[str]] = {}
    key_problem: str | None = None
    for f in SYSTEM_FIELDS:
        key, label, kind = f["key"], f["label"], f["kind"]
        raw = values.get(key)
        if key == "upstream_order_no":
            out[key], key_problem = _order_no(raw)
        elif kind in ("text", "enum"):
            out[key] = _text_field(raw, label, issues)
            if kind == "enum":
                enums[key] = _enum_values(raw)
        elif kind == "int":
            out[key] = _to_int(raw, label, issues, allow_zero=(key != "quantity"),
                               max_value=SHIP_DAYS_MAX if key == "ship_days" else None)
        elif kind == "money":
            out[key] = _to_money(raw, label, issues, cap=(key == "price_cap"))
        elif kind == "datetime":
            out[key] = _to_datetime(raw, label, issues)
        elif kind == "date":
            out[key] = _to_date(raw, label, issues)
        else:  # pragma: no cover - 清单写错
            raise ValueError(f"未知字段种类 {kind}")

    order_no = out.pop("upstream_order_no")
    if not order_no:
        issues.insert(0, key_problem or ISSUE_NO_ORDER_NO)
        order_no = ""

    out["asin"] = _check_asin(out["asin"], issues)
    out["country"] = _normalize_country(out["country"])
    out["postal_code"] = _check_postal(out["postal_code"], issues, raw=values.get("postal_code"),
                                       us=out["country"] == "US")
    if out["state"] and len(out["state"]) == 2:
        out["state"] = out["state"].upper()
    out["external_amz_order_no"] = _check_amazon_no(out["external_amz_order_no"], issues)

    review_vals = enums.get("review_value") or []
    if any(v in adm["rejected_values"] for v in review_vals):
        review = "rejected"
    elif review_vals and all(v in adm["approved_values"] for v in review_vals):
        review = "approved"
    else:
        review = "unknown"
    cancelled = any(v in adm["cancel_after_sale_values"] for v in enums.get("after_sale_value") or [])

    return UpstreamRecord(upstream_order_no=order_no, review=review, upstream_cancelled=cancelled,
                          issues=issues, **out)


def parse_datetime_text(text: str) -> datetime | None:
    """输入：文本时刻（如 '2026/10/27 21:31'、'2026-10-27 21:31:00'、ISO 带时区）→ 输出：UTC 时刻或 None。

    不带时区的文本按北京时间解释；只有日期时取北京时间当天 00:00。
    """
    dt = _parse_text_datetime(text)
    return _bj_aware(dt) if dt is not None else None


# ── 单值转换（失败 → None + 问题）────────────────────────────────────────────────


def _clean_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, tuple)):
        return all(_is_blank(x) for x in v)
    return False


def _single(v: Any) -> Any:
    """列表 → 唯一的非空元素；多个非空元素 → _MULTI；全空 → None。非列表原样返回。"""
    if isinstance(v, (list, tuple)):
        vals = [x for x in v if not _is_blank(x)]
        if not vals:
            return None
        return vals[0] if len(vals) == 1 else _MULTI
    return v


def _to_text(v: Any) -> str | None:
    """文本：去首尾空白，空串 → None；整数值的浮点数去掉 .0；列表用「, 」拼接；布尔转「是 / 否」。"""
    if _is_blank(v):
        return None
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (list, tuple)):
        parts = [_to_text(x) for x in v]
        return ", ".join(p for p in parts if p) or None
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, Decimal):
        if v.is_finite() and v == v.to_integral_value():
            return str(int(v))
        return format(v, "f")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return _clean_str(v)


def _to_int(v: Any, label: str, issues: list[str], *, allow_zero: bool, max_value: int | None = None) -> int | None:
    v = _single(v)
    if _is_blank(v):
        return None
    what = "非负整数" if allow_zero else "正整数"
    n: int | None = None
    if isinstance(v, bool) or v is _MULTI:
        n = None
    elif isinstance(v, int):
        n = v
    elif isinstance(v, float):
        n = int(v) if v.is_integer() else None
    elif isinstance(v, Decimal):
        n = int(v) if v.is_finite() and v == v.to_integral_value() else None
    else:
        s = str(v).strip().replace(",", "").replace("，", "")
        s = re.sub(r"\s*(天|日|days?|个|件|pcs?)$", "", s, flags=re.IGNORECASE)
        try:
            d = Decimal(s)
            if d.is_finite() and d == d.to_integral_value():
                n = int(d)
        except (InvalidOperation, ValueError):
            n = None
    if n is None or n < 0 or (n == 0 and not allow_zero):
        issues.append(f"{label}不是{what}：{_show(v)}")
        return None
    if max_value is not None and n > max_value:
        issues.append(f"{label}超出合理范围（0–{max_value}）：{_show(v)}")
        return None
    return n


def _money_value(x: Any, cap: bool) -> Decimal | None:
    """数字或已校验格式的文本 → 两位小数（限价向下取整）；超出 numeric(12,2) 或无法换算 → None。"""
    try:
        d = (money_cap if cap else money)(x)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if d is None or abs(d) >= _MONEY_LIMIT:
        return None
    return d


def _to_money(v: Any, label: str, issues: list[str], *, cap: bool = False) -> Decimal | None:
    """金额：数字原样换算；文本只认普通写法或千分位逗号（「12,5」「1.234,50」「1e3」都不认）。cap=True（限价）向下取整到分。"""
    v = _single(v)
    if _is_blank(v):
        return None
    d: Decimal | None = None
    if isinstance(v, bool) or v is _MULTI:
        d = None
    elif isinstance(v, (int, float, Decimal)):
        d = _money_value(Decimal(str(v)) if isinstance(v, float) else v, cap)
    else:
        s = re.sub(r"(?i)usd", "", str(v)).replace("，", ",").strip()
        d = _money_value(s, cap) if _MONEY_TEXT_RE.match(s) else None
    if d is None:
        issues.append(f"{label}不是有效金额：{_show(v)}")
        return None
    if d < 0:
        issues.append(f"{label}不能为负数：{d}")
        return None
    return d


_DT_PATTERNS = (
    # 年-月-日 [时:分[:秒]]，分隔符 - / . 或「年 月 日」，日期与时刻之间可用空格或 T
    re.compile(
        r"^(?P<y>\d{4})\s*(?:[-/.]|年)\s*(?P<m>\d{1,2})\s*(?:[-/.]|月)\s*(?P<d>\d{1,2})\s*日?"
        r"(?:(?:\s+|T)(?P<H>\d{1,2})\s*[:：]\s*(?P<M>\d{1,2})(?:\s*[:：]\s*(?P<S>\d{1,2})(?:\.\d+)?)?)?$"
    ),
    # 紧凑写法：20261027、20261027 2131、20261027 21:31:00
    re.compile(r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?:\s+(?P<H>\d{2}):?(?P<M>\d{2})(?::?(?P<S>\d{2}))?)?$"),
)


def _parse_text_datetime(s: str) -> datetime | None:
    """文本 → naive（未带时区）或 aware datetime；认不出返回 None。"""
    s = s.strip()
    for pat in _DT_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        try:
            y, mo, d = int(m["y"]), int(m["m"]), int(m["d"])
            if m["H"] is None:
                return datetime(y, mo, d)
            return datetime(y, mo, d, int(m["H"]), int(m["M"]), int(m["S"] or 0))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s)
    except ValueError:
        return None


def _from_number(n: float) -> datetime | None:
    """数字时刻：1e11–1e14 视为毫秒时间戳（UTC，飞书日期字段）；20000–80000 视为 Excel 日期序号（本地时刻）。"""
    if 1e11 <= n < 1e14:
        return datetime.fromtimestamp(n / 1000, tz=_UTC)
    if 20000 <= n < 80000:
        return (datetime(1899, 12, 30) + timedelta(days=n)).replace(microsecond=0)
    return None


def _bj_aware(dt: datetime) -> datetime:
    """naive → 按北京时间解释；aware → 原样。结果统一为 UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=clock.BJ_TZ)
    return dt.astimezone(_UTC)


def _coerce_moment(v: Any) -> datetime | date | None:
    """单值 → datetime（naive 或 aware）/ date / None（认不出）。"""
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return v
    if isinstance(v, bool) or v is _MULTI:
        return None
    if isinstance(v, (int, float, Decimal)):
        if isinstance(v, int) and 19000101 <= v <= 21001231:      # 紧凑日期数字 20261031
            return _parse_text_datetime(str(v))
        return _from_number(float(v))
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\d{11,14}(\.\d+)?", s):
            return _from_number(float(s))
        return _parse_text_datetime(s)
    return None


def _to_datetime(v: Any, label: str, issues: list[str]) -> datetime | None:
    v = _single(v)
    if _is_blank(v):
        return None
    m = _coerce_moment(v)
    if m is None:
        issues.append(f"{label}无法识别：{_show(v)}")
        return None
    if not YEAR_MIN <= m.year <= YEAR_MAX:
        issues.append(f"{label}年份不合理：{_show(v)}")
        return None
    if not isinstance(m, datetime):
        m = datetime(m.year, m.month, m.day)
    return _bj_aware(m)


def _to_date(v: Any, label: str, issues: list[str]) -> date | None:
    """日期：带时区的时刻取北京日期（飞书日期字段存的是北京当天 00:00 的毫秒值）；不带时区的取其日期部分。"""
    v = _single(v)
    if _is_blank(v):
        return None
    m = _coerce_moment(v)
    if m is None:
        issues.append(f"{label}无法识别：{_show(v)}")
        return None
    if not YEAR_MIN <= m.year <= YEAR_MAX:
        issues.append(f"{label}年份不合理：{_show(v)}")
        return None
    if isinstance(m, datetime):
        return clock.bj_date(m) if m.tzinfo is not None else m.date()
    return m


def _check_asin(asin: str | None, issues: list[str]) -> str | None:
    if asin is None:
        return None
    up = re.sub(r"\s+", "", asin).upper()
    if not _ASIN_RE.match(up):
        issues.append(f"ASIN 格式不对：{_show(asin)}")
        return None
    return up


def _check_postal(text: str | None, issues: list[str], *, raw: Any = None, us: bool = True) -> str | None:
    """邮编按文本保留；美国地址、来源单元格是数字（Excel 数字格式 / 飞书数字列丢了前导 0）且只有 3–4 位时
    补前导 0 到 5 位并记问题；其余主体不足 5 位数字时只记问题（orders 据此判为资料待补齐）。"""
    if text is None:
        return None
    one = _single(raw)
    numeric = isinstance(one, (int, float, Decimal)) and not isinstance(one, bool)
    if us and numeric and text.isdigit() and 3 <= len(text) <= 4:
        padded = text.zfill(5)
        issues.append(f"邮编 {text} 是数字格式，已补前导 0 为 {padded}")
        return padded
    digits = re.sub(r"\D", "", text.split("-")[0])
    if len(digits) < 5:
        issues.append(f"邮编不足 5 位：{_show(text)}")
    return text


def _check_amazon_no(text: str | None, issues: list[str]) -> str | None:
    """AMZ 订单号：占位写法（「—」「无」「N/A」…）→ None 并记问题；格式不对的原样保留并记问题（orders 判待核实）。"""
    if text is None:
        return None
    s = text.strip()
    if s.lower() in _AMZ_PLACEHOLDERS:
        issues.append(f"AMZ订单号「{_show(s)}」不是订单号，按没有单号处理")
        return None
    if not AMAZON_ORDER_NO_RE.match(s):
        issues.append(f"AMZ订单号「{_show(s)}」格式不对（应为 3-7-7 位数字），需人工核实")
    return s


def _order_no(raw: Any) -> tuple[str | None, str | None]:
    """平台订单号 → (文本, None)；不可用时 (None, 原因)：多个值、表格错误值、科学计数法文本（精度已丢失）。"""
    if isinstance(raw, (list, tuple)):
        vals = list(dict.fromkeys(t for t in (_to_text(x) for x in raw) if t))
        if len(vals) > 1:
            return None, f"平台订单号有多个值（{_show('、'.join(vals))}），无法确定是哪一笔"
        raw = vals[0] if vals else None
    text = _to_text(raw)
    if text is None:
        return None, None
    if text.upper() in EXCEL_ERRORS:
        return None, f"平台订单号是表格错误值 {text}"
    if isinstance(raw, str) and _SCI_RE.match(text):
        return None, f"平台订单号「{text}」是科学计数法，位数已丢失，请把该列设为文本后重新导出"
    return text, None


def _text_field(raw: Any, label: str, issues: list[str]) -> str | None:
    """文本 / 取值字段：同 _to_text；表格错误值（#N/A 等）→ None 并记问题。"""
    text = _to_text(raw)
    if text is not None and text.upper() in EXCEL_ERRORS:
        issues.append(f"{label}是表格错误值 {text}，按没读到处理")
        return None
    return text


def _enum_values(raw: Any) -> list[str]:
    """取值字段（单选 / 多选）→ 去空白后的取值列表（多选逐项；表格错误值不算）。"""
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out = []
    for x in items:
        t = _to_text(x)
        if t is not None and t.upper() not in EXCEL_ERRORS:
            out.append(t)
    return out


def _normalize_country(c: str | None) -> str:
    if c is None:
        return "US"
    if c.upper() in _COUNTRY_US:
        return "US"
    return c.upper() if len(c) == 2 else c


def _show(v: Any) -> str:
    if v is _MULTI:
        return "（多个值）"
    s = str(v)
    return s if len(s) <= 40 else s[:40] + "…"
