"""飞书写回队列（契约 §6「写回」、v1 §6.3）：入队、发送、退避、按结果版本取代、人工重试、列表。

- **入队**（enqueue_for_order）：采购结果变化的地方（插件回报成功、人工核实已下单、人工指定写回目标）调用。
  只有同时满足才入队：任务「已采购」、成功尝试**不是模拟、也不是来源带来的外部单号**、任务有飞书写回目标
  （order_sources.is_writeback_target）、写回对应非空。**连接停用或没开写回也照常入队**（只积压，发送时 flush 才看
  启用 / 开启写回 / 已验收），恢复后自动补发。文件来源、模拟结果、历史 / 外部单号永不写回。
- **补入队**（backfill_connection）：连接重新启用、开启写回或改了写回列时，把以该连接为写回目标、当前结果版本还没有
  写回行的已采购真实结果补入队列（例如写回对应原来为空时登记的结果）。
  写回值按连接保存的写回列类型（feishu_connections.writeback_field_types）转换；入队不访问飞书。
  同一（任务、连接、结果版本）只有一行；新版本入队时，同一任务同一连接的旧 pending / failed 行标 superseded。
- **发送**（flush，后台每 60 秒）：只发连接「启用 + 开启写回 + 已验收」且目标表仍是当前结果表的到期 pending 行；
  未验收的连接只积压不发送（界面显示「回填待验证」）。同一张表每批至多 500 条（batch_update_records）。
  有更新版本的行先标 superseded，**旧版本永远不会覆盖新版本**。成功 → done；失败 → tries + 1，按 1、2、5、10、30 分钟
  退避，第 10 次失败标 failed 并留 last_error。一批因某条记录出错（记录不存在、值转换失败）而整批被拒时，
  逐条重发以找出出错的那条；权限、网络等整批错误不拆分，整批退避。
- **结果表与来源表不同**：入队时 record_id 留空，发送前按平台订单号在结果表里定位记录（找不到按失败退避，多条则直接 failed）。
- **改绑**：连接改了结果表时，旧目标的 pending 行标 failed（不改投新表）；只改写回列时，pending 行按新对应重建。

模拟结果永不写回（CLAUDE.md 铁律 10）。本模块不 commit。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from api.feishu import BATCH_UPDATE_MAX, FeishuError
from services import clock, events, feishu_fields, normalize
from services.errors import DomainError, not_found, validation

log = logging.getLogger(__name__)

STATUSES = ("pending", "done", "failed", "superseded")
#: 第 n 次失败后等待的分钟数（第 5 次及以后都等 30 分钟）
BACKOFF_MINUTES = (1, 2, 5, 10, 30)
#: 失败满这么多次 → failed
MAX_TRIES = 10
#: 结果表与来源表不同、尚未定位到结果表记录时 record_id 的取值
UNRESOLVED_RECORD = ""

_ATTEMPT_SQL = """
SELECT a.*, ba.name AS account_name, ba.amazon_customer_id
FROM procure.purchase_attempts a
LEFT JOIN procure.buyer_accounts ba ON ba.id = a.account_id AND ba.workspace_id = a.workspace_id
WHERE a.id = %s AND a.workspace_id = %s
"""


# ── 入队 ─────────────────────────────────────────────────────────────────────


def _result_values(att: dict[str, Any]) -> dict[str, Any]:
    """输入：成功尝试行（带账号名与买家号 ID）→ 输出：{结果字段: 值}（None 的字段不写回）。"""
    return {
        "buyer_account": att.get("account_name") or att.get("buyer_account_name"),
        "buyer_customer_id": att.get("amazon_customer_id"),
        "amazon_order_no": att.get("amazon_order_no"),
        "purchase_status": "succeeded",
        "total_before_tax": att.get("total_before_tax"),
        "shipping": att.get("shipping"),
        "tax": att.get("tax"),
        "total": att.get("total"),
        "card_last4": att.get("card_last4"),
    }


def _eligible_attempt(conn, ws: int, order: dict[str, Any] | None) -> dict[str, Any] | None:
    """输入：任务行 → 输出：可以写回的成功尝试（已采购、非模拟、非外部单号）；不该写回 → None。"""
    if order is None or order["status"] != "purchased" or order["purchased_attempt_id"] is None:
        return None
    att = conn.execute(_ATTEMPT_SQL, (order["purchased_attempt_id"], ws)).fetchone()
    if att is None or att["status"] != "succeeded" or att["simulated"] or att["kind"] == "external":
        return None
    if not att["amazon_order_no"]:
        return None
    return att


def _target(conn, ws: int, order_id: int) -> dict[str, Any] | None:
    """输入：任务 → 输出：写回目标来源 + 连接列（c_ 前缀）；没有飞书写回目标 → None。"""
    return conn.execute(
        """
        SELECT s.feishu_record_id, s.feishu_connection_id,
               c.id AS c_id, c.enabled AS c_enabled, c.writeback_enabled AS c_writeback_enabled,
               c.writeback_map AS c_writeback_map, c.writeback_field_types AS c_types,
               c.writeback_verified_at AS c_verified_at, c.source_table_id AS c_source_table_id,
               c.result_table_id AS c_result_table_id
        FROM procure.order_sources s
        JOIN procure.feishu_connections c ON c.id = s.feishu_connection_id AND c.workspace_id = s.workspace_id
        WHERE s.workspace_id = %s AND s.purchase_order_id = %s AND s.kind = 'feishu' AND s.is_writeback_target
        LIMIT 1
        """,
        (ws, order_id),
    ).fetchone()


def _build(att: dict[str, Any], writeback_map: dict[str, Any], types: dict[str, Any] | None) -> dict[str, Any]:
    """输入：成功尝试、写回对应、列类型 → 输出：{飞书列名: 值}；列不可写或值无法转换抛 VALIDATION。"""
    return feishu_fields.build_writeback_fields(_result_values(att), writeback_map or {}, types or {})


def enqueue_for_order(conn, workspace_id: int, order_id: int) -> int | None:
    """输入：工作区、任务 id → 输出：该结果版本的写回 id（已入队过则返回原 id）；不需要写回时返回 None。

    不需要写回：任务不是已采购、成功尝试是模拟或外部单号、没有飞书写回目标、写回对应为空、按对应没有可写的值。
    连接停用 / 未开启写回时照常入队（只积压，不发送）。写回列配置有问题（列不可写、值无法转换）时不抛异常（不能挡住采购结果的登记），
    而是入队一条 failed 行并写明原因，供写回队列页处理。
    """
    order = conn.execute("SELECT * FROM procure.purchase_orders WHERE id = %s AND workspace_id = %s",
                         (order_id, workspace_id)).fetchone()
    att = _eligible_attempt(conn, workspace_id, order)
    if att is None:
        return None
    tgt = _target(conn, workspace_id, order_id)
    if tgt is None or not tgt["c_writeback_map"]:
        return None
    cid, version = tgt["c_id"], order["result_version"]
    existing = conn.execute(
        "SELECT id FROM procure.writebacks WHERE workspace_id = %s AND purchase_order_id = %s AND connection_id = %s "
        "AND result_version = %s",
        (workspace_id, order_id, cid, version),
    ).fetchone()
    if existing:
        return existing["id"]
    status, last_error = "pending", None
    try:
        fields = _build(att, tgt["c_writeback_map"], tgt["c_types"])
    except DomainError as exc:
        fields, status, last_error = {}, "failed", f"写回列配置有误：{exc.message}"
    if status == "pending" and not fields:
        return None
    same_table = tgt["c_result_table_id"] == tgt["c_source_table_id"]
    record_id = tgt["feishu_record_id"] if same_table else UNRESOLVED_RECORD
    now = clock.now()
    row = conn.execute(
        """
        INSERT INTO procure.writebacks (workspace_id, purchase_order_id, connection_id, table_id, record_id,
                                        result_version, fields, status, tries, last_error, next_try_at, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)
        ON CONFLICT (purchase_order_id, connection_id, result_version) DO NOTHING
        RETURNING id
        """,
        (workspace_id, order_id, cid, tgt["c_result_table_id"], record_id, version, Jsonb(fields), status,
         last_error, now, now),
    ).fetchone()
    if row is None:   # 并发入队
        return conn.execute(
            "SELECT id FROM procure.writebacks WHERE workspace_id = %s AND purchase_order_id = %s "
            "AND connection_id = %s AND result_version = %s", (workspace_id, order_id, cid, version)).fetchone()["id"]
    wid = row["id"]
    superseded = _supersede_older(conn, workspace_id, order_id, cid, version)
    events.record(conn, workspace_id, "order", order_id, "order.writeback_enqueued", detail={
        "writeback_id": wid, "connection_id": cid, "result_version": version, "status": status,
        "waiting_verification": tgt["c_verified_at"] is None, "superseded": superseded, "error": last_error})
    return wid


def backfill_connection(conn, workspace_id: int, connection_id: int) -> int:
    """输入：工作区、连接 id → 输出：补入队的写回行数。以该连接为写回目标、已采购且当前结果版本还没有写回行的任务
    逐个交给 enqueue_for_order（它再判断是否真实结果、是否有可写的值）。"""
    rows = conn.execute(
        """
        SELECT o.id FROM procure.purchase_orders o
        JOIN procure.order_sources s ON s.purchase_order_id = o.id AND s.workspace_id = o.workspace_id
        WHERE o.workspace_id = %s AND o.status = 'purchased' AND s.kind = 'feishu' AND s.is_writeback_target
          AND s.feishu_connection_id = %s
          AND NOT EXISTS (SELECT 1 FROM procure.writebacks w WHERE w.workspace_id = o.workspace_id
                          AND w.purchase_order_id = o.id AND w.connection_id = %s AND w.result_version = o.result_version)
        ORDER BY o.id
        """,
        (workspace_id, connection_id, connection_id),
    ).fetchall()
    n = 0
    for r in rows:
        if enqueue_for_order(conn, workspace_id, r["id"]) is not None:
            n += 1
    return n


def _supersede_older(conn, ws: int, order_id: int, connection_id: int, version: int) -> int:
    """输入：任务、连接、版本 → 输出：被标 superseded 的旧版本 pending / failed 行数。"""
    cur = conn.execute(
        "UPDATE procure.writebacks SET status = 'superseded' WHERE workspace_id = %s AND purchase_order_id = %s "
        "AND connection_id = %s AND result_version < %s AND status IN ('pending', 'failed')",
        (ws, order_id, connection_id, version),
    )
    return cur.rowcount or 0


# ── 发送 ─────────────────────────────────────────────────────────────────────

_DUE_SQL = """
SELECT w.*, o.upstream_order_no, c.app_token, c.source_table_id, c.field_map,
       EXISTS (SELECT 1 FROM procure.writebacks n
               WHERE n.workspace_id = w.workspace_id AND n.purchase_order_id = w.purchase_order_id
                 AND n.connection_id = w.connection_id AND n.result_version > w.result_version) AS has_newer
FROM procure.writebacks w
JOIN procure.feishu_connections c ON c.id = w.connection_id AND c.workspace_id = w.workspace_id
JOIN procure.purchase_orders o ON o.id = w.purchase_order_id AND o.workspace_id = w.workspace_id
WHERE w.status = 'pending' AND w.next_try_at <= %s
  AND c.enabled AND c.writeback_enabled AND c.writeback_verified_at IS NOT NULL
  AND w.table_id = c.result_table_id
ORDER BY w.connection_id, w.table_id, w.id
LIMIT %s
FOR UPDATE OF w SKIP LOCKED
"""

#: 单次 flush 最多处理的行数（其余下次再发）
FLUSH_LIMIT = 5000


def backoff_minutes(tries: int) -> int:
    """输入：已失败次数（≥1）→ 输出：下次重试前等待的分钟数（1、2、5、10、30、30……）。"""
    return BACKOFF_MINUTES[min(max(tries, 1), len(BACKOFF_MINUTES)) - 1]


def flush(conn, *, now: datetime | None = None,
          client_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """输入：连接、当前时刻（缺省 clock.now()）、飞书客户端工厂（缺省 feishu_connections.make_client）→
    输出：{due, sent, done, retrying, failed, superseded, waiting_verification[, error]}。

    跨工作区处理全部到期行（后台任务）。没有到期行时不创建飞书客户端；飞书未配置时到期行原样保留（不计失败次数）。
    """
    now = now or clock.now()
    stats: dict[str, Any] = {"due": 0, "sent": 0, "done": 0, "retrying": 0, "failed": 0, "superseded": 0,
                             "waiting_verification": _count_waiting(conn)}
    rows = conn.execute(_DUE_SQL, (now, FLUSH_LIMIT)).fetchall()
    stats["due"] = len(rows)
    fresh = []
    for r in rows:
        if r["has_newer"]:
            conn.execute("UPDATE procure.writebacks SET status = 'superseded' WHERE id = %s AND workspace_id = %s",
                         (r["id"], r["workspace_id"]))
            stats["superseded"] += 1
        else:
            fresh.append(dict(r))
    if not fresh:
        return stats
    if client_factory is None:
        from services import feishu_connections  # 延迟导入：feishu_connections 也导入本模块

        client_factory = feishu_connections.make_client
    try:
        client = client_factory()
    except DomainError as exc:
        stats["error"] = exc.message
        return stats
    try:
        groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for r in fresh:
            groups.setdefault((r["connection_id"], r["table_id"]), []).append(r)
        for group in groups.values():
            _send_group(conn, client, group, now, stats)
    finally:
        close = getattr(client, "close", None)
        if close:
            close()
    return stats


def _count_waiting(conn) -> int:
    """输入：连接 → 输出：因连接未验收 / 停用 / 关闭写回而积压的 pending 行数（全部工作区）。"""
    return int(conn.execute(
        """
        SELECT count(*) AS n FROM procure.writebacks w
        JOIN procure.feishu_connections c ON c.id = w.connection_id AND c.workspace_id = w.workspace_id
        WHERE w.status = 'pending'
          AND NOT (c.enabled AND c.writeback_enabled AND c.writeback_verified_at IS NOT NULL)
        """
    ).fetchone()["n"])


def _send_group(conn, client, group: list[dict[str, Any]], now: datetime, stats: dict[str, Any]) -> None:
    """同一连接、同一张表的到期行：先定位记录（结果表不同于来源表时），再按 500 条一批发送。"""
    head = group[0]
    app_token, table_id = head["app_token"], head["table_id"]
    unresolved = [r for r in group if not r["record_id"]]
    if unresolved:
        _resolve_records(conn, client, head, unresolved, now, stats)
    ready: dict[str, dict[str, Any]] = {}
    for r in group:
        if not r["record_id"]:
            continue
        prev = ready.get(r["record_id"])
        if prev is None or r["id"] > prev["id"]:
            ready[r["record_id"]] = r       # 同一记录只发最新一行，其余留到下次
    rows = sorted(ready.values(), key=lambda r: r["id"])
    for start in range(0, len(rows), BATCH_UPDATE_MAX):
        _send_chunk(conn, client, app_token, table_id, rows[start:start + BATCH_UPDATE_MAX], now, stats)


def _key_columns(field_map: dict[str, Any] | None) -> list[str]:
    cols = (field_map or {}).get("upstream_order_no")
    if isinstance(cols, str):
        return [cols] if cols else []
    return [c for c in (cols or []) if isinstance(c, str) and c]


def _resolve_records(conn, client, head: dict[str, Any], rows: list[dict[str, Any]], now: datetime,
                     stats: dict[str, Any]) -> None:
    """结果表与来源表不同：按平台订单号在结果表里找记录，找到唯一一条就写进 record_id。"""
    try:
        fields = client.list_fields(head["app_token"], head["table_id"])
        types = feishu_fields.field_types_of(fields)
        cols = [c for c in _key_columns(head["field_map"]) if c in types]
        if not cols:
            for r in rows:
                _final_fail(conn, r, now, "结果表里没有平台订单号列，无法定位要写回的记录", stats)
            return
        index: dict[str, list[str]] = {}
        for rec in client.search_records(head["app_token"], head["table_id"], field_names=cols):
            vals = feishu_fields.record_to_values(rec["fields"], {"upstream_order_no": cols}, types)
            key = normalize.normalize(vals).upstream_order_no
            if key:
                index.setdefault(key, []).append(rec["record_id"])
    except FeishuError as exc:
        for r in rows:
            _failed_try(conn, r, now, _error_text(exc), stats)
        return
    for r in rows:
        ids = index.get(r["upstream_order_no"]) or []
        if len(ids) == 1:
            r["record_id"] = ids[0]
            conn.execute("UPDATE procure.writebacks SET record_id = %s WHERE id = %s AND workspace_id = %s",
                         (ids[0], r["id"], r["workspace_id"]))
        elif not ids:
            _failed_try(conn, r, now, f"结果表里找不到平台订单号 {r['upstream_order_no']} 的记录", stats)
        else:
            _final_fail(conn, r, now, f"结果表里有 {len(ids)} 条平台订单号 {r['upstream_order_no']} 的记录，"
                                      "无法确定写回哪一条", stats)


#: 只与个别记录 / 个别值有关的错误：整批被拒时逐条重发能找出出错的那条（记录不存在、单元格值转换失败）。
#: 权限、表不存在等整表级错误逐条重发也一样失败，不拆分。
RECORD_LEVEL_CODES = frozenset({1254043, *range(1254060, 1254070)})


def _error_text(exc: FeishuError) -> str:
    return f"飞书写回失败：{exc.msg}（错误码 {exc.code}）"


def _send_chunk(conn, client, app_token: str, table_id: str, rows: list[dict[str, Any]], now: datetime,
                stats: dict[str, Any]) -> None:
    payload = [{"record_id": r["record_id"], "fields": r["fields"]} for r in rows]
    try:
        client.batch_update_records(app_token, table_id, payload)
    except FeishuError as exc:
        if len(rows) > 1 and exc.code in RECORD_LEVEL_CODES:
            for r in rows:                       # 整批被拒：逐条重发，找出出错的那条
                _send_chunk(conn, client, app_token, table_id, [r], now, stats)
            return
        log.warning("飞书写回失败（%s 条）：%s", len(rows), exc)
        for r in rows:
            _failed_try(conn, r, now, _error_text(exc), stats)
        return
    stats["sent"] += len(rows)
    for r in rows:
        _mark_done(conn, r, now, stats)


def _mark_done(conn, r: dict[str, Any], now: datetime, stats: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE procure.writebacks SET status = 'done', done_at = %s, tries = tries + 1, last_error = NULL "
        "WHERE id = %s AND workspace_id = %s",
        (now, r["id"], r["workspace_id"]),
    )
    stats["done"] += 1
    stats["superseded"] += _supersede_older(conn, r["workspace_id"], r["purchase_order_id"], r["connection_id"],
                                            r["result_version"])
    events.record(conn, r["workspace_id"], "order", r["purchase_order_id"], "order.writeback_done",
                  detail={"writeback_id": r["id"], "connection_id": r["connection_id"], "table_id": r["table_id"],
                          "record_id": r["record_id"], "result_version": r["result_version"],
                          "columns": sorted(r["fields"])})


def _failed_try(conn, r: dict[str, Any], now: datetime, error: str, stats: dict[str, Any]) -> None:
    tries = int(r["tries"]) + 1
    if tries >= MAX_TRIES:
        conn.execute("UPDATE procure.writebacks SET status = 'failed', tries = %s, last_error = %s WHERE id = %s "
                     "AND workspace_id = %s", (tries, error, r["id"], r["workspace_id"]))
        stats["failed"] += 1
        events.record(conn, r["workspace_id"], "order", r["purchase_order_id"], "order.writeback_failed",
                      detail={"writeback_id": r["id"], "tries": tries, "error": error})
        return
    conn.execute("UPDATE procure.writebacks SET tries = %s, last_error = %s, next_try_at = %s WHERE id = %s "
                 "AND workspace_id = %s",
                 (tries, error, now + timedelta(minutes=backoff_minutes(tries)), r["id"], r["workspace_id"]))
    stats["retrying"] += 1


def _final_fail(conn, r: dict[str, Any], now: datetime, error: str, stats: dict[str, Any]) -> None:
    conn.execute("UPDATE procure.writebacks SET status = 'failed', tries = tries + 1, last_error = %s WHERE id = %s "
                 "AND workspace_id = %s", (error, r["id"], r["workspace_id"]))
    stats["failed"] += 1
    events.record(conn, r["workspace_id"], "order", r["purchase_order_id"], "order.writeback_failed",
                  detail={"writeback_id": r["id"], "error": error})


# ── 连接改动的连带处理（供 services/feishu_connections 调用）────────────────────────


def fail_pending_for_target(conn, workspace_id: int, connection_id: int, table_id: str, reason: str) -> int:
    """输入：工作区、连接、旧结果表、原因 → 输出：被标 failed 的 pending 行数（改绑不改投新表）。"""
    cur = conn.execute(
        "UPDATE procure.writebacks SET status = 'failed', last_error = %s WHERE workspace_id = %s "
        "AND connection_id = %s AND table_id = %s AND status = 'pending'",
        (reason, workspace_id, connection_id, table_id),
    )
    return cur.rowcount or 0


def rebuild_pending(conn, workspace_id: int, connection_id: int) -> dict[str, int]:
    """输入：工作区、连接（写回列刚改过）→ 输出：{rebuilt, failed}：按新的写回对应重建当前结果表上 pending 行的 fields。"""
    c = conn.execute("SELECT * FROM procure.feishu_connections WHERE id = %s AND workspace_id = %s",
                     (connection_id, workspace_id)).fetchone()
    out = {"rebuilt": 0, "failed": 0}
    if c is None:
        return out
    rows = conn.execute(
        "SELECT * FROM procure.writebacks WHERE workspace_id = %s AND connection_id = %s AND status = 'pending' "
        "AND table_id = %s FOR UPDATE",
        (workspace_id, connection_id, c["result_table_id"]),
    ).fetchall()
    for w in rows:
        fields, error = _rebuild_fields(conn, workspace_id, w, c)
        if error:
            conn.execute("UPDATE procure.writebacks SET status = 'failed', last_error = %s WHERE id = %s "
                         "AND workspace_id = %s", (error, w["id"], workspace_id))
            out["failed"] += 1
        else:
            conn.execute("UPDATE procure.writebacks SET fields = %s WHERE id = %s AND workspace_id = %s",
                         (Jsonb(fields), w["id"], workspace_id))
            out["rebuilt"] += 1
    return out


def _rebuild_fields(conn, ws: int, w: dict[str, Any], c: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """输入：写回行、连接行 → 输出：(按当前对应重建的 fields, 错误)；任务结果已变或不该写回时给错误。"""
    order = conn.execute("SELECT * FROM procure.purchase_orders WHERE id = %s AND workspace_id = %s",
                         (w["purchase_order_id"], ws)).fetchone()
    att = _eligible_attempt(conn, ws, order)
    if att is None:
        return {}, "任务已不是本系统的真实采购成功结果，不再写回"
    try:
        fields = _build(att, c["writeback_map"], c["writeback_field_types"])
    except DomainError as exc:
        return {}, f"写回列配置有误：{exc.message}"
    if not fields:
        return {}, "按当前写回对应没有可写的结果字段"
    return fields, None


def counts_by_connection(conn, workspace_id: int) -> dict[int, dict[str, int]]:
    """输入：工作区 → 输出：{连接 id: {pending, failed, done}}。"""
    out: dict[int, dict[str, int]] = {}
    for r in conn.execute(
        "SELECT connection_id, status, count(*) AS n FROM procure.writebacks WHERE workspace_id = %s "
        "AND status IN ('pending', 'failed', 'done') GROUP BY connection_id, status",
        (workspace_id,),
    ).fetchall():
        out.setdefault(r["connection_id"], {"pending": 0, "failed": 0, "done": 0})[r["status"]] = int(r["n"])
    return out


# ── 列表与重试 ────────────────────────────────────────────────────────────────

_LIST_SELECT = """
SELECT w.*, o.upstream_order_no, c.name AS connection_name, c.result_table_id,
       (c.enabled AND c.writeback_enabled AND c.writeback_verified_at IS NOT NULL) AS active
FROM procure.writebacks w
JOIN procure.purchase_orders o ON o.id = w.purchase_order_id AND o.workspace_id = w.workspace_id
JOIN procure.feishu_connections c ON c.id = w.connection_id AND c.workspace_id = w.workspace_id
"""


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _out(r: dict[str, Any]) -> dict[str, Any]:
    """输入：带任务号、连接名的写回行 → 输出：契约 §10「写回」形状（另附 table_id、waiting_verification、target_changed）。"""
    return {
        "id": r["id"], "purchase_order_id": r["purchase_order_id"], "upstream_order_no": r["upstream_order_no"],
        "connection_id": r["connection_id"], "connection_name": r["connection_name"], "table_id": r["table_id"],
        "record_id": r["record_id"] or None, "result_version": r["result_version"], "fields": r["fields"],
        "status": r["status"], "tries": r["tries"], "last_error": r["last_error"],
        "next_try_at": _iso(r["next_try_at"]), "created_at": _iso(r["created_at"]), "done_at": _iso(r["done_at"]),
        "waiting_verification": r["status"] == "pending" and not r["active"],
        "target_changed": r["table_id"] != r["result_table_id"],
    }


def _page(page: Any, page_size: Any) -> tuple[int, int]:
    try:
        page, page_size = int(page), int(page_size)
    except (TypeError, ValueError):
        raise validation("分页参数必须是整数") from None
    if page < 1:
        raise validation("页码从 1 开始", field="page")
    return page, max(1, min(page_size, 200))


def list_writebacks(conn, workspace_id: int, status: str | None = None, page: int = 1,
                    page_size: int = 20) -> dict[str, Any]:
    """输入：工作区、状态（pending / done / failed / superseded，缺省全部）、分页 → 输出：{total, rows}（新的在前）。"""
    page, page_size = _page(page, page_size)
    where, params = ["w.workspace_id = %s"], [workspace_id]
    if status:
        if status not in STATUSES:
            raise validation(f"未知的写回状态：{status}", field="status")
        where.append("w.status = %s")
        params.append(status)
    sql_where = " AND ".join(where)
    total = conn.execute(f"SELECT count(*) AS n FROM procure.writebacks w WHERE {sql_where}", params).fetchone()["n"]
    rows = conn.execute(f"{_LIST_SELECT} WHERE {sql_where} ORDER BY w.created_at DESC, w.id DESC LIMIT %s OFFSET %s",
                        [*params, page_size, (page - 1) * page_size]).fetchall()
    return {"total": int(total), "rows": [_out(r) for r in rows]}


def get(conn, workspace_id: int, writeback_id: int) -> dict[str, Any]:
    """输入：工作区、写回 id → 输出：写回形状；不存在或属于别的工作区 → NOT_FOUND。"""
    r = conn.execute(f"{_LIST_SELECT} WHERE w.workspace_id = %s AND w.id = %s", (workspace_id, writeback_id)).fetchone()
    if r is None:
        raise not_found("写回记录")
    return _out(r)


def retry(conn, workspace_id: int, writeback_id: int, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、写回 id、操作人 → 输出：写回形状（重新排队后的这一行，或取代它的新版本行）。

    只重试 failed / pending 行（只补写回，不重跑采购）：按当前写回对应与列类型重建 fields，tries 清零、立即到期。
    已 done / superseded → VALIDATION；连接已改绑结果表 → VALIDATION（旧结果不改投新表）；
    同一任务已有更新版本 → 本行标 superseded 并返回；任务结果已变但新版本尚未入队 → 本行标 superseded，入队当前版本。
    """
    w = conn.execute("SELECT * FROM procure.writebacks WHERE id = %s AND workspace_id = %s FOR UPDATE",
                     (writeback_id, workspace_id)).fetchone()
    if w is None:
        raise not_found("写回记录")
    if w["status"] == "done":
        raise validation("这条写回已成功，无需重试")
    if w["status"] == "superseded":
        raise validation("这条写回已被更新的采购结果取代，不再发送")
    c = conn.execute("SELECT * FROM procure.feishu_connections WHERE id = %s AND workspace_id = %s",
                     (w["connection_id"], workspace_id)).fetchone()
    if c is None:
        raise not_found("飞书连接")
    if w["table_id"] != c["result_table_id"]:
        raise validation("连接的结果表已改绑，旧结果不会改投新表；请到原结果表核对后人工处理")
    newer = conn.execute(
        "SELECT 1 FROM procure.writebacks WHERE workspace_id = %s AND purchase_order_id = %s AND connection_id = %s "
        "AND result_version > %s LIMIT 1",
        (workspace_id, w["purchase_order_id"], w["connection_id"], w["result_version"])).fetchone()
    order = conn.execute("SELECT * FROM procure.purchase_orders WHERE id = %s AND workspace_id = %s",
                         (w["purchase_order_id"], workspace_id)).fetchone()
    if newer is not None or (order is not None and order["result_version"] > w["result_version"]):
        conn.execute("UPDATE procure.writebacks SET status = 'superseded' WHERE id = %s AND workspace_id = %s",
                     (w["id"], workspace_id))
        new_id = None if newer is not None else enqueue_for_order(conn, workspace_id, w["purchase_order_id"])
        events.record(conn, workspace_id, "order", w["purchase_order_id"], "order.writeback_superseded",
                      actor_kind="user" if actor_user_id else "system", actor_id=actor_user_id,
                      detail={"writeback_id": w["id"], "new_writeback_id": new_id})
        return get(conn, workspace_id, new_id or w["id"])
    fields, error = _rebuild_fields(conn, workspace_id, w, c)
    if error:
        raise validation(f"不能重试：{error}")
    conn.execute(
        "UPDATE procure.writebacks SET status = 'pending', tries = 0, last_error = NULL, next_try_at = %s, fields = %s "
        "WHERE id = %s AND workspace_id = %s",
        (clock.now(), Jsonb(fields), w["id"], workspace_id),
    )
    events.record(conn, workspace_id, "order", w["purchase_order_id"], "order.writeback_retry",
                  actor_kind="user" if actor_user_id else "system", actor_id=actor_user_id,
                  detail={"writeback_id": w["id"], "previous_status": w["status"], "previous_error": w["last_error"]})
    return get(conn, workspace_id, w["id"])
