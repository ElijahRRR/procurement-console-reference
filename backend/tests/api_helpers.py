"""HTTP 接口测试的公共工具与夹具（fastapi TestClient + 真实 PostgreSQL）。全部数据虚构：DEMO 名称、假地址、假订单号。

- `api` 夹具：冻结时钟（北京 2026-10-31 10:00）、清空数据库，给出一个不托管前端的新应用。
- 控制台客户端缺省带防跨站请求头 `X-Requested-With: pc-console`；每个 TestClient 有自己的 Cookie。
- 准备数据尽量走接口；只有接口做不到的（第二个工作区的用户、直接造的任务）才用 services / tests.factories，
  而且每次都用自己的短事务（with db.pg_conn()）提交，避免和应用的请求事务互相等待。
"""

from __future__ import annotations

import contextlib
import io
from collections.abc import Iterator
from datetime import date, datetime, timezone
from typing import Any

import openpyxl
import pytest
from fastapi.testclient import TestClient

from registry import db
from services import auth, clock

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)   # 北京 2026-10-31 10:00
TODAY = date(2026, 10, 31)
CSRF = {"X-Requested-With": "pc-console"}
OWNER = {"username": "demo_owner", "password": "demo-pass-0001", "workspace_name": "DEMO 工作区 A"}
OTHER = {"username": "demo_other", "password": "demo-pass-0002", "workspace_name": "DEMO 工作区 B"}

#: 标准模板列的子集（与 services/file_mapping.STANDARD_COLUMNS 同名）
IMPORT_HEADERS = ["平台订单号", "店铺账号", "收货人姓名", "收货人地址", "收货人城市", "收货人州/省", "邮编", "收货人电话",
                  "收货人国家", "ASIN", "数量", "人工审核", "限价", "采购单价", "下单时间", "配送天数", "建议采购日期",
                  "备注"]


# ── 夹具 ──────────────────────────────────────────────────────────────────────


@pytest.fixture()
def api(clean_db, tmp_path) -> Iterator[Any]:
    """输入：无 → 输出：新建的 FastAPI 应用（不托管前端；时钟冻结在 NOW，测试后解冻）。"""
    from server.app import TEST_CLIENT_HOST, create_app

    clock.freeze(NOW)
    try:
        yield create_app(web_dist=tmp_path / "no-dist", allowed_hosts=(TEST_CLIENT_HOST,))
    finally:
        clock.unfreeze()


@pytest.fixture()
def owner(api) -> TestClient:
    """输入：无 → 输出：已完成首次设置并登录的所有者客户端（工作区 A）。"""
    c = console_client(api)
    ok(c.post("/api/v1/auth/setup", json=OWNER))
    return c


@pytest.fixture()
def other(api, owner) -> TestClient:
    """输入：无 → 输出：另一个工作区（B）的已登录用户客户端。"""
    create_user(**OTHER)
    c = console_client(api)
    ok(c.post("/api/v1/auth/login", json={"username": OTHER["username"], "password": OTHER["password"]}))
    return c


# ── 客户端与断言 ──────────────────────────────────────────────────────────────


def console_client(app, *, csrf: bool = True) -> TestClient:
    """输入：应用、是否带防跨站请求头 → 输出：控制台客户端（自己的 Cookie）。"""
    return TestClient(app, headers=CSRF if csrf else None)


def plugin_client(app) -> TestClient:
    """输入：应用 → 输出：插件客户端（不带防跨站头、不带 Cookie）。"""
    return TestClient(app)


def bearer(token: str) -> dict[str, str]:
    """输入：设备令牌 → 输出：Authorization 请求头。"""
    return {"Authorization": f"Bearer {token}"}


def ok(resp, status: int = 200) -> Any:
    """输入：响应、期望状态 → 输出：JSON 体（状态不符时带上响应体断言失败）。"""
    assert resp.status_code == status, f"{resp.request.method} {resp.request.url} → {resp.status_code}: {resp.text}"
    return resp.json() if resp.content else None


def err(resp, status: int, code: str) -> dict[str, Any]:
    """输入：响应、期望状态与错误码 → 输出：错误体 {code, message, detail}。"""
    assert resp.status_code == status, f"{resp.request.method} {resp.request.url} → {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["code"] == code, body
    assert isinstance(body["message"], str) and body["message"], body
    assert isinstance(body["detail"], dict), body
    return body


# ── 数据准备 ──────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def tx() -> Iterator[Any]:
    """输入：无 → 输出：一条短事务连接（退出即提交）。"""
    with db.pg_conn() as conn:
        yield conn


def create_user(username: str, password: str, workspace_name: str) -> dict[str, Any]:
    """输入：用户名、密码、新工作区名 → 输出：{user, workspace, role}（直接走 services.auth，首次设置之后建第二个工作区用）。"""
    with tx() as conn:
        return auth.create_user(conn, username, password, workspace_name=workspace_name)


def create_account(client: TestClient, **kw: Any) -> dict[str, Any]:
    """输入：控制台客户端、账号字段 → 输出：新账号（缺省金额 0–100、每日 10 单、虚构买家号 ID）。"""
    body = {"name": "环境-DEMO-A", "amazon_customer_id": "A1DEMO000001", "min_amount": "0.00",
            "max_amount": "100.00", "daily_limit": 10, "priority": 2}
    body.update(kw)
    return ok(client.post("/api/v1/accounts", json=body))


def import_row(no: str | None, **kw: Any) -> dict[str, Any]:
    """输入：平台订单号、要覆盖的列 → 输出：一行资料齐全、人审通过的虚构订单（下单北京 10-27，建议日期按 +6−4 = 10-29）。"""
    r: dict[str, Any] = {"平台订单号": no, "店铺账号": "STORE-DEMO", "收货人姓名": "Demo Buyer",
                         "收货人地址": "100 Demo Street", "收货人城市": "Demo City", "收货人州/省": "FL",
                         "邮编": "32960", "收货人电话": "555-0100", "收货人国家": "US", "ASIN": "B0DEMO0001", "数量": 1,
                         "人工审核": "通过", "限价": 40, "采购单价": 25, "下单时间": "2026/10/27 21:31", "配送天数": 4,
                         "建议采购日期": None, "备注": "DEMO"}
    r.update(kw)
    return r


def xlsx_bytes(rows: list[dict[str, Any]], headers: list[str] | None = None, sheet: str = "采购订单") -> bytes:
    """输入：行（{表头: 值}）、表头、工作表名 → 输出：用 openpyxl 生成的 xlsx 字节（邮编按文本写入）。"""
    hs = headers or IMPORT_HEADERS
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(hs)
    for r in rows:
        ws.append([r.get(h) for h in hs])
    for cell in ws["H"][1:]:
        cell.number_format = "@"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def read_xlsx(data: bytes) -> list[list[Any]]:
    """输入：xlsx 字节 → 输出：第一个工作表的全部行（值列表，含表头行）。"""
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    finally:
        wb.close()


def upload(client: TestClient, data: bytes, filename: str = "demo-orders.xlsx") -> dict[str, Any]:
    """输入：控制台客户端、文件字节、文件名 → 输出：POST /imports 的结果。"""
    files = {"file": (filename, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    return ok(client.post("/api/v1/imports", files=files))


def import_orders(client: TestClient, rows: list[dict[str, Any]] | None = None, *,
                  data: bytes | None = None) -> dict[str, Any]:
    """输入：控制台客户端、行（或现成的文件字节）→ 输出：{upload, preview, commit}（上传 → 按建议对应预览 → 提交）。"""
    up = upload(client, data if data is not None else xlsx_bytes(rows or []))
    pv = ok(client.post(f"/api/v1/imports/{up['batch_id']}/preview",
                        json={"sheet": up["sheet"], "mapping": up["suggested_mapping"]}))
    cm = ok(client.post(f"/api/v1/imports/{up['batch_id']}/commit"))
    return {"upload": up, "preview": pv, "commit": cm}


def find_order(client: TestClient, upstream_order_no: str) -> dict[str, Any]:
    """输入：控制台客户端、平台订单号 → 输出：任务列表行（找不到断言失败）。"""
    rows = ok(client.get("/api/v1/orders", params={"view": "all", "q": upstream_order_no}))["rows"]
    hit = [r for r in rows if r["upstream_order_no"] == upstream_order_no]
    assert hit, f"找不到任务 {upstream_order_no}"
    return hit[0]


def view_nos(client: TestClient, view: str) -> list[str]:
    """输入：控制台客户端、视图名 → 输出：该视图里全部任务的平台订单号。"""
    body = ok(client.get("/api/v1/orders", params={"view": view, "page_size": 200}))
    return [r["upstream_order_no"] for r in body["rows"]]


def identity(customer_id: str | None, *, signed_in: bool = True) -> dict[str, Any]:
    """输入：买家号 ID、是否已登录 → 输出：插件上报的身份（虚构页面地址）。"""
    return {"signed_in": signed_in, "customer_id": customer_id, "site": "US",
            "page_url": "https://www.amazon.com/gp/your-account/order-history"}


def pair_device(console: TestClient, plugin: TestClient, name: str = "Mac-AdsPower-DEMO-1",
                account: dict[str, Any] | None = None, report_identity: bool = True) -> dict[str, Any]:
    """输入：控制台客户端、插件客户端、设备名、要绑定的账号、是否上报匹配的身份 → 输出：{device_id, token, heartbeat}。

    流程：控制台生成配对码 → /plugin/v1/pair → 控制台绑定账号 → 插件心跳（捎带匹配的身份）。
    """
    code = ok(console.post("/api/v1/devices/pairing-codes"))["code"]
    paired = ok(plugin.post("/plugin/v1/pair", json={"code": code, "device_name": name, "browser": "adspower",
                                                     "ext_version": "0.1.0"}))
    if account is not None:
        ok(console.patch(f"/api/v1/devices/{paired['device_id']}", json={"bound_account_id": account["id"]}))
    body: dict[str, Any] = {"ext_version": "0.1.0"}
    if account is not None and report_identity:
        body["identity"] = identity(account["amazon_customer_id"])
    hb = ok(plugin.post("/plugin/v1/heartbeat", json=body, headers=bearer(paired["device_token"])))
    return {"device_id": paired["device_id"], "token": paired["device_token"], "heartbeat": hb}


def good_facts(attempt: dict[str, Any], **over: Any) -> dict[str, Any]:
    """输入：领取到的尝试载荷、要覆盖的事实 → 输出：能通过护栏的结算页事实（总额 = 小计，3 天后送达，邮编一致）。"""
    sub = attempt["subtotal"]
    facts: dict[str, Any] = {
        "items": [{"asin": attempt["asin"], "quantity": attempt["quantity"], "unit_price": attempt["unit_price"]}],
        "subtotal": sub, "shipping": "0.00", "tax": "0.00", "total_before_tax": sub, "total": sub,
        "gift_card_amount": None, "currency": "USD",
        "delivery": {"text": "Arriving Nov 3 (DEMO)", "date": "2026-11-03", "days": None},
        "ship_to": {"postal_code": attempt["postal_code"]}, "card_last4": None, "fulfillment": "amazon",
    }
    facts.update(over)
    return facts
