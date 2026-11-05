"""文件导入的表头对应：标准模板列、别名、自动识别、行规范化、模板生成。不碰数据库。

标准模板 = 小蜜蜂采购导入的 15 列 + 补充列，列名与飞书「订单中心」表头统一（契约 §7）。
只有「平台订单号」必填；其余列缺什么，任务就停在「待确认」或「资料待补齐」，不下发采购。

自动识别（suggest_mapping）按三轮进行，每个系统字段至多被一列占用，先到先得：

1. 精确匹配标准模板列名；
2. 精确匹配别名（含飞书表头写法，如「买家号1」「数量1」「地址1」「国家」「亚马逊订单号」）；
3. 宽松匹配：忽略空白、大小写、全角斜杠与括号、首尾「*」后与标准列名 / 别名 / 系统字段键相同。
"""

from __future__ import annotations

import re
from typing import Any

from services import errors
from services.models import UpstreamRecord
from services.normalize import SYSTEM_FIELD_KEYS, normalize

# ── 标准模板列（契约 §7，顺序即模板列序）────────────────────────────────────────
# (列名, 系统字段 | None, 说明)。系统字段为 None 的列只保留在来源快照里，不参与采购。

STANDARD_COLUMNS: list[tuple[str, str | None, str]] = [
    ("平台订单号", "upstream_order_no", "上游采购单唯一键（必填）。同一工作区内重复上传只对应一笔任务"),
    ("店铺账号", None, "仅保留在来源快照；没有「店铺」列的文件会把它当作店铺"),
    ("收货人姓名", "recipient_name", "收件人姓名"),
    ("收货人地址", "address1", "地址第 1 行"),
    ("地址2", "address2", "地址第 2 行（门牌、公寓号等）"),
    ("收货人城市", "city", "城市"),
    ("收货人州/省", "state", "州，两位字母缩写，如 FL"),
    ("邮编", "postal_code", "5 位邮编，请设为文本格式以保留前导 0（如 02134）"),
    ("收货人电话", "phone", "收件人电话"),
    ("AMZ订单号", "external_amz_order_no", "已有亚马逊订单号的行视为历史采购，不会重新下单"),
    ("买家号", "external_buyer_account", "历史采购使用的买家号"),
    ("买家号ID", "external_buyer_customer_id", "历史采购使用的买家号 ID"),
    ("收货人国家", "country", "国家，留空按 US"),
    ("ASIN", "asin", "10 位字母或数字，如 B0DEMO0001"),
    ("数量", "quantity", "正整数"),
    ("收货人区", "district", "区 / 县（可空）"),
    ("人工审核", "review_value", "填「通过」才会下发采购；留空或其他取值停在「待确认」"),
    ("限价", "price_cap", "美元。结算页最终应付总额（含税费、运费）不得超过此值"),
    ("采购单价", "unit_price", "美元。单价 × 数量用于匹配买家账号的金额区间"),
    ("下单时间", "order_time", "原订单下单时间，按北京时间，如 2026/10/27 21:31"),
    ("配送天数", "ship_days", "审核时采集的配送天数；缺建议采购日期时按 下单日期 + 6 − 配送天数 计算"),
    ("建议采购日期", "suggested_purchase_date", "如 2026-10-31；早于或等于当天才会执行"),
    ("店铺", "store_name", "展示用"),
    ("采购订单号", "sales_po_no", "展示与对账"),
    ("SKU", "sku", "展示与对账"),
    ("销售总额", "sales_total", "美元，展示用"),
    ("售后状态", "after_sale_value", "上游取消信号（取值在准入规则里设置）"),
    ("备注", None, "仅保留在来源快照，不参与采购"),
]

STANDARD_HEADERS: list[str] = [h for h, _, _ in STANDARD_COLUMNS]

# ── 别名（系统字段 → 其他常见表头）─────────────────────────────────────────────
# 同一个别名只能属于一个系统字段（测试会检查）。

ALIASES: dict[str, list[str]] = {
    "upstream_order_no": ["平台订单号", "订单号", "上游订单号", "平台单号"],
    "order_line_id": ["order_line_id", "订单行ID", "订单行 ID"],
    "store_name": ["店铺账号", "店铺名称", "店铺名"],
    "sales_po_no": ["PO号", "PO 号", "销售采购单号"],
    "sku": ["商品SKU"],
    "asin": ["来源码", "商品ASIN"],
    "quantity": ["数量1", "采购数量", "Qty", "Quantity"],
    "unit_price": ["单价", "采购价"],
    "sales_total": ["销售金额"],
    "price_cap": ["限价USD", "最高限价"],
    "order_time": ["订单时间", "下单日期"],
    "ship_days": ["配送时长", "运输天数"],
    "suggested_purchase_date": ["采购日期", "计划采购日期"],
    "ship_method": ["配送方式", "物流方式"],
    "recipient_name": ["收件人", "收件人姓名", "收货人", "Recipient Name", "Ship To Name"],
    "address1": ["地址1", "收件人地址", "收货地址", "Address1", "Address Line 1"],
    "address2": ["收件人地址2", "收货人地址2", "Address2", "Address Line 2"],
    "city": ["收件人城市", "城市", "City"],
    "state": ["收件人州/省", "州/省", "州", "State"],
    "postal_code": ["收件人邮编", "邮编/Zip", "邮政编码", "Zip", "Zip Code", "Postal Code"],
    "phone": ["收件人电话", "电话", "联系电话", "Phone"],
    "country": ["国家", "收件人国家", "Country"],
    "district": ["收件人区", "区"],
    "review_value": ["审核", "人审", "审核结果", "人工审核结果"],
    "purchaser": ["采购方"],
    "purchase_status_value": ["采购状态"],
    "after_sale_value": ["售后", "售后信息"],
    "external_amz_order_no": ["AMZ订单号", "亚马逊订单号", "Amazon订单号", "Amazon Order ID", "Amazon Order No"],
    "external_buyer_account": ["买家号1", "买家账号"],
    "external_buyer_customer_id": ["买家号id", "买家号 ID", "买家ID"],
}


def _norm(s: str) -> str:
    s = re.sub(r"\s+", "", str(s)).casefold()
    s = s.replace("／", "/").replace("（", "(").replace("）", ")")
    return s.strip("*＊")


def _build_lookup() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    exact = {h: k for h, k, _ in STANDARD_COLUMNS if k}
    alias: dict[str, str] = {}
    for key, names in ALIASES.items():
        for n in names:
            if n not in exact:
                alias.setdefault(n, key)
    loose: dict[str, str] = {}
    for name, key in list(exact.items()) + list(alias.items()):
        loose.setdefault(_norm(name), key)
    for key in SYSTEM_FIELD_KEYS:
        loose.setdefault(_norm(key), key)
    return exact, alias, loose


_EXACT, _ALIAS, _LOOSE = _build_lookup()


def suggest_mapping(headers: list[str]) -> dict[str, str | None]:
    """输入：文件表头（按列序）→ 输出：{表头: 系统字段 | None}，每个系统字段至多对应一列。"""
    result: dict[str, str | None] = {h: None for h in headers}
    used: set[str] = set()
    for lookup in (lambda h: _EXACT.get(h), lambda h: _ALIAS.get(h), lambda h: _LOOSE.get(_norm(h))):
        for h in headers:
            if result[h] is not None or not isinstance(h, str):
                continue
            key = lookup(h)
            if key and key not in used:
                result[h] = key
                used.add(key)
    return result


def validate_mapping(mapping: dict[str, str | None]) -> None:
    """输入：{表头: 系统字段 | None} → 输出：无；有未知系统字段、同一字段对应多列或没对应平台订单号时抛 errors.validation。"""
    seen: dict[str, str] = {}
    for header, key in mapping.items():
        if not key:
            continue
        if key not in SYSTEM_FIELD_KEYS:
            raise errors.validation(f"字段对应里有未知的系统字段：{key}", header=header, key=key)
        if key in seen:
            raise errors.validation(f"「{seen[key]}」和「{header}」对应到了同一个系统字段", key=key)
        seen[key] = header
    if "upstream_order_no" not in seen:
        raise errors.validation("请为「平台订单号」选择一列")


def normalize_row(raw: dict[str, Any], mapping: dict[str, str | None],
                  admission: dict[str, Any] | None = None) -> UpstreamRecord:
    """输入：一行 {表头: 原值}、{表头: 系统字段 | None}、准入规则（缺省用 DEFAULT_ADMISSION）→ 输出：UpstreamRecord。

    同一系统字段对应多列时取第一个非空值；未知系统字段抛 errors.validation。规范化规则见 normalize.normalize。
    """
    values: dict[str, Any] = {}
    for header, key in mapping.items():
        if not key:
            continue
        if key not in SYSTEM_FIELD_KEYS:
            raise errors.validation(f"字段对应里有未知的系统字段：{key}", header=header, key=key)
        if _blank(values.get(key)):
            values[key] = raw.get(header)
    return normalize(values, admission)


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def template_xlsx() -> bytes:
    """输入：无 → 输出：标准模板 xlsx 字节：「采购订单」只有表头（无示例数据），「说明」逐列写明是否必填与说明。"""
    from services.sheet_io import write_xlsx

    notes = [[h, "必填" if k == "upstream_order_no" else "选填", note] for h, k, note in STANDARD_COLUMNS]
    text_cols = {"平台订单号", "邮编", "收货人电话", "AMZ订单号", "买家号ID", "ASIN", "采购订单号", "SKU"}
    return write_xlsx([
        ("采购订单", STANDARD_HEADERS, []),
        ("说明", ["列名", "是否必填", "说明"], notes),
    ], text_headers=text_cols)
