"""飞书读取（契约 §6「读取」「准入」、§9 定时读取）：读来源表 → 规范化 → 准入 → orders.ingest → 读取记录 → 分配。

一次读取分两段：

1. **读飞书**（只读网络，不写业务数据）：读字段类型、按字段对应只读用到的列、读完来源表（或视图）的全部记录。
   这一段出错（飞书接口错误、平台订单号列不见了）→ 记一条失败的 feishu_sync_runs、更新连接的 last_sync_at / last_error，
   再抛 DomainError（FEISHU_ERROR 或 VALIDATION，detail 带 run_id）。**失败记录写在调用方的事务里，调用方要先 commit
   再把错误返回**，否则失败历史随回滚丢失（workflows/feishu_intake、scheduler、立即读取接口都这样处理）。
2. **接收**（写库）：逐条判定（见 evaluate_records），每条在自己的保存点里规范化与 ingest，任何异常只让该条记为 invalid，
   不影响其他条；成功后记读取记录与 last_stats；有新单 / 更新 / 冲突 / 历史时触发一次分配（trigger='intake'）。
   这一段出现意料之外的异常（如数据库错误）→ 回滚本次接收的全部写入，同样记失败的读取记录后抛 DomainError
   （INTERNAL，detail 带 run_id），连接的 last_sync_at 照常前进，不会每 30 秒重读一次。

判定规则（evaluate_records，样本预览也用它）：

- 缺平台订单号 → invalid；同一次读取里平台订单号重复 → 只采用一条（优先已关联到该任务的记录），其余 invalid；
- 库里已有的单（同键任务，或这条记录已关联过任务）→ 一律交给 ingest（更新、取消、人审撤回、外部单号都要传达）；
  例外：正式读取时，规范化结果与这条记录上次的完全相同 → 直接 unchanged（只更新快照与 last_seen_at，不锁任务行）；
- 库里没有的单 → 按 feishu_connections.new_record_skip_reason 判断是否跳过（采购方、上游取消、历史单号、已拍单、
  人审未通过），跳过的计 skipped 并写明原因；其余 ingest 新建。已有的单把准入规则一并交给 ingest（采购方移走、
  采购状态变为已拍单 / 拿回 → 开始前转待核实）。
- 连接的写回验收测试记录（writeback_test_record）一律 skipped：验收写进去的测试值不能被当成订单结果接收。

来源快照 = 本次读到的已对应列（列名 → 可读值）。本模块不 commit。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from services import allocation, clock, feishu_connections, feishu_fields, orders
from services.errors import DomainError, conflict, validation
from services.models import SourceRef, UpstreamRecord

log = logging.getLogger(__name__)

STAT_KEYS = ("read", "new", "update", "unchanged", "conflict", "historical", "skipped", "invalid")
TRIGGERS = ("schedule", "manual")
#: 读取记录里保留的问题行样本条数
PROBLEM_SAMPLES = 100
_LOCK_NS = feishu_connections.INTAKE_LOCK_NS   # 同一连接同一时刻只有一次读取（写回验收也持有它）


def empty_stats() -> dict[str, int]:
    """输入：无 → 输出：{read, new, update, unchanged, conflict, historical, skipped, invalid} 全为 0。"""
    return {k: 0 for k in STAT_KEYS}


# ── 判定 ─────────────────────────────────────────────────────────────────────


def _normalize_one(rec: dict[str, Any], field_map: dict[str, Any], field_types: dict[str, int],
                   admission: dict[str, Any]) -> tuple[UpstreamRecord, str | None]:
    """一条记录 → (规范化结果, None)；规范化本身出错 → (空记录, 原因)。一条坏数据不能拖垮整次读取。"""
    try:
        return feishu_fields.normalize_record(rec.get("fields") or {}, field_map, field_types, admission), None
    except Exception as exc:  # noqa: BLE001 - 逐条兜底
        log.warning("飞书记录 %s 规范化失败：%s", rec.get("record_id"), type(exc).__name__)
        return UpstreamRecord(upstream_order_no="", issues=[f"无法解析：{type(exc).__name__}"]), \
            f"记录内容无法解析（{type(exc).__name__}）"


def evaluate_records(conn, workspace_id: int, connection_id: int, records: list[dict[str, Any]],
                     field_map: dict[str, Any], field_types: dict[str, int], admission: dict[str, Any], *,
                     dry_run: bool, skip_record_ids: set[str] | frozenset[str] | None = None) -> list[dict[str, Any]]:
    """输入：工作区、连接 id（预览可为占位 id）、records/search 的记录 [{record_id, fields}]、字段对应、列类型、准入规则、
    是否只预演、要跳过的记录 id（写回验收测试记录）→ 输出：每条记录一个
    {record_id, upstream_order_no, verdict, reason, record, issues, status, order_id}。

    verdict 取 new / update / unchanged / conflict / historical / skipped / invalid。dry_run=True 时不写库。
    每条记录的规范化与 ingest 各自兜底：任何异常只让这一条成为 invalid（写明原因），其余照常。
    """
    skip_ids = set(skip_record_ids or ())
    normalized = [_normalize_one(rec, field_map, field_types, admission) for rec in records]
    items: list[tuple[dict[str, Any], UpstreamRecord]] = [(rec, n[0]) for rec, n in zip(records, normalized)]
    broken = {i: n[1] for i, n in enumerate(normalized) if n[1]}
    keys = sorted({r.upstream_order_no for _, r in items if r.upstream_order_no})
    known_keys = {row["upstream_order_no"] for row in conn.execute(
        "SELECT upstream_order_no FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = ANY(%s)",
        (workspace_id, keys),
    ).fetchall()} if keys else set()
    record_ids = [rec.get("record_id") for rec, _ in items if rec.get("record_id")]
    links = {row["feishu_record_id"]: row for row in conn.execute(
        """
        SELECT s.id, s.feishu_record_id, s.normalized, s.purchase_order_id, o.upstream_order_no, o.status
        FROM procure.order_sources s
        JOIN procure.purchase_orders o ON o.id = s.purchase_order_id AND o.workspace_id = s.workspace_id
        WHERE s.workspace_id = %s AND s.kind = 'feishu' AND s.feishu_connection_id = %s AND s.feishu_record_id = ANY(%s)
        """,
        (workspace_id, connection_id, record_ids),
    ).fetchall()} if record_ids else {}
    linked = {rid: row["upstream_order_no"] for rid, row in links.items()}
    seen_unchanged: list[tuple[Jsonb, Any, int, int]] = []

    groups: dict[str, list[int]] = {}
    for i, (_, r) in enumerate(items):
        if r.upstream_order_no:
            groups.setdefault(r.upstream_order_no, []).append(i)
    winner = {key: next((i for i in idxs if linked.get(items[i][0].get("record_id")) == key), idxs[0])
              for key, idxs in groups.items()}

    out: list[dict[str, Any]] = []
    for i, (rec, r) in enumerate(items):
        rid = rec.get("record_id")
        key = r.upstream_order_no
        base = {"record_id": rid, "upstream_order_no": key, "record": r.to_json(), "issues": list(r.issues),
                "status": None, "order_id": None}
        if not rid:
            out.append({**base, "verdict": "invalid", "reason": "飞书没有返回记录 id"})
            continue
        if rid in skip_ids:
            out.append({**base, "verdict": "skipped", "reason": "这是写回验收用的测试记录，不接收为订单"})
            continue
        if i in broken:
            out.append({**base, "verdict": "invalid", "reason": broken[i]})
            continue
        if not key:
            out.append({**base, "verdict": "invalid", "reason": r.issues[0] if r.issues else "缺少平台订单号"})
            continue
        if winner[key] != i:
            other = items[winner[key]][0].get("record_id")
            out.append({**base, "verdict": "invalid",
                        "reason": f"平台订单号 {key} 与记录 {other} 重复，本次只采用那一条"})
            continue
        if key not in known_keys and rid not in linked:
            skip = feishu_connections.new_record_skip_reason(r, admission)
            if skip:
                out.append({**base, "verdict": "skipped", "reason": skip})
                continue
        snapshot = feishu_fields.snapshot_fields(rec.get("fields") or {}, field_types)
        link = links.get(rid)
        if (not dry_run and link is not None and link["upstream_order_no"] == key
                and link["normalized"] == r.to_json()):
            # 与上次读到的规范化记录完全相同：ingest 的结论必然是 unchanged。直接记 last_seen_at，
            # 不再逐条锁任务行（大表定时读取时不挡插件领取）。
            seen_unchanged.append((Jsonb(snapshot), clock.now(), link["id"], workspace_id))
            out.append({**base, "verdict": "unchanged", "reason": None, "status": link["status"],
                        "order_id": link["purchase_order_id"]})
            continue
        src = SourceRef(kind="feishu", snapshot=snapshot, connection_id=connection_id, record_id=rid)
        try:
            with conn.transaction():
                outcome = orders.ingest(conn, workspace_id, r, src, dry_run=dry_run, admission=admission)
        except Exception as exc:  # noqa: BLE001 - 逐条兜底：任何异常只让这一条失败（保存点已回滚它的写入）
            msg = exc.message if isinstance(exc, DomainError) else type(exc).__name__
            if isinstance(exc, (DomainError, psycopg.Error)):
                log.warning("飞书记录 %s（%s）接收失败：%s", rid, key, msg)
            else:
                log.exception("飞书记录 %s（%s）接收失败", rid, key)
            out.append({**base, "verdict": "invalid", "reason": f"接收失败：{msg}"})
            continue
        out.append({**base, "verdict": outcome.verdict, "reason": outcome.reason, "issues": list(outcome.issues),
                    "status": outcome.status, "order_id": outcome.order_id})
    if seen_unchanged:
        with conn.cursor() as cur:
            cur.executemany("UPDATE procure.order_sources SET snapshot = %s, last_seen_at = %s "
                            "WHERE id = %s AND workspace_id = %s", seen_unchanged)
    return out


# ── 一次读取 ──────────────────────────────────────────────────────────────────


def _record_failure(conn, ws: int, connection_id: int, run_id: int, error: str, now: datetime) -> None:
    conn.execute("UPDATE procure.feishu_sync_runs SET finished_at = %s, ok = false, error = %s WHERE id = %s "
                 "AND workspace_id = %s", (now, error, run_id, ws))
    conn.execute("UPDATE procure.feishu_connections SET last_sync_at = %s, last_error = %s WHERE id = %s "
                 "AND workspace_id = %s", (now, error, connection_id, ws))


def run_connection(conn, workspace_id: int, connection_id: int, *, trigger: str,
                   client: Any | None = None) -> dict[str, Any]:
    """输入：工作区、连接 id、触发方式（schedule / manual）、可选飞书客户端 →
    输出：{read, new, update, unchanged, conflict, historical, skipped, invalid, run_id, problems, allocation}。

    连接已停用 → VALIDATION；同一连接正在读取 → CONFLICT；表格已不再授权给本工作区 → 记失败后抛 FORBIDDEN；
    读飞书失败 → 记失败后抛 FEISHU_ERROR（见模块说明，调用方需 commit 才能保留失败记录）；
    飞书未配置 → FEISHU_NOT_CONFIGURED（不记读取记录）。
    """
    if trigger not in TRIGGERS:
        raise validation(f"未知的读取触发方式：{trigger}", field="trigger")
    row = feishu_connections.get_row(conn, workspace_id, connection_id)
    if not row["enabled"]:
        raise validation("连接已停用，请先启用再读取")
    got = conn.execute("SELECT pg_try_advisory_xact_lock(%s::int, %s::int) AS ok",
                       (_LOCK_NS, row["id"] % 2_147_483_647)).fetchone()["ok"]
    if not got:
        raise conflict("这个连接正在读取中，请稍后再试")
    cid = row["id"]
    with feishu_connections.client_scope(client) as c:
        started = clock.now()
        run_id = conn.execute(
            "INSERT INTO procure.feishu_sync_runs (workspace_id, connection_id, trigger, started_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (workspace_id, cid, trigger, started),
        ).fetchone()["id"]
        try:
            feishu_connections.require_base_access(conn, workspace_id, row["app_token"])
            field_map = feishu_connections.clean_field_map(row["field_map"])
            types, cols = feishu_connections.read_fields_for(c, row["app_token"], row["source_table_id"], field_map)
            with feishu_connections.feishu_guard("读取飞书记录"):
                records = list(c.search_records(row["app_token"], row["source_table_id"], field_names=cols,
                                                view_id=row["source_view_id"]))
        except DomainError as exc:
            if exc.code == "FEISHU_NOT_CONFIGURED":
                raise
            _record_failure(conn, workspace_id, cid, run_id, exc.message, clock.now())
            raise DomainError(exc.code, exc.message, exc.http_status,
                              {**exc.detail, "run_id": run_id, "connection_id": cid}) from exc

    admission = feishu_connections.clean_admission(row["admission"])
    skip = {row["writeback_test_record"]} if row.get("writeback_test_record") else set()
    try:
        with conn.transaction():
            results = evaluate_records(conn, workspace_id, cid, records, field_map, types, admission, dry_run=False,
                                       skip_record_ids=skip)
    except Exception as exc:  # noqa: BLE001 - 接收阶段的意外异常：记失败（调用方提交），不让连接每 30 秒重读
        log.exception("飞书连接 %s 接收记录时出错", cid)
        msg = f"接收飞书记录时出错（{type(exc).__name__}），本次读取未写入任何任务"
        _record_failure(conn, workspace_id, cid, run_id, msg, clock.now())
        raise DomainError("INTERNAL", msg, 500, {"run_id": run_id, "connection_id": cid}) from exc
    stats = empty_stats()
    problems: list[dict[str, Any]] = []
    for r in results:
        stats["read"] += 1
        stats[r["verdict"]] += 1
        if r["verdict"] in ("skipped", "invalid", "conflict") and len(problems) < PROBLEM_SAMPLES:
            problems.append({"record_id": r["record_id"], "upstream_order_no": r["upstream_order_no"] or None,
                             "verdict": r["verdict"], "reason": r["reason"]})
    now = clock.now()
    conn.execute("UPDATE procure.feishu_sync_runs SET finished_at = %s, ok = true, stats = %s WHERE id = %s "
                 "AND workspace_id = %s", (now, Jsonb({**stats, "problems": problems}), run_id, workspace_id))
    conn.execute("UPDATE procure.feishu_connections SET last_sync_at = %s, last_sync_ok_at = %s, last_error = NULL, "
                 "last_stats = %s WHERE id = %s AND workspace_id = %s",
                 (now, now, Jsonb(stats), cid, workspace_id))
    alloc = None
    if stats["new"] or stats["update"] or stats["conflict"] or stats["historical"]:
        alloc = allocation.allocate(conn, workspace_id, trigger="intake")
    return {**stats, "run_id": run_id, "problems": problems, "allocation": alloc}


def due_connections(conn, now: datetime | None = None) -> list[tuple[int, int]]:
    """输入：连接、当前时刻（缺省 clock.now()）→ 输出：[(workspace_id, connection_id)]：启用且距上次读取已满 poll_minutes 的连接
    （从没读过的在前）。跨工作区（后台调度用）。"""
    now = now or clock.now()
    rows = conn.execute(
        """
        SELECT workspace_id, id FROM procure.feishu_connections
        WHERE enabled AND (last_sync_at IS NULL OR last_sync_at + make_interval(mins => poll_minutes) <= %s)
        ORDER BY last_sync_at NULLS FIRST, id
        """,
        (now,),
    ).fetchall()
    return [(r["workspace_id"], r["id"]) for r in rows]
