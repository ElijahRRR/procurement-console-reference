"""文件导入（契约 §7、§10「导入」、v1 §6.2）：上传 → 选工作表与字段对应 → 预览 → 提交 / 放弃，问题行下载，个人模板。

- **上传**：只读取字节（sheet_io.read_workbook 校验格式与大小），原件存进 import_batches.file_bytes（只读副本，
  用于重新预览）；不改用户的原文件。给出第一个有表头的工作表的自动对应（file_mapping.suggest_mapping）。
- **预览**：重新读原件，逐行规范化；结论（每行一个）new / update / unchanged / conflict / historical / invalid：
  缺平台订单号 → invalid；同批次同键重复 → 第一行有效，其余 invalid；库里还没有的单命中准入规则
  （采购状态「已拍单」却没有 AMZ 订单号等，见 normalize.admission_skip_reason）→ invalid 并写明原因；
  其余经 orders.ingest(dry_run=True) 得出结论。问题（issues）里写明「停在待确认」「停在资料待补齐」等。
  文件的准入规则缺省「导入历史采购」= 是（带 AMZ 订单号的行按历史采购登记，不重新下单）。
  每次预览整体替换该批次的 import_rows；结论原因与预计状态存在 normalized._preview 里（不改表结构）。
- **提交**：只能从 previewed 提交一次（再次提交 → CONFLICT）；逐行（invalid 除外）经 orders.ingest 写入，
  记下 purchase_order_id 与实际结论；完成后触发一次分配（trigger='import'）。
- **问题行**：invalid / conflict / historical 或有问题的行，原列 + 结论 + 原因（+ 原行号）导出为 xlsx。

每个查询都带工作区条件；别的工作区的批次 / 模板一律 NOT_FOUND。本模块不 commit。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from psycopg.types.json import Jsonb

from services import allocation, clock, events, feishu_connections, file_mapping, normalize, orders, sheet_io
from services.errors import DomainError, conflict, not_found, validation
from services.models import SourceRef, UpstreamRecord

VERDICTS = ("new", "update", "unchanged", "conflict", "historical", "invalid")
VERDICT_ZH = {"new": "新单", "update": "更新", "unchanged": "无变化", "conflict": "待核实", "historical": "历史采购",
              "invalid": "无效"}
BATCH_STATUSES = ("uploaded", "previewed", "committed", "discarded")
#: 文件来源的准入缺省：带 AMZ 订单号的行按历史采购登记（v1 §6.2）
FILE_ADMISSION_DEFAULTS: dict[str, Any] = {"import_historical": True}
PROBLEM_VERDICTS = ("invalid", "conflict", "historical")
_PREVIEW_KEY = "_preview"


# ── 小工具 ───────────────────────────────────────────────────────────────────


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _page(page: Any, page_size: Any) -> tuple[int, int]:
    try:
        page, page_size = int(page), int(page_size)
    except (TypeError, ValueError):
        raise validation("分页参数必须是整数") from None
    if page < 1:
        raise validation("页码从 1 开始", field="page")
    return page, max(1, min(page_size, 200))


def _actor(actor_user_id: int | None) -> tuple[str, int | None]:
    return ("user", actor_user_id) if actor_user_id is not None else ("system", None)


def _clean_filename(filename: Any) -> str:
    name = re.split(r"[\\/]", str(filename or ""))[-1].strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:190] + dot + ext) if dot and len(ext) <= 8 else name[:200]
    return name or "upload"


def empty_stats() -> dict[str, int]:
    """输入：无 → 输出：批次统计 {total, new, update, unchanged, conflict, historical, invalid, pending_review, needs_info}。"""
    return {"total": 0, **{v: 0 for v in VERDICTS}, "pending_review": 0, "needs_info": 0}


_BATCH_SELECT = """
SELECT b.id, b.workspace_id, b.filename, b.file_sha256, b.sheet_name, b.mapping, b.status, b.stats, b.uploaded_by,
       b.created_at, b.committed_at, b.committed_by, octet_length(b.file_bytes) AS size,
       COALESCE(u.display_name, u.username) AS uploaded_by_name
FROM procure.import_batches b
LEFT JOIN procure.users u ON u.id = b.uploaded_by
"""


def _batch(conn, ws: int, batch_id: Any, for_update: bool = False) -> dict[str, Any]:
    try:
        bid = int(batch_id)
    except (TypeError, ValueError):
        raise not_found("导入批次") from None
    if for_update:
        conn.execute("SELECT id FROM procure.import_batches WHERE id = %s AND workspace_id = %s FOR UPDATE",
                     (bid, ws))
    row = conn.execute(f"{_BATCH_SELECT} WHERE b.id = %s AND b.workspace_id = %s", (bid, ws)).fetchone()
    if row is None:
        raise not_found("导入批次")
    return row


def batch_out(row: dict[str, Any]) -> dict[str, Any]:
    """输入：批次行（带上传人名与大小）→ 输出：契约 §10「批次」形状（另附 mapping、size）。"""
    return {"id": row["id"], "filename": row["filename"], "sheet_name": row["sheet_name"], "status": row["status"],
            "stats": row["stats"], "mapping": row["mapping"], "size": row.get("size"),
            "created_at": _iso(row["created_at"]), "committed_at": _iso(row["committed_at"]),
            "uploaded_by_name": row.get("uploaded_by_name")}


def get_batch(conn, workspace_id: int, batch_id: int) -> dict[str, Any]:
    """输入：工作区、批次 id → 输出：批次形状；跨工作区 → NOT_FOUND。"""
    return batch_out(_batch(conn, workspace_id, batch_id))


def _file_admission(admission: dict[str, Any] | None) -> dict[str, Any]:
    return feishu_connections.clean_admission({**FILE_ADMISSION_DEFAULTS, **(admission or {})})


# ── 上传 ─────────────────────────────────────────────────────────────────────


def _match_template(templates: list[dict[str, Any]], headers: list[str]) -> int | None:
    """表头覆盖了模板全部已对应列、且含平台订单号列的模板中，对应列最多的那个。"""
    best, best_n = None, 0
    present = set(headers)
    for t in templates:
        mapped = {h for h, k in (t["mapping"] or {}).items() if k}
        if not mapped or not mapped <= present or "upstream_order_no" not in (t["mapping"] or {}).values():
            continue
        if len(mapped) > best_n:
            best, best_n = t["id"], len(mapped)
    return best


def upload(conn, workspace_id: int, *, filename: str, data: bytes, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、原文件名、文件字节、上传人 → 输出：{batch_id, filename, sheets: [{name, row_count, headers}], sheet,
    headers, suggested_mapping: {表头: 系统字段 | None}, system_fields, templates: [{id, name}], matched_template_id,
    previous_upload}。

    文件为空、过大、格式不支持、没有任何表头 → VALIDATION。previous_upload 为同一工作区里内容完全相同的最近一次上传
    （{id, status, created_at}，没有为 None），提醒「这个文件已经导入过」。
    """
    name = _clean_filename(filename)
    if isinstance(data, memoryview):
        data = data.tobytes()
    if not isinstance(data, (bytes, bytearray)):
        raise validation("上传内容不是文件")
    data = bytes(data)
    sheets = sheet_io.read_workbook(data, name)
    first = next((s for s in sheets if s.headers), None)
    if first is None:
        raise validation("文件里没有任何数据（所有工作表都是空的）")
    suggested = file_mapping.suggest_mapping(first.headers)
    sha = hashlib.sha256(data).hexdigest()
    prev = conn.execute(
        "SELECT id, status, created_at FROM procure.import_batches WHERE workspace_id = %s AND file_sha256 = %s "
        "AND status <> 'discarded' ORDER BY id DESC LIMIT 1",
        (workspace_id, sha),
    ).fetchone()
    now = clock.now()
    bid = conn.execute(
        """
        INSERT INTO procure.import_batches (workspace_id, filename, file_sha256, file_bytes, sheet_name, mapping,
                                            status, uploaded_by, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'uploaded', %s, %s) RETURNING id
        """,
        (workspace_id, name, sha, data, first.name, Jsonb(suggested), actor_user_id, now),
    ).fetchone()["id"]
    ak, aid = _actor(actor_user_id)
    events.record(conn, workspace_id, "batch", bid, "batch.uploaded", actor_kind=ak, actor_id=aid,
                  detail={"filename": name, "size": len(data), "sheets": [s.name for s in sheets]})
    templates = list_templates(conn, workspace_id)
    return {
        "batch_id": bid, "filename": name,
        "sheets": [{"name": s.name, "row_count": len(s.rows), "headers": s.headers} for s in sheets],
        "sheet": first.name, "headers": first.headers, "suggested_mapping": suggested,
        "system_fields": [{"key": f["key"], "label": f["label"], "required": f["required"]}
                          for f in normalize.SYSTEM_FIELDS],
        "templates": [{"id": t["id"], "name": t["name"]} for t in templates],
        "matched_template_id": _match_template(templates, first.headers),
        "previous_upload": ({"id": prev["id"], "status": prev["status"], "created_at": _iso(prev["created_at"])}
                            if prev else None),
    }


# ── 预览 ─────────────────────────────────────────────────────────────────────


def _read_sheet(row: dict[str, Any], data: bytes, sheet: str | None) -> sheet_io.Sheet:
    sheets = sheet_io.read_workbook(data, row["filename"])
    name = sheet or row["sheet_name"]
    if name is None:
        found = next((s for s in sheets if s.headers), None)
    else:
        found = next((s for s in sheets if s.name == name), None)
    if found is None:
        raise validation(f"文件里没有工作表「{name}」", field="sheet")
    return found


def _clean_mapping(mapping: Any, headers: list[str]) -> dict[str, str | None]:
    if not isinstance(mapping, dict):
        raise validation("字段对应格式不对", field="mapping")
    present = set(headers)
    out: dict[str, str | None] = {h: None for h in headers}
    for h, k in mapping.items():
        if h in present:
            if k is not None and not isinstance(k, str):
                raise validation("字段对应的值必须是系统字段", field="mapping")
            out[h] = k or None
    file_mapping.validate_mapping(out)
    return out


def _evaluate(conn, ws: int, batch_id: int, sheet: sheet_io.Sheet, mapping: dict[str, str | None],
              admission: dict[str, Any]) -> list[dict[str, Any]]:
    """逐行规范化并预演，返回 import_rows 要写的内容（含 _preview 说明）。不写库。"""
    recs = [(row_no, raw, file_mapping.normalize_row(raw, mapping, admission))
            for raw, row_no in zip(sheet.rows, sheet.row_numbers)]
    keys = sorted({r.upstream_order_no for _, _, r in recs if r.upstream_order_no})
    known = {x["upstream_order_no"] for x in conn.execute(
        "SELECT upstream_order_no FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = ANY(%s)",
        (ws, keys)).fetchall()} if keys else set()
    seen: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for row_no, raw, rec in recs:
        raw_j = sheet_io.row_to_json(raw)
        key = rec.upstream_order_no
        item = {"row_no": row_no, "raw": raw_j, "record": rec, "issues": list(rec.issues), "reason": None,
                "status": None, "order_id": None, "date": _expected_date(rec)}
        if not key:
            out.append({**item, "verdict": "invalid", "reason": "缺少平台订单号"})
            continue
        if key in seen:
            out.append({**item, "verdict": "invalid",
                        "reason": f"与第 {seen[key]} 行的平台订单号重复，同一批次只导入第一行"})
            continue
        seen[key] = row_no
        if key not in known:
            skip = normalize.admission_skip_reason(rec, admission)
            if skip:
                out.append({**item, "verdict": "invalid", "reason": skip})
                continue
        src = SourceRef(kind="file", snapshot=raw_j, batch_id=batch_id, row_no=row_no)
        oc = orders.ingest(conn, ws, rec, src, dry_run=True)
        out.append({**item, "verdict": oc.verdict, "reason": oc.reason, "issues": list(oc.issues),
                    "status": oc.status, "order_id": oc.order_id})
    return out


def _expected_date(rec: UpstreamRecord) -> dict[str, Any]:
    """建议采购日期及依据：来源给出 → source；否则下单北京日期 + 6 − 配送天数 → computed；都没有 → None。"""
    if rec.suggested_purchase_date is not None:
        return {"suggested_purchase_date": rec.suggested_purchase_date.isoformat(), "suggested_date_basis": "source"}
    d = orders.compute_suggested_date(rec.order_time, rec.ship_days)
    return {"suggested_purchase_date": d.isoformat() if d else None,
            "suggested_date_basis": "computed" if d else None}


def _normalized_json(rec: UpstreamRecord, reason: str | None, status: str | None,
                     date: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**rec.to_json(), _PREVIEW_KEY: {"reason": reason, "status": status, **(date or {})}}


def _stats_of(items: list[dict[str, Any]]) -> dict[str, int]:
    stats = empty_stats()
    for it in items:
        stats["total"] += 1
        stats[it["verdict"]] += 1
        if it["verdict"] != "invalid" and it.get("status") in ("pending_review", "needs_info"):
            stats[it["status"]] += 1
    return stats


def preview(conn, workspace_id: int, batch_id: int, *, sheet: str | None = None,
            mapping: dict[str, str | None] | None = None, admission: dict[str, Any] | None = None) -> dict[str, Any]:
    """输入：工作区、批次、工作表名（缺省上次 / 第一个）、{表头: 系统字段 | None}（缺省上次保存的或自动识别）、
    准入规则（缺省：契约 §6 缺省值 + 导入历史采购）→ 输出：{batch, stats, rows: {total, rows}}（rows 为第一页）。

    只能对 uploaded / previewed 的批次预览；字段对应必须含平台订单号（VALIDATION）。整体替换该批次的 import_rows，
    批次状态改为 previewed。不建任务、不改任务。
    """
    b = _batch(conn, workspace_id, batch_id, for_update=True)
    if b["status"] not in ("uploaded", "previewed"):
        raise validation(("已提交" if b["status"] == "committed" else "已放弃") + "的批次不能再预览")
    data = conn.execute("SELECT file_bytes FROM procure.import_batches WHERE id = %s AND workspace_id = %s",
                        (b["id"], workspace_id)).fetchone()["file_bytes"]
    s = _read_sheet(b, bytes(data), sheet)
    if mapping is None:
        mapping = b["mapping"] if (b["mapping"] and s.name == b["sheet_name"]) else file_mapping.suggest_mapping(s.headers)
    mp = _clean_mapping(mapping, s.headers)
    adm = _file_admission(admission)
    items = _evaluate(conn, workspace_id, b["id"], s, mp, adm)
    conn.execute("DELETE FROM procure.import_rows WHERE batch_id = %s", (b["id"],))
    if items:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO procure.import_rows (batch_id, row_no, raw, normalized, verdict, issues, purchase_order_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, NULL)",
                [(b["id"], it["row_no"], Jsonb(it["raw"]),
                  Jsonb(_normalized_json(it["record"], it["reason"], it["status"], it["date"])),
                  it["verdict"], it["issues"]) for it in items],
            )
    stats = _stats_of(items)
    conn.execute("UPDATE procure.import_batches SET sheet_name = %s, mapping = %s, status = 'previewed', stats = %s "
                 "WHERE id = %s AND workspace_id = %s",
                 (s.name, Jsonb(mp), Jsonb(stats), b["id"], workspace_id))
    events.record(conn, workspace_id, "batch", b["id"], "batch.previewed",
                  detail={"sheet": s.name, "stats": stats, "admission": adm})
    return {"batch": get_batch(conn, workspace_id, b["id"]), "stats": stats,
            "rows": rows(conn, workspace_id, b["id"], page=1, page_size=20)}


# ── 批次行 ────────────────────────────────────────────────────────────────────


def _row_out(r: dict[str, Any]) -> dict[str, Any]:
    norm = dict(r["normalized"] or {})
    meta = norm.pop(_PREVIEW_KEY, None) or {}
    return {"row_no": r["row_no"], "verdict": r["verdict"], "verdict_zh": VERDICT_ZH.get(r["verdict"]),
            "issues": list(r["issues"] or []), "reason": meta.get("reason"), "status": meta.get("status"),
            "suggested_purchase_date": meta.get("suggested_purchase_date"),
            "suggested_date_basis": meta.get("suggested_date_basis"),
            "upstream_order_no": norm.get("upstream_order_no") or None, "normalized": norm, "raw": r["raw"],
            "purchase_order_id": r["purchase_order_id"]}


def rows(conn, workspace_id: int, batch_id: int, *, verdict: str | None = None, page: int = 1,
         page_size: int = 20) -> dict[str, Any]:
    """输入：工作区、批次、结论过滤（new … invalid，或 'problems' = 问题行）、分页 → 输出：{total, rows}（按行号）。

    行：{row_no, verdict, verdict_zh, issues, reason, status（预计 / 实际任务状态）, suggested_purchase_date,
    suggested_date_basis, upstream_order_no, normalized, raw, purchase_order_id}。
    """
    b = _batch(conn, workspace_id, batch_id)
    page, page_size = _page(page, page_size)
    where, params = ["batch_id = %s"], [b["id"]]
    if verdict:
        if verdict == "problems":
            where.append("(verdict = ANY(%s) OR cardinality(issues) > 0)")
            params.append(list(PROBLEM_VERDICTS))
        elif verdict in VERDICTS:
            where.append("verdict = %s")
            params.append(verdict)
        else:
            raise validation(f"未知的结论：{verdict}", field="verdict")
    sql_where = " AND ".join(where)
    total = conn.execute(f"SELECT count(*) AS n FROM procure.import_rows WHERE {sql_where}", params).fetchone()["n"]
    found = conn.execute(f"SELECT * FROM procure.import_rows WHERE {sql_where} ORDER BY row_no LIMIT %s OFFSET %s",
                         [*params, page_size, (page - 1) * page_size]).fetchall()
    return {"total": int(total), "rows": [_row_out(r) for r in found]}


# ── 提交 / 放弃 / 列表 ─────────────────────────────────────────────────────────


def commit(conn, workspace_id: int, batch_id: int, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、批次、操作人 → 输出：{batch, stats, allocation}。

    只能提交 previewed 的批次，且只能提交一次（已提交 → CONFLICT；未预览 / 已放弃 → VALIDATION）。逐行（invalid 除外）
    经 orders.ingest 写入（一行出错只把该行记为 invalid 并写明原因），记下 purchase_order_id 与实际结论，重算统计，
    批次改为 committed，最后触发一次分配（trigger='import'）。
    """
    b = _batch(conn, workspace_id, batch_id, for_update=True)
    if b["status"] == "committed":
        raise conflict("这个批次已经提交过了，不能重复提交")
    if b["status"] == "discarded":
        raise validation("已放弃的批次不能提交")
    if b["status"] != "previewed":
        raise validation("请先预览再提交")
    ak, aid = _actor(actor_user_id)
    found = conn.execute("SELECT * FROM procure.import_rows WHERE batch_id = %s ORDER BY row_no", (b["id"],)).fetchall()
    items: list[dict[str, Any]] = []
    for r in found:
        norm = dict(r["normalized"] or {})
        meta = norm.pop(_PREVIEW_KEY, None) or {}
        if r["verdict"] == "invalid":
            items.append({"verdict": "invalid", "status": None})
            continue
        rec = UpstreamRecord.from_json(norm)
        src = SourceRef(kind="file", snapshot=r["raw"] or {}, batch_id=b["id"], row_no=r["row_no"])
        try:
            with conn.transaction():
                oc = orders.ingest(conn, workspace_id, rec, src, actor_kind=ak, actor_id=aid)
        except DomainError as exc:
            issues = list(r["issues"] or []) + [f"提交失败：{exc.message}"]
            conn.execute("UPDATE procure.import_rows SET verdict = 'invalid', issues = %s, normalized = %s "
                         "WHERE batch_id = %s AND row_no = %s",
                         (issues, Jsonb({**norm, _PREVIEW_KEY: {**meta, "reason": f"提交失败：{exc.message}"}}),
                          b["id"], r["row_no"]))
            items.append({"verdict": "invalid", "status": None})
            continue
        conn.execute("UPDATE procure.import_rows SET verdict = %s, issues = %s, purchase_order_id = %s, normalized = %s "
                     "WHERE batch_id = %s AND row_no = %s",
                     (oc.verdict, list(oc.issues), oc.order_id,
                      Jsonb({**norm, _PREVIEW_KEY: {**meta, "reason": oc.reason, "status": oc.status}}), b["id"],
                      r["row_no"]))
        items.append({"verdict": oc.verdict, "status": oc.status})
    stats = _stats_of(items)
    now = clock.now()
    conn.execute("UPDATE procure.import_batches SET status = 'committed', committed_at = %s, committed_by = %s, "
                 "stats = %s WHERE id = %s AND workspace_id = %s",
                 (now, actor_user_id, Jsonb(stats), b["id"], workspace_id))
    events.record(conn, workspace_id, "batch", b["id"], "batch.committed", actor_kind=ak, actor_id=aid,
                  detail={"stats": stats})
    alloc = allocation.allocate(conn, workspace_id, trigger="import", actor_kind=ak, actor_id=aid)
    return {"batch": get_batch(conn, workspace_id, b["id"]), "stats": stats, "allocation": alloc}


def discard(conn, workspace_id: int, batch_id: int, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、批次、操作人 → 输出：批次形状（discarded；清掉原件副本与批次行）。已提交的批次 → VALIDATION。"""
    b = _batch(conn, workspace_id, batch_id, for_update=True)
    if b["status"] == "committed":
        raise validation("已提交的批次不能放弃")
    if b["status"] == "discarded":
        return batch_out(b)
    conn.execute("DELETE FROM procure.import_rows WHERE batch_id = %s", (b["id"],))
    conn.execute("UPDATE procure.import_batches SET status = 'discarded', file_bytes = ''::bytea WHERE id = %s "
                 "AND workspace_id = %s", (b["id"], workspace_id))
    ak, aid = _actor(actor_user_id)
    events.record(conn, workspace_id, "batch", b["id"], "batch.discarded", actor_kind=ak, actor_id=aid)
    return get_batch(conn, workspace_id, b["id"])


def list_batches(conn, workspace_id: int, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """输入：工作区、分页 → 输出：{total, rows: [批次]}（新的在前）。"""
    page, page_size = _page(page, page_size)
    total = conn.execute("SELECT count(*) AS n FROM procure.import_batches WHERE workspace_id = %s",
                         (workspace_id,)).fetchone()["n"]
    found = conn.execute(f"{_BATCH_SELECT} WHERE b.workspace_id = %s ORDER BY b.created_at DESC, b.id DESC "
                         "LIMIT %s OFFSET %s", (workspace_id, page_size, (page - 1) * page_size)).fetchall()
    return {"total": int(total), "rows": [batch_out(r) for r in found]}


# ── 问题行下载 ────────────────────────────────────────────────────────────────


def _unique_header(name: str, used: set[str]) -> str:
    cand, n = name, 2
    while cand in used:
        cand = f"{name}（导入{'' if n == 2 else n - 1}）"
        n += 1
    used.add(cand)
    return cand


def problems_xlsx(conn, workspace_id: int, batch_id: int) -> bytes:
    """输入：工作区、批次 → 输出：问题行 xlsx 字节：invalid / conflict / historical 或有问题的行，原列（原顺序）+ 结论 + 原因
    + 原行号。已放弃的批次 → VALIDATION。"""
    b = _batch(conn, workspace_id, batch_id)
    if b["status"] == "discarded":
        raise validation("已放弃的批次没有问题行可下载")
    found = conn.execute(
        "SELECT * FROM procure.import_rows WHERE batch_id = %s AND (verdict = ANY(%s) OR cardinality(issues) > 0) "
        "ORDER BY row_no",
        (b["id"], list(PROBLEM_VERDICTS)),
    ).fetchall()
    headers: list[str] = []
    try:
        data = conn.execute("SELECT file_bytes FROM procure.import_batches WHERE id = %s AND workspace_id = %s",
                            (b["id"], workspace_id)).fetchone()["file_bytes"]
        headers = list(_read_sheet(b, bytes(data), None).headers)
    except DomainError:
        for r in found:
            headers.extend(h for h in (r["raw"] or {}) if h not in headers)
    used = set(headers)
    h_verdict, h_reason, h_row = (_unique_header(n, used) for n in ("结论", "原因", "原行号"))
    out_rows = []
    for r in found:
        meta = (r["normalized"] or {}).get(_PREVIEW_KEY) or {}
        reasons = [x for x in [meta.get("reason"), *(r["issues"] or [])] if x]
        raw = r["raw"] or {}
        out_rows.append({**{h: raw.get(h) for h in headers}, h_verdict: VERDICT_ZH.get(r["verdict"], r["verdict"]),
                         h_reason: "；".join(dict.fromkeys(reasons)), h_row: r["row_no"]})
    return sheet_io.write_xlsx([("问题行", [*headers, h_verdict, h_reason, h_row], out_rows)])


# ── 个人模板 ──────────────────────────────────────────────────────────────────


def template_xlsx() -> bytes:
    """输入：无 → 输出：标准模板 xlsx 字节（见 file_mapping.template_xlsx）。"""
    return file_mapping.template_xlsx()


def _template_out(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "name": r["name"], "mapping": r["mapping"], "created_at": _iso(r["created_at"]),
            "created_by_name": r.get("created_by_name")}


def list_templates(conn, workspace_id: int) -> list[dict[str, Any]]:
    """输入：工作区 → 输出：[{id, name, mapping, created_at, created_by_name}]（按名称）。"""
    found = conn.execute(
        "SELECT t.*, COALESCE(u.display_name, u.username) AS created_by_name FROM procure.import_templates t "
        "LEFT JOIN procure.users u ON u.id = t.created_by WHERE t.workspace_id = %s ORDER BY t.name, t.id",
        (workspace_id,),
    ).fetchall()
    return [_template_out(r) for r in found]


def save_template(conn, workspace_id: int, name: str, mapping: dict[str, str | None],
                  actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、模板名、{表头: 系统字段 | None}、操作人 → 输出：模板。同名模板覆盖其对应关系。

    名称为空或超过 60 字、对应里有未知系统字段 / 同一字段对应多列 / 没对应平台订单号 → VALIDATION。
    """
    if not isinstance(name, str) or not name.strip():
        raise validation("请填写模板名称", field="name")
    name = name.strip()
    if len(name) > 60:
        raise validation("模板名称不能超过 60 个字", field="name")
    if not isinstance(mapping, dict):
        raise validation("字段对应格式不对", field="mapping")
    clean = {str(h): k for h, k in mapping.items() if isinstance(k, str) and k}
    file_mapping.validate_mapping(clean)
    now = clock.now()
    row = conn.execute(
        """
        INSERT INTO procure.import_templates (workspace_id, name, mapping, created_by, created_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (workspace_id, name) DO UPDATE SET mapping = EXCLUDED.mapping
        RETURNING id
        """,
        (workspace_id, name, Jsonb(clean), actor_user_id, now),
    ).fetchone()
    return next(t for t in list_templates(conn, workspace_id) if t["id"] == row["id"])


def delete_template(conn, workspace_id: int, template_id: int, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、模板 id、操作人 → 输出：{id}；不存在或属于别的工作区 → NOT_FOUND。"""
    try:
        tid = int(template_id)
    except (TypeError, ValueError):
        raise not_found("导入模板") from None
    row = conn.execute("DELETE FROM procure.import_templates WHERE id = %s AND workspace_id = %s RETURNING id",
                       (tid, workspace_id)).fetchone()
    if row is None:
        raise not_found("导入模板")
    return {"id": row["id"]}
