"""共享的形状与常量：上游记录、来源引用、状态集合、错误码表。

这里只放「多个模块都要认的同一份定义」，不放业务判断。契约见 docs/development/开发契约-v2.md。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
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
    ErrorCode("ITEM_MISMATCH", "guard", "结算商品或数量与任务不一致"),
    ErrorCode("ZIP_MISMATCH", "guard", "收货邮编与任务不一致"),
    ErrorCode("SUBTOTAL_OUT_OF_RANGE", "guard", "商品小计不在账号金额区间内"),
    ErrorCode("PRICE_OVER_CAP", "guard", "结算总额超过限价"),
    ErrorCode("DELIVERY_TOO_LONG", "guard", "预计送达超过配送上限"),
    ErrorCode("DATA_UNRELIABLE", "guard", "结算页金额或配送日期读取不可靠"),
    ErrorCode("CARD_MISMATCH", "guard", "支付卡尾号与账号设置不符"),
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


def money(v: Any) -> Decimal | None:
    """输入：数字 / 字符串（可带 $ 与千分位）/ None → 输出：两位小数的 Decimal 或 None。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        d = v
    else:
        s = str(v).strip().replace("$", "").replace(",", "").replace("US", "").strip()
        if not s:
            return None
        try:
            d = Decimal(s)
        except Exception:
            return None
    if not d.is_finite():
        return None
    return d.quantize(Decimal("0.01"))
