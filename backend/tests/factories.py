"""测试数据工厂（其他模块的测试也可复用）。全部数据虚构：DEMO 名称、假地址、假订单号。

约定：工厂直接写库（不经业务服务，避免触发分配等副作用），只有 ingest_ready_order 走 orders.ingest。
时间一律取 services.clock（测试里先 clock.freeze）。
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from services import clock, orders
from services.models import SourceRef, UpstreamRecord

_seq = itertools.count(1)
_DEFAULT = object()


def _n() -> int:
    return next(_seq)


def make_workspace(conn, name: str | None = None) -> dict[str, Any]:
    """输入：连接、工作区名 → 输出：{id, user_id}：新工作区、它的所有者（密码不可登录）与默认设置行。"""
    n = _n()
    ws = conn.execute("INSERT INTO procure.workspaces (name) VALUES (%s) RETURNING id",
                      (name or f"DEMO 工作区 {n}",)).fetchone()["id"]
    uid = conn.execute(
        "INSERT INTO procure.users (username, display_name, password_hash) VALUES (%s, %s, 'x') RETURNING id",
        (f"demo_owner_{ws}_{n}", f"DEMO 所有者 {n}"),
    ).fetchone()["id"]
    conn.execute("INSERT INTO procure.workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'owner')",
                 (ws, uid))
    conn.execute("INSERT INTO procure.workspace_settings (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING", (ws,))
    return {"id": ws, "user_id": uid}


def make_account(conn, ws_id: int, **kw: Any) -> dict[str, Any]:
    """输入：工作区、任意 buyer_accounts 列（name、min_amount、max_amount、daily_limit、priority…）→ 输出：账号行。

    缺省：名称「环境-DEMO-<n>」、金额 0–不限、每日 20 单、优先级 2、启用采购、未暂停。
    """
    n = _n()
    vals: dict[str, Any] = {
        "name": f"环境-DEMO-{n:03d}", "min_amount": Decimal("0"), "max_amount": None, "daily_limit": 20,
        "priority": 2, "purchase_enabled": True, "execution_paused": False,
    }
    vals.update(kw)
    for k in ("min_amount", "max_amount"):
        if vals.get(k) is not None:
            vals[k] = Decimal(str(vals[k]))
    now = clock.now()
    vals.setdefault("created_at", now)
    vals.setdefault("updated_at", now)
    vals["workspace_id"] = ws_id
    cols = list(vals)
    return conn.execute(
        f"INSERT INTO procure.buyer_accounts ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
        list(vals.values()),
    ).fetchone()


def make_device(conn, ws_id: int, *, name: str | None = None, bound_account_id: int | None = None,
                identity: dict[str, Any] | None = None, identity_status: str = "unknown",
                last_seen_at: datetime | None | object = _DEFAULT, token_hash: str | None = None,
                revoked_at: datetime | None = None) -> dict[str, Any]:
    """输入：工作区、设备名、绑定账号、身份、最近心跳 → 输出：devices 行（缺省刚刚心跳过）。"""
    n = _n()
    seen = clock.now() if last_seen_at is _DEFAULT else last_seen_at
    return conn.execute(
        """
        INSERT INTO procure.devices (workspace_id, name, browser, token_hash, bound_account_id, ext_version,
                                     last_seen_at, identity, identity_status, identity_at, created_at, revoked_at)
        VALUES (%s, %s, 'chrome', %s, %s, '0.1.0', %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (ws_id, name or f"Mac-DEMO-{n:03d}", token_hash or f"demo-token-hash-{n}", bound_account_id, seen,
         Jsonb(identity) if identity is not None else None, identity_status, seen, clock.now(), revoked_at),
    ).fetchone()


def make_run(conn, ws_id: int, account_id: int, device_id: int, *, simulated: bool = True,
             trigger: str = "manual") -> dict[str, Any]:
    """输入：工作区、账号、设备 → 输出：未结束的 execution_runs 行（租约 180 秒）。"""
    from datetime import timedelta

    now = clock.now()
    return conn.execute(
        """
        INSERT INTO procure.execution_runs (workspace_id, account_id, device_id, trigger, simulated, started_at,
                                            heartbeat_at, lease_expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (ws_id, account_id, device_id, trigger, simulated, now, now, now + timedelta(seconds=180)),
    ).fetchone()


def make_record(**kw: Any) -> orders.UpstreamRecord:
    """输入：任意 UpstreamRecord 字段 → 输出：资料齐全、人审通过的虚构记录。

    缺省：单价 $25.00 × 1、限价 $40.00、建议采购日期 = 北京今天（传 suggested_purchase_date=None 改为按
    下单时间与配送天数计算）、收件人 Demo Buyer（虚构地址）。订单行 ID 等展示字段由平台订单号推出，
    同一订单号多次调用得到相同记录（便于测试「同一记录再次到来」）。
    """
    no = kw.get("upstream_order_no") or f"PO-DEMO-{_n():06d}"
    tail = no.removeprefix("PO-")
    vals: dict[str, Any] = {
        "upstream_order_no": no,
        "order_line_id": f"LINE-{tail}",
        "store_name": "STORE-DEMO",
        "sales_po_no": f"SPO-{tail}",
        "sku": "SKU-DEMO",
        "asin": "B0DEMO0001",
        "quantity": 1,
        "unit_price": Decimal("25.00"),
        "sales_total": Decimal("39.99"),
        "price_cap": Decimal("40.00"),
        "order_time": datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc),
        "ship_days": 4,
        "suggested_purchase_date": clock.bj_today(),
        "ship_method": "FBA",
        "recipient_name": "Demo Buyer",
        "address1": "100 Demo Street",
        "address2": None,
        "city": "Demo City",
        "state": "FL",
        "postal_code": "32960",
        "phone": "555-0100",
        "country": "US",
        "district": None,
        "review_value": "通过",
        "review": "approved",
    }
    vals.update(kw)
    for k in ("unit_price", "sales_total", "price_cap"):
        if vals.get(k) is not None and not isinstance(vals[k], Decimal):
            vals[k] = Decimal(str(vals[k]))
    return UpstreamRecord(**vals)


def feishu_source(conn, ws_id: int, record_id: str, connection_id: int | None = None,
                  snapshot: dict[str, Any] | None = None) -> SourceRef:
    """输入：工作区、飞书记录 id、连接 id（缺省建 / 复用一个虚构连接）→ 输出：SourceRef(kind='feishu')。"""
    if connection_id is None:
        row = conn.execute("SELECT id FROM procure.feishu_connections WHERE workspace_id = %s ORDER BY id LIMIT 1",
                           (ws_id,)).fetchone()
        if row is None:
            n = _n()
            row = conn.execute(
                """
                INSERT INTO procure.feishu_connections (workspace_id, name, link, app_token, source_table_id,
                                                        result_table_id)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (ws_id, "DEMO 订单中心", "https://demo.example.invalid/base/bascnDEMO", f"bascnDEMO{n:06d}",
                 "tblDEMO0000001", "tblDEMO0000001"),
            ).fetchone()
        connection_id = row["id"]
    return SourceRef(kind="feishu", snapshot=snapshot or {"平台订单号": record_id}, connection_id=connection_id,
                     record_id=record_id)


def make_batch(conn, ws_id: int, *, filename: str = "demo-orders.xlsx") -> int:
    """输入：工作区、文件名 → 输出：新建的 import_batches id（状态 previewed，原件为空字节）。"""
    return conn.execute(
        "INSERT INTO procure.import_batches (workspace_id, filename, file_sha256, file_bytes, status) "
        "VALUES (%s, %s, %s, %s, 'previewed') RETURNING id",
        (ws_id, filename, f"demo-sha-{_n()}", b""),
    ).fetchone()["id"]


def file_source(conn, ws_id: int, row_no: int, batch_id: int | None = None,
                snapshot: dict[str, Any] | None = None) -> SourceRef:
    """输入：工作区、行号、批次 id（缺省新建一个批次）→ 输出：SourceRef(kind='file')。"""
    if batch_id is None:
        batch_id = make_batch(conn, ws_id)
    return SourceRef(kind="file", snapshot=snapshot or {"行号": row_no}, batch_id=batch_id, row_no=row_no)


def ingest_ready_order(conn, ws_id: int, *, source: SourceRef | None = None, **kw: Any) -> dict[str, Any]:
    """输入：工作区、可选来源（缺省新文件批次第 2 行）、记录字段 → 输出：ingest 后的任务行（通过人审且资料齐全，
    状态 queued；没有自动分配）。"""
    rec = make_record(**kw)
    src = source or file_source(conn, ws_id, 2)
    out = orders.ingest(conn, ws_id, rec, src)
    assert out.order_id is not None, out
    return orders.get_row(conn, ws_id, out.order_id)


def make_attempt(conn, ws_id: int, order_id: int, account_id: int | None, *, status: str = "claimed",
                 bj_date: date | None = None, simulated: bool = True, kind: str = "auto",
                 submitted: bool = False, amazon_order_no: str | None = None,
                 claimed_at: datetime | None = None) -> dict[str, Any]:
    """输入：工作区、任务、账号、尝试状态、北京日期（缺省按 claimed_at / 现在）等 → 输出：purchase_attempts 行。

    只写尝试本身，不改任务状态（测试按需另调 set_status）。
    """
    at = claimed_at or clock.now()
    no = conn.execute("SELECT COALESCE(max(attempt_no), 0) + 1 AS n FROM procure.purchase_attempts "
                      "WHERE purchase_order_id = %s", (order_id,)).fetchone()["n"]
    terminal = status in ("succeeded", "failed", "unknown", "aborted")
    return conn.execute(
        """
        INSERT INTO procure.purchase_attempts (workspace_id, purchase_order_id, attempt_no, kind, account_id,
            simulated, status, bj_date, claimed_at, submitted_at, finished_at, amazon_order_no)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (ws_id, order_id, no, kind, account_id, simulated, status, bj_date or clock.bj_date(at), at,
         at if submitted else None, at if terminal else None, amazon_order_no),
    ).fetchone()


def set_status(conn, order_id: int, status: str, **cols: Any) -> dict[str, Any]:
    """输入：任务、状态、其他列 → 输出：任务行。测试专用：绕过迁移表直接改状态（准备「执行中」等场景）。"""
    cols["status"] = status
    sets = ", ".join(f"{k} = %s" for k in cols)
    return conn.execute(f"UPDATE procure.purchase_orders SET {sets} WHERE id = %s RETURNING *",
                        [*cols.values(), order_id]).fetchone()


def count(conn, table: str, where: str = "TRUE", params: tuple = ()) -> int:
    """输入：表名（procure 下）、条件 → 输出：行数。"""
    return conn.execute(f"SELECT count(*) AS n FROM procure.{table} WHERE {where}", params).fetchone()["n"]
