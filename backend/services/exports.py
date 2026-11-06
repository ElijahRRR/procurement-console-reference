"""结果导出（契约 §7「导出」、v1 §6.2）：按批次或按视图导出采购结果 xlsx。不要求连接飞书。

列：平台订单号、状态、买家号、AMZ订单号、税前总额、运费、税费、总额、卡尾号、建议采购日期、最近更新时间、异常原因，
另加「结果来源」（本系统 / 模拟 / 来源已有单号），避免把模拟单号误当真实采购。

- 结果取任务的成功尝试（purchased_attempt_id）；还没采购成功的任务只有状态与原因，买家号显示已分配的账号。
- 卡号只导出尾号（库里本来就只存后 4 位；这里再按数字截一次，绝不导出完整卡号）。
- 金额以数字写入（Excel 可求和）；时刻按北京时间写出。
- 按批次：该批次各行关联的任务（按行号，去重）；批次不存在或属于别的工作区 → NOT_FOUND；未提交 → VALIDATION。
- 按视图：与任务列表同一套视图（services/orders.list_orders），缺省全部。
"""

from __future__ import annotations

import re
from typing import Any

from services import orders, sheet_io
from services.errors import not_found, validation
from services.models import ORDER_STATUS_ZH

EXPORT_HEADERS = ["平台订单号", "状态", "买家号", "AMZ订单号", "税前总额", "运费", "税费", "总额", "卡尾号",
                  "建议采购日期", "最近更新时间", "异常原因", "结果来源"]
_PAGE = 200

_EXPORT_SQL = """
SELECT o.id, o.upstream_order_no, o.status, o.status_reason, o.needs_attention, o.attention_note,
       o.suggested_purchase_date, o.updated_at, o.external_amz_order_no,
       pa.id AS pa_id, pa.kind AS pa_kind, pa.simulated AS pa_simulated, pa.amazon_order_no, pa.total_before_tax,
       pa.shipping, pa.tax, pa.total, pa.card_last4, pa.buyer_account_name,
       pba.name AS purchase_account_name, aba.name AS assigned_account_name
FROM procure.purchase_orders o
LEFT JOIN procure.purchase_attempts pa ON pa.id = o.purchased_attempt_id AND pa.workspace_id = o.workspace_id
LEFT JOIN procure.buyer_accounts pba ON pba.id = pa.account_id AND pba.workspace_id = o.workspace_id
LEFT JOIN procure.buyer_accounts aba ON aba.id = o.assigned_account_id AND aba.workspace_id = o.workspace_id
WHERE o.workspace_id = %s AND o.id = ANY(%s)
"""


def _last4(card: Any) -> str | None:
    if card is None:
        return None
    digits = re.sub(r"\D", "", str(card))
    return digits[-4:] if len(digits) >= 4 else None


def _result_source(r: dict[str, Any]) -> str | None:
    if r["pa_id"] is None:
        return None
    if r["pa_kind"] == "external":
        return "来源已有单号"
    return "模拟" if r["pa_simulated"] else "本系统"


def _reason(r: dict[str, Any]) -> str | None:
    parts = [r["status_reason"]] if r["status_reason"] else []
    if r["needs_attention"] and r["attention_note"]:
        parts.append(r["attention_note"])
    return "；".join(parts) or None


def _row(r: dict[str, Any]) -> list[Any]:
    has_result = r["pa_id"] is not None
    buyer = (r["purchase_account_name"] or r["buyer_account_name"]) if has_result else r["assigned_account_name"]
    return [
        r["upstream_order_no"], ORDER_STATUS_ZH.get(r["status"], r["status"]), buyer,
        r["amazon_order_no"] if has_result else r["external_amz_order_no"],
        r["total_before_tax"] if has_result else None, r["shipping"] if has_result else None,
        r["tax"] if has_result else None, r["total"] if has_result else None,
        _last4(r["card_last4"]) if has_result else None, r["suggested_purchase_date"], r["updated_at"], _reason(r),
        _result_source(r),
    ]


def _batch_order_ids(conn, ws: int, batch_id: Any) -> list[int]:
    try:
        bid = int(batch_id)
    except (TypeError, ValueError):
        raise not_found("导入批次") from None
    b = conn.execute("SELECT id, status FROM procure.import_batches WHERE id = %s AND workspace_id = %s",
                     (bid, ws)).fetchone()
    if b is None:
        raise not_found("导入批次")
    if b["status"] != "committed":
        raise validation("批次尚未提交，没有可导出的结果")
    found = conn.execute(
        "SELECT r.purchase_order_id, min(r.row_no) AS first_row FROM procure.import_rows r "
        "JOIN procure.purchase_orders o ON o.id = r.purchase_order_id AND o.workspace_id = %s "
        "WHERE r.batch_id = %s AND r.workspace_id = %s AND r.purchase_order_id IS NOT NULL "
        "GROUP BY r.purchase_order_id ORDER BY first_row",
        (ws, bid, ws),
    ).fetchall()
    return [r["purchase_order_id"] for r in found]


def _view_order_ids(conn, ws: int, view: str | None) -> list[int]:
    ids: list[int] = []
    page = 1
    while True:
        res = orders.list_orders(conn, ws, view=view or "all", page=page, page_size=_PAGE)
        ids.extend(r["id"] for r in res["rows"])
        if len(res["rows"]) < _PAGE or len(ids) >= res["total"]:
            return ids
        page += 1


def order_rows(conn, workspace_id: int, *, batch_id: int | None = None, view: str | None = None) -> list[list[Any]]:
    """输入：工作区、批次 id 或视图 → 输出：导出行（与 EXPORT_HEADERS 同序的值列表）。"""
    ids = (_batch_order_ids(conn, workspace_id, batch_id) if batch_id is not None
           else _view_order_ids(conn, workspace_id, view))
    if not ids:
        return []
    by_id = {r["id"]: r for r in conn.execute(_EXPORT_SQL, (workspace_id, ids)).fetchall()}
    return [_row(by_id[i]) for i in ids if i in by_id]


def orders_xlsx(conn, workspace_id: int, *, batch_id: int | None = None, view: str | None = None) -> bytes:
    """输入：工作区、批次 id（优先）或视图（契约 §8 的视图名，缺省全部）→ 输出：采购结果 xlsx 字节（工作表「采购结果」）。"""
    return sheet_io.write_xlsx([("采购结果", EXPORT_HEADERS, order_rows(conn, workspace_id, batch_id=batch_id,
                                                                       view=view))],
                               text_headers={"平台订单号", "AMZ订单号", "卡尾号"})
