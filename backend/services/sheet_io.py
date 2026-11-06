"""表格文件读写：上传的 .xlsx / .csv → 工作表（表头 + 行），以及导出 / 模板用的 xlsx 生成。不碰数据库。

读取规则：

- 只收 .xlsx（含 .xlsm）与 .csv，不超过 MAX_BYTES；.xls 提示另存为 .xlsx；
- xlsx 用 openpyxl 只读模式、取公式的缓存值（data_only）；日期单元格得到 naive datetime（由 normalize 按北京时间解释）；
- csv 依次尝试 utf-8-sig → utf-8 → gb18030（兼容 GBK），分隔符在逗号、制表符、分号中按表头行自动判断；
- 表头 = 第一个非空行；完全空白的行跳过；重复表头加后缀「 (2)」「 (3)」；表头为空但有数据的列命名为「第 N 列」；
- 一行（去掉行尾空单元格后）超过 MAX_COLS 列 → 拒绝（防止「一格写在 XFD 列」的小文件把每行撑成 16384 列）；
- row_numbers 为文件里的实际行号（从 1 起，与 Excel 左侧行号一致；表头在第 1 行时与契约的 row_no 一致）。

不改写用户的原文件：只读取传入的字节。
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import PurePath
from typing import Any

from services import clock, errors

MAX_BYTES = 10 * 1024 * 1024
#: 单个文件的非空数据行上限（防止压缩炸弹把内存撑满）
MAX_ROWS = 100_000
#: 单行的列数上限（去掉行尾空单元格后）；订单表远用不到这么多列
MAX_COLS = 512

_CSV_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")
_SHEET_NAME_BAD = re.compile(r"[\[\]:*?/\\]")
_ILLEGAL_XML = re.compile(r"[\000-\010\013\014\016-\037]")


@dataclass
class Sheet:
    """一个工作表：表头、数据行（{表头: 原值}）、每行在文件里的行号。"""

    name: str
    headers: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_numbers: list[int] = field(default_factory=list)


# ── 读取 ─────────────────────────────────────────────────────────────────────


def read_workbook(data: bytes, filename: str) -> list[Sheet]:
    """输入：上传文件的字节、原文件名 → 输出：[Sheet]（csv 只有一个工作表，名为文件名去扩展名）。

    文件过大、为空、格式不支持或无法解析时抛 errors.validation。没有任何非空行的工作表也会返回（headers 为空）。
    """
    if not data:
        raise errors.validation("文件是空的")
    if len(data) > MAX_BYTES:
        raise errors.validation(f"文件超过 {MAX_BYTES // (1024 * 1024)} MB，请拆分后再上传", size=len(data))
    ext = PurePath(filename or "").suffix.lower()
    if ext == ".xls":
        raise errors.validation("请把 .xls 另存为 .xlsx 后再上传")
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(data)
    if ext == ".csv":
        return [_read_csv(data, PurePath(filename).stem or "CSV")]
    raise errors.validation("只支持 .xlsx 和 .csv 文件")


def _read_xlsx(data: bytes) -> list[Sheet]:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # 损坏的 zip、缺部件、不是工作簿……统一成校验错误
        raise errors.validation("无法读取这个 .xlsx 文件，请确认它是 Excel 工作簿", reason=type(exc).__name__) from None
    try:
        sheets: list[Sheet] = []
        budget = [MAX_ROWS]
        for ws in wb.worksheets:
            if hasattr(ws, "reset_dimensions"):
                ws.reset_dimensions()       # 有些导出工具写错尺寸信息，按实际单元格读
            raw = ((i, _trim_row(r, ws.title)) for i, r in enumerate(ws.iter_rows(values_only=True), start=1))
            sheets.append(_build_sheet(ws.title, raw, budget))
        return sheets
    except errors.DomainError:
        raise
    except Exception as exc:
        raise errors.validation("这个 .xlsx 文件内容损坏，无法读取", reason=type(exc).__name__) from None
    finally:
        wb.close()


def _trim_row(values, sheet_name: str) -> list[Any]:
    """一行 → 去掉行尾空单元格后的值列表；仍超过 MAX_COLS 列 → 校验错误（只读模式会把行补到最右一格）。"""
    row = list(values)
    while row and _blank(row[-1]):
        row.pop()
    if len(row) > MAX_COLS:
        raise errors.validation(f"工作表「{sheet_name}」超过 {MAX_COLS} 列，请删掉多余的列后再上传")
    return row


def _decode(data: bytes) -> str:
    for enc in _CSV_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise errors.validation("无法识别 CSV 文件的编码，请另存为 UTF-8 后再上传")


def _read_csv(data: bytes, name: str) -> Sheet:
    text = _decode(data)
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = max((",", "\t", ";"), key=lambda d: first.count(d)) if first else ","
    if first and first.count(delimiter) == 0:
        delimiter = ","
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        raw = [(i, _trim_row([c if c != "" else None for c in r], name)) for i, r in enumerate(reader, start=1)]
    except csv.Error as exc:
        raise errors.validation(f"CSV 文件格式不对：{exc}") from None
    return _build_sheet(name, raw, [MAX_ROWS])


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _build_sheet(name: str, raw_rows, budget: list[int]) -> Sheet:
    """输入：工作表名、(行号, 值列表) 序列、剩余行数额度 → 输出：Sheet（定表头、跳空行、去重表头、命名空表头列）。"""
    header_row: list[Any] | None = None
    body: list[tuple[int, list[Any]]] = []
    for row_no, values in raw_rows:
        if all(_blank(v) for v in values):
            continue
        if header_row is None:
            header_row = values
            continue
        budget[0] -= 1
        if budget[0] < 0:
            raise errors.validation(f"文件超过 {MAX_ROWS} 行，请拆分后再上传")
        body.append((row_no, values))
    if header_row is None:
        return Sheet(name=name, headers=[])

    filled = {i for i, h in enumerate(header_row) if not _blank(h)}
    for _, v in body:
        filled.update(i for i, x in enumerate(v) if not _blank(x))
    used_cols = sorted(filled)
    headers: list[str] = []
    seen: set[str] = set()
    for i in used_cols:
        h = header_row[i] if i < len(header_row) else None
        base = _header_text(h) if not _blank(h) else f"第 {i + 1} 列"
        name_ = base
        n = 2
        while name_ in seen:
            name_ = f"{base} ({n})"
            n += 1
        seen.add(name_)
        headers.append(name_)

    sheet = Sheet(name=name, headers=headers)
    for row_no, values in body:
        sheet.rows.append({h: (values[i] if i < len(values) else None) for h, i in zip(headers, used_cols)})
        sheet.row_numbers.append(row_no)
    return sheet


def _header_text(h: Any) -> str:
    if isinstance(h, float) and h.is_integer():
        h = int(h)
    if isinstance(h, datetime):
        return h.strftime("%Y-%m-%d %H:%M:%S") if h.time() != time(0) else h.date().isoformat()
    s = str(h).replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


def row_to_json(row: dict[str, Any]) -> dict[str, Any]:
    """输入：Sheet.rows 的一行 → 输出：可写入 jsonb 的 {表头: 值}（时刻转 ISO 字符串、Decimal 转字符串）。"""
    return {k: _jsonable(v) for k, v in row.items()}


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, float) and not (v == v and abs(v) != float("inf")):
        return None
    return v


# ── 写出 ─────────────────────────────────────────────────────────────────────


def write_xlsx(sheets: list[tuple[str, list[str], list[Any]]], *,
               text_headers: set[str] | frozenset[str] | None = None) -> bytes:
    """输入：[(工作表名, 表头, 行)]（行为 {表头: 值} 或与表头同序的列表）、可选「按文本格式」的列名 → 输出：xlsx 字节。

    表头加粗并冻结首行，列宽按内容估算（中文按两个字符宽）。带时区的时刻按北京时间写出（Excel 不支持时区）；
    以「=」开头的文本（表头与数据都一样）按文本写入（不当公式）；dict / list 写成 JSON 文本。text_headers 里的列设为文本格式
    （模板用：邮编、订单号等手填时保留前导 0）。
    """
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    names: set[str] = set()
    bold = Font(bold=True)
    text_cols = set(text_headers or ())
    for sheet_name, headers, rows in sheets or [("Sheet1", [], [])]:
        rows = list(rows or [])
        ws = wb.create_sheet(_unique_sheet_name(sheet_name, names))
        widths = [_display_width(h) for h in headers]
        for c, h in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=c)
            _set_value(cell, _clean_cell_text(str(h)))
            cell.font = bold
        for r, row in enumerate(rows, start=2):
            values = [row.get(h) for h in headers] if isinstance(row, dict) else list(row)
            for c, v in enumerate(values, start=1):
                cell = ws.cell(row=r, column=c)
                xv = _excel_value(v)
                _set_value(cell, xv)
                if isinstance(xv, date) and not isinstance(xv, datetime):
                    cell.number_format = "yyyy-mm-dd"
                elif isinstance(xv, datetime):
                    cell.number_format = "yyyy-mm-dd hh:mm:ss"
                if c <= len(widths) and r <= 500:
                    widths[c - 1] = max(widths[c - 1], _display_width(xv))
        for c, h in enumerate(headers, start=1):
            letter = get_column_letter(c)
            ws.column_dimensions[letter].width = min(60, max(8, widths[c - 1] + 2))
            if h in text_cols:
                ws.column_dimensions[letter].number_format = "@"
                for r in range(2, len(rows) + 2):
                    ws.cell(row=r, column=c).number_format = "@"
        if headers:
            ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _set_value(cell, value: Any) -> None:
    """写单元格；以「=」开头的文本强制为文本（防公式注入：上传者写的表头 / 内容不能在下载者那里变成公式）。"""
    cell.value = value
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"


def _unique_sheet_name(name: str, used: set[str]) -> str:
    base = _SHEET_NAME_BAD.sub("_", str(name or "Sheet")).strip("'") or "Sheet"
    base = base[:31]
    cand, n = base, 2
    while cand.lower() in used:
        suffix = f" ({n})"
        cand = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(cand.lower())
    return cand


def _clean_cell_text(s: str) -> str:
    return _ILLEGAL_XML.sub("", s)


def _excel_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            return v.astimezone(clock.BJ_TZ).replace(tzinfo=None)
        return v
    if isinstance(v, (date, time, int, float, Decimal)):
        return v
    if isinstance(v, (dict, list, tuple)):
        return _clean_cell_text(json.dumps(v, ensure_ascii=False, default=str))
    return _clean_cell_text(str(v))


def _display_width(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, datetime):
        return 19
    if isinstance(v, date):
        return 10
    s = str(v)
    if "\n" in s:
        s = max(s.split("\n"), key=len)
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s[:100])
