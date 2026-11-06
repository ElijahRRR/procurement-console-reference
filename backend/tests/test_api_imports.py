"""HTTP：只用文件的工作区——下载模板 → 上传（openpyxl 生成的模板同名列）→ 预览 → 提交 → 按视图查看
（today / future / pending_review）→ 导出结果 xlsx；问题行下载、重复提交、放弃、个人模板、超过 10 MB 拒绝（413）。
全部数据虚构。"""

from __future__ import annotations

from urllib.parse import unquote

from services import exports
from tests.api_helpers import (IMPORT_HEADERS, api, create_account, err, find_order, import_orders,  # noqa: F401
                               import_row, ok, owner, read_xlsx, upload, view_nos, xlsx_bytes)

ROWS = [
    import_row("PO-DEMO-F0001"),                                   # 第 2 行：今天可执行（建议日期 10-29 ≤ 今天）
    import_row("PO-DEMO-F0002", 建议采购日期="2026-11-02"),          # 第 3 行：未来
    import_row("PO-DEMO-F0003", 人工审核=None),                      # 第 4 行：缺人审 → 待确认
    import_row("PO-DEMO-F0004", ASIN=None),                         # 第 5 行：缺 ASIN → 资料待补齐
    import_row("PO-DEMO-F0001", 限价=99),                            # 第 6 行：同批次重复 → 无效
]


def test_template_download_is_an_xlsx_attachment_with_utf8_filename(owner):
    resp = owner.get("/api/v1/imports/template.xlsx")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;") and 'filename="' in cd and "filename*=UTF-8''" in cd
    assert unquote(cd.split("filename*=UTF-8''", 1)[1]) == "采购导入模板.xlsx"
    header = read_xlsx(resp.content)[0]
    assert header[0] == "平台订单号" and set(IMPORT_HEADERS) <= set(header)


def test_file_import_flow_preview_commit_views_and_export(owner):
    acc = create_account(owner)
    up = upload(owner, xlsx_bytes(ROWS))
    assert up["sheet"] == "采购订单" and up["headers"] == IMPORT_HEADERS
    assert up["suggested_mapping"]["平台订单号"] == "upstream_order_no" and up["suggested_mapping"]["备注"] is None
    assert any(f["key"] == "upstream_order_no" and f["required"] for f in up["system_fields"])

    bid = up["batch_id"]
    pv = ok(owner.post(f"/api/v1/imports/{bid}/preview", json={"sheet": "采购订单", "mapping": up["suggested_mapping"]}))
    assert pv["stats"]["total"] == 5 and pv["stats"]["new"] == 4 and pv["stats"]["invalid"] == 1
    assert pv["stats"]["pending_review"] == 1 and pv["stats"]["needs_info"] == 1
    assert pv["batch"]["status"] == "previewed" and pv["rows"]["total"] == 5
    by_row = {r["row_no"]: r for r in pv["rows"]["rows"]}
    assert by_row[2]["verdict"] == "new" and by_row[2]["suggested_purchase_date"] == "2026-10-29"
    assert by_row[6]["verdict"] == "invalid"
    assert ok(owner.get("/api/v1/orders"))["total"] == 0                 # 预览不建任务

    invalid = ok(owner.get(f"/api/v1/imports/{bid}/rows", params={"verdict": "invalid"}))
    assert [r["row_no"] for r in invalid["rows"]] == [6]

    cm = ok(owner.post(f"/api/v1/imports/{bid}/commit"))
    assert cm["batch"]["status"] == "committed" and cm["stats"]["new"] == 4
    assert cm["allocation"]["trigger"] == "import"
    err(owner.post(f"/api/v1/imports/{bid}/commit"), 409, "CONFLICT")   # 只能提交一次

    assert view_nos(owner, "today") == ["PO-DEMO-F0001"]
    assert view_nos(owner, "future") == ["PO-DEMO-F0002"]
    assert view_nos(owner, "pending_review") == ["PO-DEMO-F0003"]
    assert view_nos(owner, "needs_info") == ["PO-DEMO-F0004"]
    today = find_order(owner, "PO-DEMO-F0001")
    assert today["assigned_account"] == {"id": acc["id"], "name": acc["name"]}
    assert today["unit_price"] == "25.00" and today["price_cap_eff"] == "40.00" and today["subtotal_eff"] == "25.00"
    assert today["primary_source_kind"] == "file" and today["source_kinds"] == ["file"]
    assert find_order(owner, "PO-DEMO-F0004")["missing_fields"]

    listing = ok(owner.get("/api/v1/imports"))
    assert listing["total"] == 1 and listing["rows"][0]["id"] == bid and listing["rows"][0]["status"] == "committed"

    resp = owner.get("/api/v1/exports/orders.xlsx", params={"view": "all"})
    assert resp.status_code == 200 and resp.headers["content-disposition"].startswith("attachment;")
    table = read_xlsx(resp.content)
    assert table[0][:len(exports.EXPORT_HEADERS)] == exports.EXPORT_HEADERS
    nos = {r[0] for r in table[1:]}
    assert nos == {"PO-DEMO-F0001", "PO-DEMO-F0002", "PO-DEMO-F0003", "PO-DEMO-F0004"}

    by_batch = read_xlsx(owner.get("/api/v1/exports/orders.xlsx", params={"batch_id": bid}).content)
    assert {r[0] for r in by_batch[1:]} == nos
    today_only = read_xlsx(owner.get("/api/v1/exports/orders.xlsx", params={"view": "today"}).content)
    assert [r[0] for r in today_only[1:]] == ["PO-DEMO-F0001"]


def test_reimporting_the_same_rows_keeps_one_task_per_order(owner):
    create_account(owner)
    data = xlsx_bytes(ROWS[:2])                 # 同一个文件上传两次（openpyxl 写入时间戳，重新生成字节会不同）
    import_orders(owner, data=data)
    again = import_orders(owner, data=data)
    assert again["preview"]["stats"]["unchanged"] == 2 and again["commit"]["stats"]["new"] == 0
    assert again["upload"]["previous_upload"] is not None              # 提醒「这个文件已经导入过」
    assert ok(owner.get("/api/v1/orders"))["total"] == 2


def test_problem_rows_download(owner):
    up = upload(owner, xlsx_bytes(ROWS))
    ok(owner.post(f"/api/v1/imports/{up['batch_id']}/preview", json={"sheet": up["sheet"],
                                                                     "mapping": up["suggested_mapping"]}))
    resp = owner.get(f"/api/v1/imports/{up['batch_id']}/problems.xlsx")
    assert resp.status_code == 200
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]
    table = read_xlsx(resp.content)
    header = table[0]
    assert header[:len(IMPORT_HEADERS)] == IMPORT_HEADERS and header[-3:] == ["结论", "原因", "原行号"]
    assert sorted(r[-1] for r in table[1:]) == [4, 5, 6]       # 缺人审、缺字段、同批次重复


def test_discard_and_unknown_batch(owner):
    up = upload(owner, xlsx_bytes(ROWS[:1]))
    out = ok(owner.post(f"/api/v1/imports/{up['batch_id']}/discard"))
    assert out["status"] == "discarded"
    err(owner.post(f"/api/v1/imports/{up['batch_id']}/commit"), 422, "VALIDATION")
    err(owner.post("/api/v1/imports/999999/preview", json={}), 404, "NOT_FOUND")
    err(owner.get("/api/v1/imports/999999/problems.xlsx"), 404, "NOT_FOUND")
    err(owner.get("/api/v1/exports/orders.xlsx", params={"batch_id": 999999}), 404, "NOT_FOUND")


def test_upload_rejects_bad_files(owner):
    err(owner.post("/api/v1/imports", files={"file": ("demo.xls", b"not really xls", "application/vnd.ms-excel")}),
        422, "VALIDATION")
    err(owner.post("/api/v1/imports", files={"file": ("demo.csv", b"", "text/csv")}), 422, "VALIDATION")
    err(owner.post("/api/v1/imports", data={"other": "x"}), 422, "VALIDATION")       # 没有 file 字段


def test_upload_over_10_mb_is_413(owner):
    big = b"x" * (10 * 1024 * 1024 + 1)
    body = err(owner.post("/api/v1/imports", files={"file": ("demo-big.csv", big, "text/csv")}), 413, "VALIDATION")
    assert body["detail"]["max_bytes"] == 10 * 1024 * 1024
    assert ok(owner.get("/api/v1/imports"))["total"] == 0


def test_csv_upload_works_without_feishu(owner):
    csv = ("平台订单号,收货人姓名,ASIN,数量,人工审核\nPO-DEMO-C0001,Demo Buyer,B0DEMO0001,1,通过\n").encode("utf-8-sig")
    up = upload(owner, csv, filename="demo-orders.csv")
    assert up["headers"][:2] == ["平台订单号", "收货人姓名"]
    pv = ok(owner.post(f"/api/v1/imports/{up['batch_id']}/preview", json={"mapping": up["suggested_mapping"]}))
    assert pv["stats"]["total"] == 1 and pv["stats"]["needs_info"] == 1


def test_personal_templates_crud(owner):
    mapping = {"单号": "upstream_order_no", "商品": "asin", "备注": None}
    t = ok(owner.post("/api/v1/import-templates", json={"name": "DEMO 模板", "mapping": mapping}))
    assert t["name"] == "DEMO 模板" and t["mapping"] == {"单号": "upstream_order_no", "商品": "asin"}
    listing = ok(owner.get("/api/v1/import-templates"))
    assert listing["total"] == 1 and listing["rows"][0]["id"] == t["id"]
    err(owner.post("/api/v1/import-templates", json={"name": "坏模板", "mapping": {"商品": "asin"}}), 422, "VALIDATION")
    assert ok(owner.delete(f"/api/v1/import-templates/{t['id']}")) == {"id": t["id"]}
    err(owner.delete(f"/api/v1/import-templates/{t['id']}"), 404, "NOT_FOUND")
