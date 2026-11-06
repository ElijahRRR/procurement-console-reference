"""内存里的假飞书客户端：方法名、参数与返回形状同 api.feishu.FeishuClient，供服务层与 HTTP 测试使用。

- 数据：apps[app_token][table_id] = {name, fields: [字段定义], records: {record_id: fields}, views: {view_id: [record_id] | None}}；
  记录的 fields 按 records/search 的**读取形状**保存（文本为片段数组、日期为毫秒时间戳、单选为文本……）。
- 写入（batch_update_records）先整体校验再应用（与飞书一样一批要么全成要么全败）：记录与列必须存在、
  列必须可写、值类型必须与列类型相符（数字列只收数字等），否则抛 FeishuError；写入的值转成读取形状保存。
- 注入错误：fail_next('search_records', FeishuError(...), times=1)；ignore_writes_to = {'列名'} 模拟「写入被接受
  但读回不一致」（例如表里的自动化把值改回去了）。
- 调用记录：calls（方法名与主要参数）、updates（每次 batch_update 的 {app_token, table_id, records}）。

全部数据虚构：DEMO 名称、假地址、假订单号；不含任何真实链接或 token。
"""

from __future__ import annotations

import copy
import itertools
import json
from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from api.feishu import FeishuError
from services import clock

# ── 字段类型（与 services/feishu_fields 一致）─────────────────────────────────────

TEXT, NUMBER, SINGLE_SELECT, MULTI_SELECT, DATETIME, CHECKBOX = 1, 2, 3, 4, 5, 7
USER, PHONE, URL, ATTACHMENT, SINGLE_LINK, LOOKUP, FORMULA = 11, 13, 15, 17, 18, 19, 20

_UI_TYPE = {TEXT: "Text", NUMBER: "Number", SINGLE_SELECT: "SingleSelect", MULTI_SELECT: "MultiSelect",
            DATETIME: "DateTime", CHECKBOX: "Checkbox", USER: "User", PHONE: "Phone", URL: "Url",
            ATTACHMENT: "Attachment", SINGLE_LINK: "SingleLink", LOOKUP: "Lookup", FORMULA: "Formula"}
_READONLY_TYPES = frozenset({ATTACHMENT, SINGLE_LINK, LOOKUP, FORMULA, 21, 1001, 1002, 1003, 1004, 1005, 3001})

# ── 虚构的表格定位（只用于测试）──────────────────────────────────────────────────

DEMO_APP = "bascnDEMOAPP0001"
DEMO_TABLE = "tblDEMOORD001"
DEMO_RESULT_TABLE = "tblDEMORES002"
DEMO_VIEW = "vewDEMOVIEW01"
DEMO_LINK = f"https://example-demo.feishu.cn/base/{DEMO_APP}?table={DEMO_TABLE}&view={DEMO_VIEW}"
DEMO_WIKI_TOKEN = "wikcnDEMOWIKI0001"
DEMO_WIKI_LINK = f"https://example-demo.feishu.cn/wiki/{DEMO_WIKI_TOKEN}?table={DEMO_TABLE}"
DEMO_PURCHASER = "自采购DEMO"

#: 与所有者「订单中心」默认对应同名的列（类型为合理推测）；「总成本USD」是公式列，不能写回
ORDER_TABLE_FIELDS: list[tuple[str, int]] = [
    ("平台订单号", TEXT), ("order_line_id", TEXT), ("店铺", SINGLE_SELECT), ("采购订单号", TEXT), ("SKU", TEXT),
    ("销售总额", NUMBER), ("采购单价", NUMBER), ("数量", NUMBER), ("限价", NUMBER), ("下单时间", DATETIME),
    ("建议采购日期", DATETIME), ("配送方式", SINGLE_SELECT), ("收货人姓名", TEXT), ("收货人地址", TEXT),
    ("地址2", TEXT), ("收货人城市", TEXT), ("收货人州/省", TEXT), ("邮编", TEXT), ("收货人电话", PHONE),
    ("国家", SINGLE_SELECT), ("ASIN", TEXT), ("人工审核", SINGLE_SELECT), ("采购方", SINGLE_SELECT),
    ("采购状态", SINGLE_SELECT), ("售后状态", SINGLE_SELECT), ("AMZ订单号", TEXT), ("买家号", TEXT),
    ("买家号id", TEXT), ("税前总额USD", NUMBER), ("运费USD", NUMBER), ("税费USD", NUMBER), ("付款卡号", TEXT),
    ("总成本USD", FORMULA), ("备注", TEXT),
]
_OPTIONS = {"采购状态": ["待拍单", "已拍单", "拿回"], "人工审核": ["通过", "拒绝", "建议拒绝", "钓鱼"],
            "采购方": [DEMO_PURCHASER, "外采购DEMO"], "售后状态": ["正常", "已取消"], "国家": ["US"],
            "配送方式": ["FBA", "FBM"], "店铺": ["STORE-DEMO"]}


def text(s: str) -> list[dict[str, str]]:
    """输入：文本 → 输出：飞书文本列的读取形状（片段数组）。"""
    return [{"type": "text", "text": s}]


def ms(dt: datetime) -> int:
    """输入：带时区的时刻 → 输出：毫秒时间戳（飞书日期列的读取 / 写入形状）。"""
    return int(dt.timestamp() * 1000)


def bj_date_ms(d: date) -> int:
    """输入：北京日期 → 输出：北京当天 00:00 的毫秒时间戳（飞书日期列存的就是它）。"""
    return ms(clock.bj_datetime(d))


def order_fields(no: str, **overrides: Any) -> dict[str, Any]:
    """输入：平台订单号、要覆盖的列（列名 → 读取形状的值；值为 None 表示该列为空）→ 输出：一条订单记录的 fields。

    缺省：人审「通过」、采购方 自采购DEMO、采购状态「待拍单」、单价 $25.00 × 1、限价 $40.00、
    建议采购日期 = 北京今天、虚构收件人 Demo Buyer。
    """
    tail = no.removeprefix("PO-")
    fields: dict[str, Any] = {
        "平台订单号": text(no), "order_line_id": text(f"OL-{tail}"), "店铺": "STORE-DEMO",
        "采购订单号": text(f"SPO-{tail}"), "SKU": text("SKU-DEMO"), "销售总额": 39.99, "采购单价": 25.0,
        "数量": 1, "限价": 40.0, "下单时间": ms(datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)),
        "建议采购日期": bj_date_ms(clock.bj_today()), "配送方式": "FBA", "收货人姓名": text("Demo Buyer"),
        "收货人地址": text("100 Demo Street"), "收货人城市": text("Demo City"), "收货人州/省": text("FL"),
        "邮编": text("32960"), "收货人电话": "555-0100", "国家": "US", "ASIN": text("B0DEMO0001"),
        "人工审核": "通过", "采购方": DEMO_PURCHASER, "采购状态": "待拍单",
    }
    for k, v in overrides.items():
        if v is None:
            fields.pop(k, None)
        else:
            fields[k] = v
    return fields


class FakeFeishuClient:
    """内存里的飞书多维表格（见模块说明）。"""

    def __init__(self) -> None:
        self.apps: dict[str, dict[str, dict[str, Any]]] = {}
        self.wiki: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.ignore_writes_to: set[str] = set()
        self.closed = 0
        self._errors: dict[str, list[Exception]] = {}
        self._field_seq = itertools.count(1)

    # ── 搭数据 ────────────────────────────────────────────────────────────────

    @classmethod
    def with_order_table(cls, app_token: str = DEMO_APP, table_id: str = DEMO_TABLE,
                         name: str = "采购订单DEMO") -> "FakeFeishuClient":
        """输入：app_token、table_id、表名 → 输出：带一张「订单中心」同名列空表的假客户端（另带一个知识库节点）。"""
        c = cls()
        c.add_table(app_token, table_id, name, ORDER_TABLE_FIELDS)
        c.add_view(app_token, table_id, DEMO_VIEW)
        c.wiki[DEMO_WIKI_TOKEN] = {"obj_token": app_token, "obj_type": "bitable", "title": "DEMO 订单中心"}
        return c

    @classmethod
    def from_fixture(cls, path: str | Path | None = None, app_token: str = DEMO_APP,
                     table_id: str = DEMO_TABLE) -> "FakeFeishuClient":
        """输入：tests/fixtures/feishu_demo_records.json 形状的文件 → 输出：装好这张表与记录的假客户端。"""
        p = Path(path) if path else Path(__file__).parent / "fixtures" / "feishu_demo_records.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        c = cls()
        c.apps.setdefault(app_token, {})[table_id] = {
            "name": "采购订单DEMO", "fields": copy.deepcopy(data["fields"]), "records": {}, "views": {}}
        for r in data["records"]:
            c.add_record(app_token, table_id, r["record_id"], r["fields"])
        return c

    def add_table(self, app_token: str, table_id: str, name: str,
                  fields: list[tuple[str, int]] | list[dict[str, Any]]) -> None:
        """输入：app_token、table_id、表名、字段（(列名, 类型) 或完整字段定义）→ 输出：无。"""
        defs: list[dict[str, Any]] = []
        for i, f in enumerate(fields):
            if isinstance(f, dict):
                defs.append(copy.deepcopy(f))
                continue
            fname, ftype = f
            prop = None
            if ftype in (SINGLE_SELECT, MULTI_SELECT):
                prop = {"options": [{"id": f"optDEMO{j}", "name": o, "color": 0}
                                    for j, o in enumerate(_OPTIONS.get(fname, []))]}
            defs.append({"field_id": f"fldDEMO{next(self._field_seq):06d}", "field_name": fname, "type": ftype,
                         "ui_type": _UI_TYPE.get(ftype, "Text"), "is_primary": i == 0, "property": prop})
        self.apps.setdefault(app_token, {})[table_id] = {"name": name, "fields": defs, "records": {}, "views": {}}

    def add_view(self, app_token: str, table_id: str, view_id: str, record_ids: list[str] | None = None) -> None:
        """输入：表、视图 id、视图里的记录（None = 全部）→ 输出：无。"""
        self._table(app_token, table_id)["views"][view_id] = record_ids

    def add_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        """输入：表、记录 id、读取形状的 fields → 输出：无（已存在则整条替换）。"""
        self._table(app_token, table_id)["records"][record_id] = copy.deepcopy(fields)

    def edit_record(self, app_token: str, table_id: str, record_id: str, **changes: Any) -> None:
        """输入：表、记录 id、列名 → 读取形状的新值（None = 清空）→ 输出：无。模拟有人在飞书里改了这条记录。"""
        rec = self._table(app_token, table_id)["records"][record_id]
        for k, v in changes.items():
            if v is None:
                rec.pop(k, None)
            else:
                rec[k] = copy.deepcopy(v)

    def record(self, app_token: str, table_id: str, record_id: str) -> dict[str, Any]:
        """输入：表、记录 id → 输出：当前 fields（读取形状，副本）。测试断言用，不记调用。"""
        return copy.deepcopy(self._table(app_token, table_id)["records"][record_id])

    def fail_next(self, method: str, error: Exception | None = None, times: int = 1) -> None:
        """输入：方法名、要抛的异常（缺省为飞书 1254302 无权限）、次数 → 输出：无（之后 times 次调用抛它）。"""
        err = error or FeishuError(1254302, "Permission denied (DEMO)")
        self._errors.setdefault(method, []).extend([err] * times)

    # ── 与 FeishuClient 相同的接口 ─────────────────────────────────────────────

    def close(self) -> None:
        """输入：无 → 输出：无（只计数，假客户端可继续使用）。"""
        self.closed += 1

    def __enter__(self) -> "FakeFeishuClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def list_tables(self, app_token: str) -> list[dict[str, Any]]:
        """输入：app_token → 输出：[{table_id, name}]；多维表格不存在抛 FeishuError(1254040)。"""
        self._enter("list_tables", app_token=app_token)
        app = self.apps.get(app_token)
        if app is None:
            raise FeishuError(1254040, "BaseTokenNotFound", 404)
        return [{"table_id": tid, "name": t["name"]} for tid, t in app.items()]

    def list_fields(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        """输入：app_token、table_id → 输出：[{field_id, field_name, type, ui_type, is_primary, property}]。"""
        self._enter("list_fields", app_token=app_token, table_id=table_id)
        return [{k: copy.deepcopy(f.get(k)) for k in ("field_id", "field_name", "type", "ui_type", "is_primary",
                                                      "property")}
                for f in self._table(app_token, table_id)["fields"]]

    def search_records(self, app_token: str, table_id: str, *, field_names: list[str] | None = None,
                       view_id: str | None = None, page_size: int = 500) -> Iterator[dict[str, Any]]:
        """输入：表、只读的列、视图 → 输出：逐条 {record_id, fields}；列或视图不存在抛 FeishuError。"""
        self._enter("search_records", app_token=app_token, table_id=table_id, field_names=list(field_names or []),
                    view_id=view_id)
        t = self._table(app_token, table_id)
        names = {f["field_name"] for f in t["fields"]}
        for n in field_names or []:
            if n not in names:
                raise FeishuError(1254045, f"FieldNameNotFound: {n}", 200)
        ids = list(t["records"])
        if view_id is not None:
            if view_id not in t["views"]:
                raise FeishuError(1254042, "ViewIdNotFound", 200)
            only = t["views"][view_id]
            if only is not None:
                ids = [i for i in ids if i in only]
        return self._iter_records(t, ids, field_names)

    @staticmethod
    def _iter_records(t: dict[str, Any], ids: list[str], field_names: list[str] | None) -> Iterator[dict[str, Any]]:
        for rid in ids:
            rec = t["records"].get(rid)
            if rec is None:
                continue
            fields = {k: copy.deepcopy(v) for k, v in rec.items() if not field_names or k in field_names}
            yield {"record_id": rid, "fields": fields}

    def get_record(self, app_token: str, table_id: str, record_id: str) -> dict[str, Any]:
        """输入：表、记录 id → 输出：{record_id, fields}；记录不存在抛 FeishuError(1254043)。"""
        self._enter("get_record", app_token=app_token, table_id=table_id, record_id=record_id)
        rec = self._table(app_token, table_id)["records"].get(record_id)
        if rec is None:
            raise FeishuError(1254043, "RecordIdNotFound", 200)
        return {"record_id": record_id, "fields": copy.deepcopy(rec)}

    def batch_update_records(self, app_token: str, table_id: str,
                             records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """输入：表、[{record_id, fields}] → 输出：更新后的记录；先整批校验（记录、列、可写、类型）再应用。"""
        self._enter("batch_update_records", app_token=app_token, table_id=table_id,
                    record_ids=[r.get("record_id") for r in records])
        t = self._table(app_token, table_id)
        types = {f["field_name"]: f["type"] for f in t["fields"]}
        staged: list[tuple[str, dict[str, Any]]] = []
        for r in records:
            rid = r.get("record_id")
            if rid not in t["records"]:
                raise FeishuError(1254043, f"RecordIdNotFound: {rid}", 200)
            converted: dict[str, Any] = {}
            for col, v in (r.get("fields") or {}).items():
                if col not in types:
                    raise FeishuError(1254045, f"FieldNameNotFound: {col}", 200)
                converted[col] = _stored_value(col, v, types[col])
            staged.append((rid, converted))
        self.updates.append({"app_token": app_token, "table_id": table_id, "records": copy.deepcopy(records)})
        out = []
        for rid, converted in staged:
            rec = t["records"][rid]
            for col, v in converted.items():
                if col in self.ignore_writes_to:
                    continue
                if v is None:
                    rec.pop(col, None)
                else:
                    rec[col] = v
            out.append({"record_id": rid, "fields": copy.deepcopy(rec)})
        return out

    def wiki_get_node(self, token: str) -> dict[str, Any]:
        """输入：知识库节点 token → 输出：{obj_token, obj_type, title}；不存在抛 FeishuError(131005)。"""
        self._enter("wiki_get_node", token=token)
        node = self.wiki.get(token)
        if node is None:
            raise FeishuError(131005, "node not found", 200)
        return dict(node)

    # ── 内部 ──────────────────────────────────────────────────────────────────

    def _enter(self, method: str, **args: Any) -> None:
        self.calls.append((method, args))
        queue = self._errors.get(method)
        if queue:
            raise queue.pop(0)

    def _table(self, app_token: str, table_id: str) -> dict[str, Any]:
        app = self.apps.get(app_token)
        if app is None:
            raise FeishuError(1254040, "BaseTokenNotFound", 404)
        t = app.get(table_id)
        if t is None:
            raise FeishuError(1254041, "TableIdNotFound", 200)
        return t

    def calls_of(self, method: str) -> list[dict[str, Any]]:
        """输入：方法名 → 输出：该方法每次被调用时的参数。"""
        return [a for m, a in self.calls if m == method]


def _stored_value(col: str, v: Any, ftype: int) -> Any:
    """写入值 → 读取形状；类型不符抛 FeishuError（与飞书的 *FieldConvFail 相同语义）。"""
    if ftype in _READONLY_TYPES:
        raise FeishuError(1254027, f"UpdateFieldFail: 列「{col}」不可编辑", 200)
    if v is None:
        return None
    if ftype == NUMBER:
        if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
            raise FeishuError(1254061, f"NumberFieldConvFail: {col}", 200)
        return v
    if ftype == DATETIME:
        if isinstance(v, bool) or not isinstance(v, int):
            raise FeishuError(1254064, f"DatetimeFieldConvFail: {col}", 200)
        return v
    if ftype == CHECKBOX:
        if not isinstance(v, bool):
            raise FeishuError(1254066, f"CheckboxFieldConvFail: {col}", 200)
        return v
    if ftype == MULTI_SELECT:
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise FeishuError(1254063, f"MultiSelectFieldConvFail: {col}", 200)
        return list(v)
    if ftype == URL:
        if not isinstance(v, dict):
            raise FeishuError(1254068, f"URLFieldConvFail: {col}", 200)
        return {"link": v.get("link"), "text": v.get("text")}
    if not isinstance(v, str):
        raise FeishuError(1254060, f"TextFieldConvFail: {col}", 200)
    if ftype in (SINGLE_SELECT, PHONE):
        return v
    return text(v)


# ── 测试辅助：建一个指向假表的连接 ─────────────────────────────────────────────────


def demo_connection_data(client: FakeFeishuClient, **overrides: Any) -> dict[str, Any]:
    """输入：假客户端、要覆盖的连接参数 → 输出：feishu_connections.create 的参数（字段对应与写回对应按表头自动识别，
    准入只接 自采购DEMO，读取整张表不限视图）。"""
    from services import feishu_fields

    fields = client.list_fields(DEMO_APP, DEMO_TABLE)
    data: dict[str, Any] = {
        "name": "DEMO 订单中心", "link": DEMO_LINK, "app_token": DEMO_APP, "source_table_id": DEMO_TABLE,
        "source_view_id": None,
        "field_map": feishu_fields.suggest_field_map([f["field_name"] for f in fields]),
        "admission": {"purchaser_values": [DEMO_PURCHASER]},
        "writeback_map": feishu_fields.suggest_writeback_map(fields),
    }
    data.update(overrides)
    return data


def create_demo_connection(conn, ws_id: int, client: FakeFeishuClient, user_id: int | None = None,
                           **overrides: Any) -> dict[str, Any]:
    """输入：连接、工作区、假客户端、操作人、要覆盖的连接参数 → 输出：feishu_connections.create 的结果（连接形状）。"""
    from services import feishu_connections

    return feishu_connections.create(conn, ws_id, demo_connection_data(client, **overrides), user_id, client=client)
