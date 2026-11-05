"""来源规范化与文件提交（NEW-D4-sources-1、NEW-RB-7、NEW-DOC-9）：数量 1–999 之外视为不可靠；一行出意外的数据库
错误只让该行无效、不挡整批；来源金额与 models.money / money_cap 同一套规则（US$、USD 写法）。全部数据虚构。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from services import clock, imports, normalize, orders, sheet_io
from services.models import money, money_cap
from tests.api_helpers import api, import_row, ok, owner, upload, xlsx_bytes  # noqa: F401

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
HEADERS = ["平台订单号", "收货人姓名", "收货人地址", "收货人城市", "收货人州/省", "邮编", "收货人电话", "收货人国家",
           "ASIN", "数量", "人工审核", "限价", "采购单价", "下单时间", "配送天数"]


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def _row(no, **kw):
    r = {"平台订单号": no, "收货人姓名": "Demo Buyer", "收货人地址": "100 Demo Street", "收货人城市": "Demo City",
         "收货人州/省": "FL", "邮编": "32960", "收货人电话": "555-0100", "收货人国家": "US", "ASIN": "B0DEMO0001",
         "数量": 1, "人工审核": "通过", "限价": 40, "采购单价": 25, "下单时间": "2026/10/27 21:31", "配送天数": 4}
    r.update(kw)
    return r


def _upload(conn, ws, uid, rows):
    data = sheet_io.write_xlsx([("采购订单", HEADERS, [[r.get(h) for h in HEADERS] for r in rows])])
    return imports.upload(conn, ws, filename="demo-orders.xlsx", data=data, actor_user_id=uid)


# ── 数量上限 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [(999, 999), ("999", 999), (1, 1)])
def test_quantity_up_to_999_is_accepted(raw, expected):
    rec = normalize.normalize({"upstream_order_no": "PO-DEMO-Q0", "quantity": raw})
    assert rec.quantity == expected and rec.issues == []


@pytest.mark.parametrize("raw", [1000, "3000000000", 3000000000, 2147483648])
def test_quantity_above_999_is_unreliable(raw):
    rec = normalize.normalize({"upstream_order_no": "PO-DEMO-Q1", "quantity": raw})
    assert rec.quantity is None
    assert any(i.startswith("数量超出合理范围（1–999）") for i in rec.issues)


def test_console_override_uses_the_same_quantity_limit(conn, workspace):
    from tests import factories as f
    from services.errors import DomainError

    ws, uid = workspace["id"], workspace["user_id"]
    o = f.ingest_ready_order(conn, ws)
    with pytest.raises(DomainError) as e:
        orders.set_override(conn, ws, o["id"], quantity=normalize.QUANTITY_MAX + 1, actor_user_id=uid)
    assert e.value.code == "VALIDATION" and str(normalize.QUANTITY_MAX) in e.value.message


def test_huge_quantity_row_previews_as_needs_info_and_commit_keeps_the_good_row(conn, workspace):
    ws, uid = workspace["id"], workspace["user_id"]
    up = _upload(conn, ws, uid, [_row("PO-DEMO-Q2-OK"), _row("PO-DEMO-Q2-BIG", 数量="3000000000")])
    pv = imports.preview(conn, ws, up["batch_id"])
    by_no = {r["normalized"]["upstream_order_no"]: r for r in pv["rows"]["rows"]}
    big = by_no["PO-DEMO-Q2-BIG"]
    assert big["status"] == "needs_info" and any("数量超出合理范围" in i for i in big["issues"])
    out = imports.commit(conn, ws, up["batch_id"], uid)
    assert out["batch"]["status"] == "committed" and out["stats"]["new"] == 2
    rows = {r["upstream_order_no"]: r for r in conn.execute(
        "SELECT upstream_order_no, status, quantity FROM procure.purchase_orders WHERE workspace_id = %s", (ws,))}
    assert rows["PO-DEMO-Q2-OK"]["status"] == "queued"
    assert rows["PO-DEMO-Q2-BIG"]["status"] == "needs_info" and rows["PO-DEMO-Q2-BIG"]["quantity"] is None


def test_unexpected_error_on_one_row_marks_only_that_row_invalid(conn, workspace, monkeypatch):
    """提交时某行抛出非业务异常（如数据库数值越界）：保存点已回滚，该行记为无效「提交失败：<类型>」，其余照常提交。"""
    ws, uid = workspace["id"], workspace["user_id"]
    up = _upload(conn, ws, uid, [_row("PO-DEMO-E1"), _row("PO-DEMO-E2")])
    imports.preview(conn, ws, up["batch_id"])
    real = orders.ingest

    def flaky(conn_, ws_, rec, src, **kw):
        if rec.upstream_order_no == "PO-DEMO-E2":
            conn_.execute("SELECT 1/0")              # 真实的数据库错误：让保存点进入出错状态
        return real(conn_, ws_, rec, src, **kw)

    monkeypatch.setattr(orders, "ingest", flaky)
    out = imports.commit(conn, ws, up["batch_id"], uid)
    assert out["batch"]["status"] == "committed" and out["stats"]["new"] == 1 and out["stats"]["invalid"] == 1
    bad = conn.execute("SELECT verdict, issues, normalized FROM procure.import_rows WHERE batch_id = %s AND row_no = 3",
                       (up["batch_id"],)).fetchone()
    assert bad["verdict"] == "invalid" and bad["issues"][-1] == "提交失败：DivisionByZero"
    assert bad["normalized"]["_preview"]["reason"] == "提交失败：DivisionByZero"
    nos = [r["upstream_order_no"] for r in conn.execute("SELECT upstream_order_no FROM procure.purchase_orders")]
    assert nos == ["PO-DEMO-E1"]


def test_http_commit_with_a_huge_quantity_row_is_not_a_500(owner):
    up = upload(owner, xlsx_bytes([import_row("PO-DEMO-Q3-OK"), import_row("PO-DEMO-Q3-BIG", 数量="3000000000")]))
    pv = ok(owner.post(f"/api/v1/imports/{up['batch_id']}/preview",
                       json={"sheet": up["sheet"], "mapping": up["suggested_mapping"]}))
    statuses = {r["normalized"]["upstream_order_no"]: r["status"] for r in pv["rows"]["rows"]}
    assert statuses == {"PO-DEMO-Q3-OK": "queued", "PO-DEMO-Q3-BIG": "needs_info"}
    cm = ok(owner.post(f"/api/v1/imports/{up['batch_id']}/commit"))
    assert cm["stats"]["new"] == 2
    listed = ok(owner.get("/api/v1/orders", params={"view": "all", "q": "PO-DEMO-Q3"}))["rows"]
    assert sorted(r["upstream_order_no"] for r in listed) == ["PO-DEMO-Q3-BIG", "PO-DEMO-Q3-OK"]


# ── 金额：与 models 同一套规则 ────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["unit_price", "price_cap"])
@pytest.mark.parametrize("raw,expected", [("US$25.50", "25.50"), ("US $25.50", "25.50"), ("us$25.50", "25.50"),
                                          ("USD 52", "52.00"), ("52 usd", "52.00"), ("$1,234.50", "1234.50"),
                                          ("1，234.50", "1234.50")])
def test_source_money_accepts_us_dollar_forms(key, raw, expected):
    rec = normalize.normalize({"upstream_order_no": "PO-DEMO-M1", key: raw})
    assert getattr(rec, key) == Decimal(expected) and rec.issues == []


@pytest.mark.parametrize("raw", ["4usd0", "12,5", "1e3", "1.234,50", "40$5", "12US", "abc"])
def test_source_money_rejects_what_models_rejects(raw):
    rec = normalize.normalize({"upstream_order_no": "PO-DEMO-M2", "price_cap": raw})
    assert rec.price_cap is None and any("不是有效金额" in i for i in rec.issues)


@pytest.mark.parametrize("raw", ["US$40", "USD 40", "40 USD", "4usd0", "usd40usd", "$40.009", "78.685", "12,5",
                                 "1e3", "1,234.50", " 25 ", "+5", "9999999999.99", "10000000000", "US$ 1,000",
                                 "$.5", "40$5", "4US0"])
def test_text_amounts_follow_models_exactly(raw):
    """来源的文本金额只多做一步（全角逗号换半角），其余与 models.money / money_cap 完全一致（契约 §0）。"""
    rec = normalize.normalize({"upstream_order_no": "PO-DEMO-M3", "unit_price": raw, "price_cap": raw})
    want_up, want_cap = money(raw), money_cap(raw)
    assert rec.unit_price == (want_up if want_up is None or want_up >= 0 else None)
    assert rec.price_cap == (want_cap if want_cap is None or want_cap >= 0 else None)
