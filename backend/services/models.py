"""共享的形状与常量：上游记录、来源引用、状态集合、错误码表。

这里只放「多个模块都要认的同一份定义」，不放业务判断。契约见 docs/development/开发契约-v2.md。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Context, Decimal
from typing import Any

# ── 任务状态 ──────────────────────────────────────────────────────────────────

ORDER_STATUSES = (
    "pending_review", "needs_info", "conflict", "queued", "assigned",
    "executing", "result_unknown", "exception", "purchased",
    "cancelled", "repurchase_pending", "closed",
)

#: 采购开始前的状态：来源更新可以改原值并重新派生
PRE_EXECUTION_STATUSES = frozenset({"pending_review", "needs_info", "conflict", "queued", "assigned"})

ORDER_STATUS_ZH = {
    "pending_review": "待确认",
    "needs_info": "资料待补齐",
    "conflict": "待核实",
    "queued": "待分配",
    "assigned": "已分配",
    "executing": "采购中",
    "result_unknown": "结果待核实",
    "exception": "异常",
    "purchased": "已采购",
    "cancelled": "已取消",
    "repurchase_pending": "待决定重采",
    "closed": "已关闭",
}

# ── 采购尝试状态 ──────────────────────────────────────────────────────────────

ATTEMPT_STATUSES = (
    "claimed", "checking", "guard_passed", "submitting", "verifying",
    "succeeded", "failed", "unknown", "aborted",
)
ATTEMPT_INFLIGHT = frozenset({"claimed", "checking", "guard_passed", "submitting", "verifying"})
#: 不占额度的尝试结局
ATTEMPT_QUOTA_FREE = frozenset({"failed", "aborted"})

# ── 执行所需字段（缺一即 needs_info）────────────────────────────────────────────

REQUIRED_FOR_EXECUTION = (
    ("asin", "ASIN"),
    ("quantity", "数量"),
    ("unit_price", "采购单价"),
    ("price_cap", "限价"),
    ("suggested_purchase_date", "建议采购日期"),
    ("recipient_name", "收货人姓名"),
    ("address1", "收货地址"),
    ("city", "城市"),
    ("state", "州"),
    ("postal_code", "邮编"),
    ("phone", "收货人电话"),
)

#: 跨来源比对与「执行相关原值」所用字段（不同来源这些字段不一致 → 冲突）
EXECUTION_FIELDS = (
    "asin", "quantity", "unit_price", "price_cap", "suggested_purchase_date",
    "recipient_name", "address1", "address2", "city", "state", "postal_code", "phone", "country",
)

# ── 插件错误码 ────────────────────────────────────────────────────────────────
# category：task = 本任务转异常；account = 暂停账号执行、任务保留；retry_once = 提交前页面错误自动重试一次；
#           abort = 插件按服务端指示中止；guard = 服务端护栏拦截（按 task 处理）。


@dataclass(frozen=True)
class ErrorCode:
    code: str
    category: str
    zh: str


ERROR_CODES: dict[str, ErrorCode] = {e.code: e for e in (
    ErrorCode("OUT_OF_STOCK", "task", "商品无库存"),
    ErrorCode("QUANTITY_UNAVAILABLE", "task", "无法选择所需数量"),
    ErrorCode("ADDRESS_FAILED", "task", "收货地址填写失败"),
    ErrorCode("CART_MISMATCH", "task", "购物车内容与任务不一致"),
    ErrorCode("UNEXPECTED_PAGE", "task", "页面结构与预期不符"),
    ErrorCode("PAGE_LOAD_FAILED", "retry_once", "提交前页面加载失败"),
    ErrorCode("LOGIN_REQUIRED", "account", "买家账号登录失效"),
    ErrorCode("ACCOUNT_RESTRICTED", "account", "买家账号受限"),
    ErrorCode("CARD_DECLINED", "account", "银行卡被拒付"),
    ErrorCode("CAPTCHA_ENCOUNTERED", "account", "遇到验证码或风控验证，需要人工处理"),
    ErrorCode("IDENTITY_MISMATCH", "account", "浏览器登录的买家账号与绑定不符"),
    ErrorCode("ABORTED_UPSTREAM_CANCELLED", "abort", "上游已取消，已中止"),
    ErrorCode("ABORTED_BY_USER", "abort", "插件端人工中止"),
    ErrorCode("ABORTED_BY_SERVER", "abort", "服务端要求中止（账号暂停或归档、身份不符或过期、来源有变化），未提交前中止"),
    ErrorCode("ITEM_MISMATCH", "guard", "结算商品或数量与任务不一致"),
    ErrorCode("ZIP_MISMATCH", "guard", "收货邮编与任务不一致"),
    ErrorCode("SUBTOTAL_OUT_OF_RANGE", "guard", "商品小计不在账号金额区间内"),
    ErrorCode("PRICE_OVER_CAP", "guard", "结算总额超过限价"),
    ErrorCode("DELIVERY_TOO_LONG", "guard", "预计送达超过配送上限"),
    ErrorCode("DATA_UNRELIABLE", "guard", "结算页金额或配送日期读取不可靠"),
    ErrorCode("CARD_MISMATCH", "guard", "支付卡尾号与账号设置不符"),
    ErrorCode("GUARD_BLOCKED", "guard", "护栏拦截"),
    # 插件端自己的结局码（契约 §5「结果处理」）：都发生在提交登记之前
    ErrorCode("SUBMIT_REJECTED", "task", "服务端拒绝了提交登记"),
    ErrorCode("PLUGIN_SIMULATION_ONLY", "account", "插件只执行模拟任务，却领到了真实任务"),
)}


# ── 上游记录与来源引用 ────────────────────────────────────────────────────────


@dataclass
class UpstreamRecord:
    """飞书记录与文件行规范化后的统一形状。只有 upstream_order_no 必填，其余 None = 没有或没读到。"""

    upstream_order_no: str
    order_line_id: str | None = None
    store_name: str | None = None
    sales_po_no: str | None = None
    sku: str | None = None
    asin: str | None = None
    quantity: int | None = None
    unit_price: Decimal | None = None
    sales_total: Decimal | None = None
    price_cap: Decimal | None = None
    order_time: datetime | None = None
    ship_days: int | None = None
    suggested_purchase_date: date | None = None
    ship_method: str | None = None
    recipient_name: str | None = None
    address1: str | None = None
    address2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    phone: str | None = None
    country: str | None = None
    district: str | None = None
    review_value: str | None = None
    review: str = "unknown"                 # approved / rejected / unknown
    purchaser: str | None = None
    purchase_status_value: str | None = None
    after_sale_value: str | None = None
    upstream_cancelled: bool = False
    external_amz_order_no: str | None = None
    external_buyer_account: str | None = None
    external_buyer_customer_id: str | None = None
    issues: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """输入：无 → 输出：可写入 jsonb 的 dict（Decimal / 日期转字符串）。"""
        return {k: _jsonable(v) for k, v in dataclasses.asdict(self).items()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "UpstreamRecord":
        """输入：to_json() 的结果 → 输出：UpstreamRecord（还原 Decimal / 日期类型）。"""
        d = dict(data)
        for k in ("unit_price", "sales_total", "price_cap"):
            if d.get(k) is not None:
                d[k] = Decimal(str(d[k]))
        if d.get("order_time"):
            d["order_time"] = datetime.fromisoformat(d["order_time"])
        if d.get("suggested_purchase_date"):
            d["suggested_purchase_date"] = date.fromisoformat(d["suggested_purchase_date"])
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def execution_fingerprint(self) -> str:
        """输入：无 → 输出：执行相关字段的 sha256（判断来源内容是否变化）。"""
        payload = {k: _jsonable(getattr(self, k)) for k in EXECUTION_FIELDS}
        payload.update({
            "review": self.review,
            "upstream_cancelled": self.upstream_cancelled,
            "external_amz_order_no": self.external_amz_order_no,
        })
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class SourceRef:
    """一条上游记录来自哪里。飞书：connection_id + record_id；文件：batch_id + row_no。"""

    kind: str                               # feishu / file
    snapshot: dict[str, Any]                # 来源原始字段 → 值（已转成可 JSON 的形式）
    connection_id: int | None = None
    record_id: str | None = None
    batch_id: int | None = None
    row_no: int | None = None

    def __post_init__(self) -> None:
        if self.kind == "feishu" and (self.connection_id is None or not self.record_id):
            raise ValueError("飞书来源需要 connection_id 与 record_id")
        if self.kind == "file" and (self.batch_id is None or self.row_no is None):
            raise ValueError("文件来源需要 batch_id 与 row_no")
        if self.kind not in ("feishu", "file"):
            raise ValueError(f"未知来源种类 {self.kind}")


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


_CENT = Decimal("0.01")
_NOISE = Decimal("0.000001")
#: 金额列都是 numeric(12,2)：绝对值须小于 10^10，放不下的按读不出（None）
MONEY_LIMIT = Decimal(10) ** 10
#: 模拟尝试的亚马逊订单号前缀（模拟单号只能以它开头，真实单号不得以它开头）
SIM_PREFIX = "SIM-"
#: 金额换算用自己的上下文（不受调用方 decimal.localcontext 影响）
_CTX = Context(prec=28)
#: 文本金额：可带正负号；整数部分是普通数字或按 3 位分组的千分位逗号；不认指数写法
_MONEY_TEXT_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?$|^[+-]?\.\d+$")
#: 文本金额的外层写法：只在**开头**认一个正负号、「US」/「USD」与「$」（标记后可留空白，「$」后还可再带一个
#: 正负号，如 -$5、$-5），结尾只认「USD」（如 52 usd，不分大小写），开头的 US / USD 与结尾的 USD 不能同时出现；
#: 别处出现的 $、US 或其他字母一律不认
_MONEY_WRAP_RE = re.compile(
    r"([+-]?)(USD?\s*)?(?:\$\s*)?([+-]?)((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(\s*USD)?",
    re.IGNORECASE,
)


def money(v: Any) -> Decimal | None:
    """输入：数字 / 字符串（开头可带 $、US、USD，结尾可带 USD，可有千分位逗号）/ None → 输出：两位小数的
    Decimal（四舍五入）或 None。

    先按 6 位小数抹掉浮点噪声（78.68999999 → 78.690000），再取两位。读不出、指数写法、逗号不是千分位
    （12,5、1.234,50）、$ 或 US 出现在数字中间或末尾（40$5、4US0、12US）、绝对值 ≥ 10^10（numeric(12,2)
    放不下）→ None。从不抛异常。
    """
    return _round(_parse_decimal(v), ROUND_HALF_UP)


def money_cap(v: Any) -> Decimal | None:
    """输入：同 money → 输出：用作上限（限价）的两位小数，**向下取整**（78.685 → 78.68），宁可少放行 1 美分；
    不合格的输入同样 → None。"""
    return _round(_parse_decimal(v), ROUND_DOWN)


def _round(d: Decimal | None, rounding: str) -> Decimal | None:
    if d is None:
        return None
    try:
        out = d.quantize(_NOISE, rounding=ROUND_HALF_UP, context=_CTX).quantize(_CENT, rounding=rounding, context=_CTX)
    except ArithmeticError:            # decimal.InvalidOperation 等：位数超出精度（|d| < 10^10 时不会发生）
        return None
    return out if out.copy_abs() < MONEY_LIMIT else None


def _parse_decimal(v: Any) -> Decimal | None:
    """输入：任意值 → 输出：有限且绝对值 < 10^10 的 Decimal；其他 → None（不抛异常）。

    数字（int / float / Decimal）按数值换算（float 取最短十进制表示）；其他值按文本：去掉首尾空白后整段须是
    _MONEY_WRAP_RE 的写法（标记只在开头 / 结尾，正负号最多一个，US 不能首尾都有），里面的数字只认 _MONEY_TEXT_RE 的写法。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        if isinstance(v, Decimal):
            d = v
        elif isinstance(v, int):
            d = Decimal(v)
        elif isinstance(v, float):
            d = Decimal(str(v))
        else:
            m = _MONEY_WRAP_RE.fullmatch(str(v).strip())
            if m is None or (m.group(1) and m.group(3)) or (m.group(2) and m.group(5)):
                return None
            num = (m.group(1) or m.group(3)) + m.group(4)
            if not _MONEY_TEXT_RE.match(num):
                return None
            d = Decimal(num.replace(",", ""))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if not d.is_finite() or d.copy_abs() >= MONEY_LIMIT:
        return None
    return d
