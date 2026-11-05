"""上游记录规范化：把「系统字段 → Python 值」转成 models.UpstreamRecord，并给出准入判断。

飞书记录（services/feishu_fields）与文件行（services/file_mapping）都先各自取出
「系统字段 → 原值」，再交给这里的 normalize()。这里只做类型转换与准入规则：

- 转换失败不抛异常：值置 None 并把问题写进 record.issues（「ASIN 格式不对」「数量不是正整数」等）；
- NULL 不是 0：空串、空白、空列表一律视为 None，绝不补 0；
- 文本时刻（如 ``2026/10/27 21:31``）与不带时区的单元格时刻按北京时间解释；带时区的时刻原样保留；
- 人审结论与上游取消由准入规则（admission，契约 §6）判定，取值比较为去首尾空白后的完全相等。

本模块不碰数据库，也不计算建议采购日期（由 services/orders 按「下单北京日期 + 6 − 配送天数」补算）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from services import clock
from services.models import UpstreamRecord, money

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

#: 缺少唯一键时写进 issues 的固定文案（调用方据此把该行判为 invalid）
ISSUE_NO_ORDER_NO = "缺少平台订单号"

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
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
    if record.external_amz_order_no:
        if adm["import_historical"]:
            return None
        return "已有 AMZ 订单号（历史采购），未开启导入历史采购，不新建采购"
    status = (record.purchase_status_value or "").strip()
    if status and status in adm["skip_purchase_status_values"]:
        return f"采购状态为「{status}」但没有 AMZ 订单号，不新建采购"
    return None


# ── 规范化 ───────────────────────────────────────────────────────────────────


def normalize(values: dict[str, Any], admission: dict[str, Any] | None = None) -> UpstreamRecord:
    """输入：{系统字段: Python 值}（未知键忽略）、准入规则 → 输出：UpstreamRecord（转换问题记在 issues）。

    缺少平台订单号时 upstream_order_no 为空串，issues 第一条为 ISSUE_NO_ORDER_NO；调用方必须把该行
    判为 invalid，不得交给 ingest。country 缺省 'US'；review / upstream_cancelled 按准入规则判定。
    """
    adm = effective_admission(admission)
    issues: list[str] = []
    out: dict[str, Any] = {}
    for f in SYSTEM_FIELDS:
        key, label, kind = f["key"], f["label"], f["kind"]
        raw = values.get(key)
        if kind in ("text", "enum"):
            out[key] = _to_text(raw)
        elif kind == "int":
            out[key] = _to_int(raw, label, issues, allow_zero=(key != "quantity"))
        elif kind == "money":
            out[key] = _to_money(raw, label, issues)
        elif kind == "datetime":
            out[key] = _to_datetime(raw, label, issues)
        elif kind == "date":
            out[key] = _to_date(raw, label, issues)
        else:  # pragma: no cover - 清单写错
            raise ValueError(f"未知字段种类 {kind}")

    order_no = out.pop("upstream_order_no")
    if not order_no:
        issues.insert(0, ISSUE_NO_ORDER_NO)
        order_no = ""

    out["asin"] = _check_asin(out["asin"], issues)
    out["postal_code"] = _check_postal(out["postal_code"], issues)
    out["country"] = _normalize_country(out["country"])
    if out["state"] and len(out["state"]) == 2:
        out["state"] = out["state"].upper()

    review_value = out["review_value"]
    if review_value is not None and review_value in adm["approved_values"]:
        review = "approved"
    elif review_value is not None and review_value in adm["rejected_values"]:
        review = "rejected"
    else:
        review = "unknown"
    after_sale = out["after_sale_value"]
    cancelled = after_sale is not None and after_sale in adm["cancel_after_sale_values"]

    return UpstreamRecord(upstream_order_no=order_no, review=review, upstream_cancelled=cancelled,
                          issues=issues, **out)


def field_label(key: str) -> str:
    """输入：系统字段键 → 输出：中文名（未知键原样返回）。"""
    f = _FIELD_BY_KEY.get(key)
    return f["label"] if f else key


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


def _to_int(v: Any, label: str, issues: list[str], *, allow_zero: bool) -> int | None:
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
    return n


def _to_money(v: Any, label: str, issues: list[str]) -> Decimal | None:
    v = _single(v)
    if _is_blank(v):
        return None
    d: Decimal | None = None
    if isinstance(v, bool) or v is _MULTI:
        d = None
    elif isinstance(v, (int, float, Decimal)):
        d = money(Decimal(str(v)) if isinstance(v, float) else v)
    else:
        s = re.sub(r"(?i)usd", "", str(v)).replace("，", ",")
        d = money(s)
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


def _check_postal(text: str | None, issues: list[str]) -> str | None:
    """邮编按文本保留（不补前导 0，以免猜错）；主体不足 5 位数字时记问题。"""
    if text is None:
        return None
    digits = re.sub(r"\D", "", text.split("-")[0])
    if len(digits) < 5:
        issues.append(f"邮编不足 5 位：{_show(text)}")
    return text


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
