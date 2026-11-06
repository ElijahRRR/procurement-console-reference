"""飞书多维表格的纯函数部分：链接解析、单元格取值、字段对应、写回值构造。不碰数据库，也不发请求。

- 链接：``https://<租户>.feishu.cn/base/<app_token>?table=<table_id>&view=<view_id>``，或 ``/wiki/<token>``
  （知识库节点，需再调 api.feishu.wiki_get_node 换出 obj_token）。也接受 ``*.larksuite.com``。
- 单元格：records/search 返回的 fields 是「列名 → 值」，值的形状随字段类型变化（见 cell_to_python）。
- 字段对应 field_map：``{系统字段: 飞书列名 | [候选列名, ...]}``，候选列表按顺序取第一个非空值。
- 写回对应 writeback_map：``{结果字段: 飞书列名}``；写回目标不得是公式、查找引用、关联、附件与系统列。

飞书字段类型（type）：1 文本、2 数字（含货币 / 进度 / 评分）、3 单选、4 多选、5 日期、7 复选框、11 人员、
13 电话、15 超链接、17 附件、18 单向关联、19 查找引用、20 公式、21 双向关联、22 地理位置、23 群组、
1001 创建时间、1002 最后更新时间、1003 创建人、1004 修改人、1005 自动编号、3001 按钮。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

from services import clock, errors
from services.models import UpstreamRecord, money
from services.normalize import SYSTEM_FIELD_KEYS, normalize, parse_datetime_text

# ── 字段类型 ─────────────────────────────────────────────────────────────────

TEXT, NUMBER, SINGLE_SELECT, MULTI_SELECT, DATETIME, CHECKBOX = 1, 2, 3, 4, 5, 7
USER, PHONE, URL, ATTACHMENT, SINGLE_LINK, LOOKUP, FORMULA, DUPLEX_LINK = 11, 13, 15, 17, 18, 19, 20, 21
LOCATION, GROUP_CHAT = 22, 23
CREATED_TIME, MODIFIED_TIME, CREATED_USER, MODIFIED_USER, AUTO_NUMBER, BUTTON = 1001, 1002, 1003, 1004, 1005, 3001

FIELD_TYPE_ZH: dict[int, str] = {
    TEXT: "文本", NUMBER: "数字", SINGLE_SELECT: "单选", MULTI_SELECT: "多选", DATETIME: "日期",
    CHECKBOX: "复选框", USER: "人员", PHONE: "电话号码", URL: "超链接", ATTACHMENT: "附件",
    SINGLE_LINK: "单向关联", LOOKUP: "查找引用", FORMULA: "公式", DUPLEX_LINK: "双向关联",
    LOCATION: "地理位置", GROUP_CHAT: "群组", CREATED_TIME: "创建时间", MODIFIED_TIME: "最后更新时间",
    CREATED_USER: "创建人", MODIFIED_USER: "修改人", AUTO_NUMBER: "自动编号", BUTTON: "按钮",
}

_DATE_TYPES = frozenset({DATETIME, CREATED_TIME, MODIFIED_TIME})

#: 不能作为写回目标的字段类型（飞书计算或系统维护的列）
NON_WRITABLE_TYPES = frozenset({ATTACHMENT, SINGLE_LINK, LOOKUP, FORMULA, DUPLEX_LINK,
                                CREATED_TIME, MODIFIED_TIME, CREATED_USER, MODIFIED_USER, AUTO_NUMBER, BUTTON})
#: 可写、但写回值（文本 / 数字 / 日期）无法填入的类型
_UNSUPPORTED_WRITE_TYPES = frozenset({USER, LOCATION, GROUP_CHAT})
_NON_WRITABLE_UI_TYPES = frozenset({"Formula", "Lookup", "SingleLink", "DuplexLink", "Attachment", "CreatedTime",
                                    "ModifiedTime", "CreatedUser", "ModifiedUser", "AutoNumber", "Button"})

# ── 链接解析 ─────────────────────────────────────────────────────────────────

_HOST_SUFFIXES = ("feishu.cn", "larksuite.com")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_TABLE_RE = re.compile(r"^tbl[A-Za-z0-9]{4,40}$")
_VIEW_RE = re.compile(r"^vew[A-Za-z0-9]{4,40}$")


def parse_link(link: str) -> dict[str, Any]:
    """输入：用户粘贴的飞书多维表格 / 知识库链接 → 输出：{kind: 'base'|'wiki', token, table_id, view_id}。

    table_id / view_id 取自查询参数 table= / view=，缺省为 None。链接不合法抛 errors.validation
    （错误信息里不回显链接本身，链接属于私人信息）。
    """
    s = (link or "").strip()
    if not s:
        raise errors.validation("请粘贴飞书多维表格链接")
    if "://" not in s:
        s = "https://" + s
    try:
        u = urlsplit(s)
        host = (u.hostname or "").lower()
    except ValueError:
        raise errors.validation("链接格式不对") from None
    if u.scheme not in ("https", "http") or not any(host == h or host.endswith("." + h) for h in _HOST_SUFFIXES):
        raise errors.validation("只支持 feishu.cn 或 larksuite.com 的多维表格链接")
    parts = [p for p in u.path.split("/") if p]
    if parts and parts[0] == "share":
        raise errors.validation("这是分享链接，请在多维表格里用「复制链接」得到的地址")
    if len(parts) < 2 or parts[0] not in ("base", "wiki"):
        raise errors.validation("不是多维表格链接：地址里应包含 /base/ 或 /wiki/")
    kind, token = parts[0], parts[1]
    if not _TOKEN_RE.match(token):
        raise errors.validation("链接里的表格标识格式不对")
    qs = parse_qs(u.query)
    table_id = (qs.get("table") or [None])[0] or None
    view_id = (qs.get("view") or [None])[0] or None
    if table_id is not None and not _TABLE_RE.match(table_id):
        raise errors.validation("链接里的数据表标识（table=）格式不对")
    if view_id is not None and not _VIEW_RE.match(view_id):
        raise errors.validation("链接里的视图标识（view=）格式不对")
    return {"kind": kind, "token": token, "table_id": table_id, "view_id": view_id}


# ── 单元格取值 ───────────────────────────────────────────────────────────────


def cell_to_python(value: Any, field_type: int | None = None) -> Any:
    """输入：records/search 返回的单元格值、该列字段类型 → 输出：str / int / float / bool / datetime(UTC) / list[str] / None。

    - 文本片段数组 ``[{type:'text', text}, {type:'mention', text}, {type:'url', text, link}]`` → 拼接后的文本；
    - 数字 → 原数字；单选 → 文本；多选 → 文本列表；复选框 → bool；
    - 日期 / 创建时间 / 更新时间（毫秒时间戳）→ 带时区的 UTC datetime；
    - 人员 / 群组 / 附件 → 名称（多个用「, 」连接）；关联 → 关联记录的文本；超链接 ``{link, text}`` → text（无则 link）；
    - 公式 / 查找引用 ``{type: 内部类型, value: [...]}`` → 按内部类型递归取值，单个值取出，多个值保留列表
      （查找引用列引用了多条记录时，各文本片段各算一个值，不拼接；公式列的片段照常拼接）；
    - 空值、空列表 → None。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if field_type in _DATE_TYPES:
            return _ms_to_dt(value)
        return value
    if isinstance(value, str):
        if field_type in _DATE_TYPES and re.fullmatch(r"\d{11,14}", value.strip()):
            return _ms_to_dt(int(value.strip()))
        return value
    if isinstance(value, dict):
        return _dict_to_python(value, field_type)
    if isinstance(value, (list, tuple)):
        return _list_to_python(list(value), field_type)
    return str(value)


def _ms_to_dt(ms: float) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)


def _inner_type(t: Any) -> int | None:
    try:
        return int(t)
    except (TypeError, ValueError):
        return None


def _dict_to_python(v: dict[str, Any], field_type: int | None) -> Any:
    if "value" in v and "type" in v:                       # 公式 / 查找引用
        inner = v.get("value")
        if field_type == LOOKUP and isinstance(inner, list):
            segs = [x for x in inner if x is not None and x != ""]
            if len(segs) > 1 and all(_is_segment(x) for x in segs):
                # 查找引用引用了多条记录：每个片段是一个值，不拼成一个文本（拼起来会造出不存在的订单号或 ASIN）。
                # 公式列（如「平台订单号 = 采购订单号 & SKU」）的文本片段仍是同一个值，照常拼接。
                texts = [str(x.get("text") or "").strip() for x in segs]
                texts = [t for t in texts if t]
                return texts if len(texts) > 1 else (texts[0] if texts else None)
        return cell_to_python(inner, _inner_type(v.get("type")))
    if "text_arr" in v and isinstance(v.get("text_arr"), list):
        return _join([str(x) for x in v["text_arr"] if x not in (None, "")]) or v.get("text") or None
    if "text" in v or "link" in v:                         # 超链接 / 单个文本片段
        return (v.get("text") or v.get("link") or None)
    if "link_record_ids" in v:                             # 关联（只有记录 id）
        return _join([str(x) for x in v.get("link_record_ids") or []])
    if "full_address" in v or "location" in v:             # 地理位置
        return v.get("full_address") or v.get("name") or v.get("location") or None
    if "name" in v:                                        # 人员 / 群组 / 附件
        return v.get("name") or None
    if "users" in v and isinstance(v["users"], list):      # 部分接口的人员包装
        return _list_to_python(v["users"], USER)
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _is_segment(x: Any) -> bool:
    return isinstance(x, dict) and "text" in x and "name" not in x and "text_arr" not in x


def _list_to_python(items: list[Any], field_type: int | None) -> Any:
    items = [x for x in items if x is not None and x != ""]
    if not items:
        return None
    if all(_is_segment(x) for x in items):                 # 同一格里的文本片段：直接拼接
        text = "".join(str(x.get("text") or "") for x in items)
        return text or None
    if all(isinstance(x, dict) for x in items):            # 人员、附件、关联记录、嵌套公式……
        parts = [p for p in (cell_to_python(x, field_type) for x in items) if p not in (None, "")]
        if len(parts) == 1:
            return parts[0]
        return _join([_scalar_text(p) for p in parts]) or None
    converted = [cell_to_python(x, field_type) for x in items]
    converted = [c for c in converted if c not in (None, "")]
    if not converted:
        return None
    if field_type == MULTI_SELECT:
        return [_scalar_text(c) for c in converted]
    if len(converted) == 1:
        return converted[0]
    return converted


def _scalar_text(v: Any) -> str:
    if isinstance(v, list):
        return _join([_scalar_text(x) for x in v])
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _join(parts: list[str]) -> str | None:
    parts = [p for p in parts if p]
    return ", ".join(parts) if parts else None


def _is_empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, tuple)):
        return all(_is_empty(x) for x in v)
    return False


def field_types_of(fields: list[dict[str, Any]]) -> dict[str, int]:
    """输入：api.feishu.list_fields 的结果 → 输出：{列名: 字段类型}。"""
    return {f["field_name"]: f.get("type") for f in fields if f.get("field_name")}


def snapshot_fields(fields: dict[str, Any], field_types: dict[str, int] | None = None) -> dict[str, Any]:
    """输入：一条记录的 fields、列类型 → 输出：{列名: 可写入 jsonb 的可读值}（来源快照用；时刻转 ISO 字符串）。"""
    types = field_types or {}
    out: dict[str, Any] = {}
    for col, raw in fields.items():
        v = cell_to_python(raw, types.get(col))
        out[col] = _jsonable(v)
    return out


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


# ── 字段对应（来源 → 系统字段）────────────────────────────────────────────────

#: 所有者「订单中心」采购订单视图的默认对应（方案 v2 §3；列名均来自真实表头）。
#: 表里没有「配送天数」「收货人区」两列，所以 ship_days / district 缺省不对应。
DEFAULT_FIELD_MAP: dict[str, str | list[str]] = {
    "upstream_order_no": "平台订单号",
    "order_line_id": "order_line_id",
    "store_name": ["店铺", "店铺账号"],
    "sales_po_no": "采购订单号",
    "sku": "SKU",
    "asin": ["ASIN", "来源码"],
    "quantity": ["数量", "数量1"],
    "unit_price": "采购单价",
    "sales_total": "销售总额",
    "price_cap": "限价",
    "order_time": "下单时间",
    "suggested_purchase_date": "建议采购日期",
    "ship_method": "配送方式",
    "recipient_name": "收货人姓名",
    "address1": ["收货人地址", "地址1"],
    "address2": "地址2",
    "city": "收货人城市",
    "state": "收货人州/省",
    "postal_code": "邮编",
    "phone": "收货人电话",
    "country": "国家",
    "review_value": "人工审核",
    "purchaser": "采购方",
    "purchase_status_value": "采购状态",
    "after_sale_value": "售后状态",
    "external_amz_order_no": "AMZ订单号",
    "external_buyer_account": ["买家号", "买家号1"],
    "external_buyer_customer_id": "买家号id",
}

#: 自动识别用的候选列名（按优先级）：默认表头 + 其他表格的常见写法
FIELD_CANDIDATES: dict[str, list[str]] = {
    "upstream_order_no": ["平台订单号"],
    "order_line_id": ["order_line_id", "订单行ID"],
    "store_name": ["店铺", "店铺账号", "店铺名称"],
    "sales_po_no": ["采购订单号"],
    "sku": ["SKU"],
    "asin": ["ASIN", "来源码"],
    "quantity": ["数量", "数量1", "采购数量"],
    "unit_price": ["采购单价"],
    "sales_total": ["销售总额"],
    "price_cap": ["限价"],
    "order_time": ["下单时间"],
    "ship_days": ["配送天数"],
    "suggested_purchase_date": ["建议采购日期"],
    "ship_method": ["配送方式"],
    "recipient_name": ["收货人姓名", "收件人"],
    "address1": ["收货人地址", "地址1"],
    "address2": ["地址2"],
    "city": ["收货人城市"],
    "state": ["收货人州/省"],
    "postal_code": ["邮编"],
    "phone": ["收货人电话"],
    "country": ["国家", "收货人国家"],
    "district": ["收货人区"],
    "review_value": ["人工审核"],
    "purchaser": ["采购方"],
    "purchase_status_value": ["采购状态"],
    "after_sale_value": ["售后状态"],
    "external_amz_order_no": ["AMZ订单号", "亚马逊订单号", "Amazon订单号"],
    "external_buyer_account": ["买家号", "买家号1"],
    "external_buyer_customer_id": ["买家号id", "买家号ID"],
}


def _norm_name(s: str) -> str:
    """列名宽松比较：去掉所有空白、统一大小写与全角斜杠 / 括号。"""
    s = re.sub(r"\s+", "", str(s)).casefold()
    return s.replace("／", "/").replace("（", "(").replace("）", ")")


def _pick_columns(candidates: list[str], names: list[str], used: set[str]) -> list[str]:
    """候选名 → 表里实际存在且未被占用的列（先精确，后宽松），保持候选顺序、去重。"""
    by_norm: dict[str, str] = {}
    for n in names:
        by_norm.setdefault(_norm_name(n), n)
    picked: list[str] = []
    for cand in candidates:
        col = cand if cand in names else by_norm.get(_norm_name(cand))
        if col and col not in used and col not in picked:
            picked.append(col)
    return picked


def suggest_field_map(field_names: list[str]) -> dict[str, str | list[str]]:
    """输入：表里的全部列名 → 输出：建议的 field_map（找到多个候选列时给列表，按顺序回退；找不到的系统字段不出现）。

    每一列至多对应一个系统字段。对所有者的真实表头，结果与 DEFAULT_FIELD_MAP 相同。
    """
    names = [n for n in field_names if n]
    used: set[str] = set()
    out: dict[str, str | list[str]] = {}
    for key in SYSTEM_FIELD_KEYS:
        cols = _pick_columns(FIELD_CANDIDATES.get(key, []), names, used)
        if not cols:
            continue
        used.update(cols)
        out[key] = cols[0] if len(cols) == 1 else cols
    return out


def _candidates(cols: Any) -> list[str]:
    if cols is None:
        return []
    if isinstance(cols, str):
        return [cols] if cols else []
    return [c for c in cols if isinstance(c, str) and c]


def used_columns(field_map: dict[str, Any]) -> list[str]:
    """输入：field_map → 输出：对应里用到的全部飞书列名（去重、保持顺序；作 records/search 的 field_names）。"""
    out: list[str] = []
    for key, cols in field_map.items():
        if key not in SYSTEM_FIELD_KEYS:
            continue
        for c in _candidates(cols):
            if c not in out:
                out.append(c)
    return out


def record_to_values(fields: dict[str, Any], field_map: dict[str, Any],
                     field_types: dict[str, int] | None = None) -> dict[str, Any]:
    """输入：一条记录的 fields、field_map、{列名: 字段类型} → 输出：{系统字段: Python 值}。

    field_map 的值可以是列名或候选列名列表，取第一个非空值；都为空则为 None。未知系统字段忽略。
    """
    types = field_types or {}
    out: dict[str, Any] = {}
    for key, cols in field_map.items():
        if key not in SYSTEM_FIELD_KEYS:
            continue
        val = None
        for col in _candidates(cols):
            v = cell_to_python(fields.get(col), types.get(col))
            if not _is_empty(v):
                val = v
                break
        out[key] = val
    return out


def normalize_record(fields: dict[str, Any], field_map: dict[str, Any], field_types: dict[str, int] | None,
                     admission: dict[str, Any] | None) -> UpstreamRecord:
    """输入：一条记录的 fields、field_map、列类型、准入规则 → 输出：UpstreamRecord（见 normalize.normalize）。"""
    return normalize(record_to_values(fields, field_map, field_types), admission)


# ── 写回（结果字段 → 飞书列）────────────────────────────────────────────────────

RESULT_FIELDS: list[dict[str, str]] = [
    {"key": "buyer_account", "label": "买家号"},
    {"key": "buyer_customer_id", "label": "买家号 ID"},
    {"key": "amazon_order_no", "label": "AMZ订单号"},
    {"key": "purchase_status", "label": "采购状态"},
    {"key": "total_before_tax", "label": "税前总额"},
    {"key": "shipping", "label": "运费"},
    {"key": "tax", "label": "税费"},
    {"key": "total", "label": "总额"},
    {"key": "card_last4", "label": "卡尾号"},
]
RESULT_FIELD_KEYS: tuple[str, ...] = tuple(f["key"] for f in RESULT_FIELDS)

#: 默认写回列（方案 v2 §3，验收后才启用）。total 缺省不对应：「总成本USD」可能是公式或含头程的派生列。
DEFAULT_WRITEBACK_MAP: dict[str, str] = {
    "buyer_account": "买家号",
    "buyer_customer_id": "买家号id",
    "amazon_order_no": "AMZ订单号",
    "purchase_status": "采购状态",
    "total_before_tax": "税前总额USD",
    "shipping": "运费USD",
    "tax": "税费USD",
    "card_last4": "付款卡号",
}

WRITEBACK_CANDIDATES: dict[str, list[str]] = {
    "buyer_account": ["买家号"],
    "buyer_customer_id": ["买家号id", "买家号ID"],
    "amazon_order_no": ["AMZ订单号", "亚马逊订单号", "Amazon订单号"],
    "purchase_status": ["采购状态"],
    "total_before_tax": ["税前总额USD", "税前总额"],
    "shipping": ["运费USD", "运费"],
    "tax": ["税费USD", "税费"],
    "total": [],
    "card_last4": ["付款卡号", "卡尾号"],
}

#: 采购结果 → 「采购状态」列的取值
PURCHASE_STATUS_TEXT = {"succeeded": "已拍单", "success": "已拍单", "purchased": "已拍单"}


def writable(field: dict[str, Any]) -> bool:
    """输入：list_fields 的一项 {type, ui_type, ...} → 输出：能否作为写回目标列。

    公式、查找引用、关联、附件、创建 / 更新时间与人、自动编号、按钮不可写；人员、地理位置、群组
    无法填入文本或数字结果，也视为不可写。类型未知时不可写。
    """
    t = field.get("type")
    if t is None:
        return False
    try:
        t = int(t)
    except (TypeError, ValueError):
        return False
    if t in NON_WRITABLE_TYPES or t in _UNSUPPORTED_WRITE_TYPES:
        return False
    return field.get("ui_type") not in _NON_WRITABLE_UI_TYPES


def suggest_writeback_map(fields: list[dict[str, Any]]) -> dict[str, str]:
    """输入：list_fields 的结果 → 输出：建议的 writeback_map（只含表里存在且可写的列；每列至多一个结果字段）。"""
    writable_names = [f["field_name"] for f in fields if f.get("field_name") and writable(f)]
    used: set[str] = set()
    out: dict[str, str] = {}
    for key in RESULT_FIELD_KEYS:
        cols = _pick_columns(WRITEBACK_CANDIDATES.get(key, []), writable_names, used)
        if cols:
            out[key] = cols[0]
            used.add(cols[0])
    return out


def build_writeback_fields(result: dict[str, Any], writeback_map: dict[str, str],
                           field_types: dict[str, int] | None) -> dict[str, Any]:
    """输入：{结果字段: 值}、writeback_map、{列名: 字段类型} → 输出：batch_update 用的 {飞书列名: 值}。

    值为 None 的结果字段跳过（不清空飞书已有内容）。按目标列类型转换：数字列 → float、日期列 → 毫秒时间戳、
    单选 → 选项文本、多选 → [文本]、复选框 → bool、超链接 → {text, link}、其余 → 文本（金额两位小数）。
    purchase_status 为 succeeded / success / purchased 时写「已拍单」；card_last4 只写最后 4 位数字。
    目标列不可写或值无法转换时抛 errors.validation。
    """
    types = field_types or {}
    out: dict[str, Any] = {}
    for key, col in writeback_map.items():
        if not col or key not in RESULT_FIELD_KEYS:
            continue
        v = result.get(key)
        if v is None:
            continue
        if key == "purchase_status":
            v = PURCHASE_STATUS_TEXT.get(str(v).strip().lower(), v)
        if key == "card_last4":
            digits = re.sub(r"\D", "", str(v))
            if len(digits) < 4:
                continue
            v = digits[-4:]
        t = types.get(col)
        if t is not None and (int(t) in NON_WRITABLE_TYPES or int(t) in _UNSUPPORTED_WRITE_TYPES):
            raise errors.validation(f"飞书列「{col}」是{FIELD_TYPE_ZH.get(int(t), '系统')}列，不能作为写回目标",
                                    column=col, field_type=int(t))
        fv = _to_feishu_value(v, int(t) if t is not None else None, col)
        if fv is not None:
            out[col] = fv
    return out


def _to_feishu_value(v: Any, t: int | None, col: str) -> Any:
    if t == NUMBER:
        if isinstance(v, bool):
            raise errors.validation(f"飞书列「{col}」是数字列，写入值不是数字", column=col)
        if isinstance(v, int):
            return v
        d = money(v) if not isinstance(v, float) else money(Decimal(str(v)))
        if d is None:
            raise errors.validation(f"飞书列「{col}」是数字列，写入值不是数字", column=col)
        return float(d)
    if t in _DATE_TYPES:
        dt: datetime | None
        if isinstance(v, datetime):
            dt = v if v.tzinfo is not None else v.replace(tzinfo=clock.BJ_TZ)
        elif isinstance(v, date):
            dt = clock.bj_datetime(v)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
        else:
            dt = parse_datetime_text(str(v))
        if dt is None:
            raise errors.validation(f"飞书列「{col}」是日期列，写入值不是日期", column=col)
        return int(dt.timestamp() * 1000)
    if t == CHECKBOX:
        return bool(v)
    text = _to_text_value(v)
    if text is None:
        return None
    if t == MULTI_SELECT:
        return [text]
    if t == URL:
        return {"text": text, "link": text}
    return text


def _to_text_value(v: Any) -> str | None:
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, Decimal):
        return format(v.quantize(Decimal("0.01")), "f")
    if isinstance(v, float):
        d = money(Decimal(str(v)))
        return format(d, "f") if d is not None else None
    if isinstance(v, datetime):
        dt = v if v.tzinfo is not None else v.replace(tzinfo=clock.BJ_TZ)
        return dt.astimezone(clock.BJ_TZ).strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    return s or None
