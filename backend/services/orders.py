"""采购任务：有效值、状态派生与迁移、来源接收（ingest）、列表与详情、全部人工动作（契约 §2、§8、§10）。

要点：

- **有效值**：控制台覆盖值优先，否则来源原值（ASIN、数量、限价）；商品小计 = 采购单价 × 有效数量。
- **状态只经 transition() 改**：它校验 ALLOWED_TRANSITIONS、做乐观并发检查（状态已被别人改 → CONFLICT）、
  写 'order.status_changed' 事件；进入不需要账号的状态时自动解除分配。唯一例外是 revert_simulation()
  （契约 §5 清除模拟结果：把模拟采购过的任务直接退回开始前的派生状态）。
- **ingest 的增量模型**：同一来源记录再次到来时，只把「这条来源相对上次的变化」应用到任务上
  （上次的规范化记录存在 order_sources.normalized）；来源没变（指纹与字段都相同）就是 unchanged，
  不会把控制台已处理过的冲突、覆盖值、人工人审结论再改回去。第一次见到的来源记录（如新批次文件行）
  与任务现值比较，None 表示「没有或没读到」，不清空已有值；收件信息作为一组整体替换。
- 来源更新按任务状态分别处理（开始前改原值并重新派生；异常时改原值但保持异常；开始后只记事件并置
  needs_attention），上游取消、外部 AMZ 订单号、跨来源冲突各有专门规则，见 ingest() 说明。
- 人工动作都写带操作者的事件；本模块的动作**不自动触发重新分配**（契约 §3.2 的触发点只有来源导入/同步后、
  人工点「重新分配」、账号配置变化、每日 00:01），回到「待分配」的任务等待下一次分配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg.types.json import Jsonb

from services import clock, events, writeback
from services.errors import DomainError, conflict, not_found, validation
from services.models import (
    ATTEMPT_INFLIGHT,
    EXECUTION_FIELDS,
    ORDER_STATUS_ZH,
    ORDER_STATUSES,
    PRE_EXECUTION_STATUSES,
    REQUIRED_FOR_EXECUTION,
    SourceRef,
    UpstreamRecord,
    money,
)

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


class _Unset:
    """「没有传这个参数」的标记（与显式 None = 清除 区分）。"""

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _Unset()

# ── 状态集合与迁移表 ──────────────────────────────────────────────────────────

PRE = PRE_EXECUTION_STATUSES
#: derive_status() 可能给出的状态（开始前的状态 + 外部单号的 purchased + 上游取消的 cancelled）
DERIVED_STATUSES = frozenset(PRE | {"purchased", "cancelled"})
#: 采购已开始（或已结束）：来源更新不再改执行相关原值
STARTED_STATUSES = frozenset({"executing", "result_unknown", "purchased", "repurchase_pending", "closed"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    **{s: frozenset((DERIVED_STATUSES - {s}) | ({"executing"} if s == "assigned" else set())) for s in PRE},
    "executing": frozenset({"assigned", "exception", "result_unknown", "purchased", "cancelled"}),
    "result_unknown": frozenset({"purchased", "assigned", "queued"}),
    "exception": frozenset(DERIVED_STATUSES | {"closed"}),
    "purchased": frozenset({"repurchase_pending"}),
    "repurchase_pending": frozenset(DERIVED_STATUSES | {"closed"}),
    "cancelled": frozenset(DERIVED_STATUSES - {"cancelled"}),
    "closed": frozenset(),
}

#: 进入这些状态时自动解除分配
_CLEAR_ASSIGNMENT_ON = frozenset({"pending_review", "needs_info", "conflict", "queued", "cancelled", "closed"})
_CLEAR_ASSIGNMENT = {"assigned_account_id": None, "assignment_kind": None, "assigned_at": None, "assignment_note": None}

#: transition() / _update() 允许写的列（status、status_changed_at、updated_at 由本模块管理）
ORDER_COLUMNS = frozenset({
    "upstream_order_no", "order_line_id", "store_name", "sales_po_no", "sku", "asin", "quantity",
    "unit_price", "sales_total", "price_cap", "order_time", "order_date_bj", "ship_days",
    "suggested_purchase_date", "suggested_date_basis", "ship_method", "recipient_name", "address1",
    "address2", "city", "state", "postal_code", "phone", "country", "district", "review_status",
    "review_source", "review_value", "reviewed_by", "reviewed_at", "upstream_cancelled",
    "upstream_cancel_note", "upstream_cancelled_at", "external_amz_order_no", "override_asin",
    "override_quantity", "override_price_cap", "override_note", "override_by", "override_at",
    "status_reason", "missing_fields", "conflict", "needs_attention", "attention_note",
    "primary_source_kind", "assigned_account_id", "assignment_kind", "assigned_at", "assignment_note",
    "page_load_retries", "current_attempt_id", "purchased_attempt_id", "result_version",
})

# ── 来源字段分组 ──────────────────────────────────────────────────────────────

#: 来源原值中按普通字段对待的列（人审、外部单号、取消、建议采购日期另有规则）
PLAIN_COLUMNS = (
    "order_line_id", "store_name", "sales_po_no", "sku", "asin", "quantity", "unit_price", "sales_total",
    "price_cap", "order_time", "ship_days", "ship_method", "recipient_name", "address1", "address2",
    "city", "state", "postal_code", "phone", "country", "district",
)
ADDRESS_GROUP = ("recipient_name", "address1", "address2", "city", "state", "postal_code", "phone",
                 "country", "district")
ADDRESS_CORE = ("recipient_name", "address1", "city", "state", "postal_code")
#: 采购开始后仍可随来源更新的纯展示字段
DISPLAY_COLUMNS = frozenset({"order_line_id", "store_name", "sales_po_no", "sku", "sales_total", "ship_method"})
_MONEY_COLUMNS = frozenset({"unit_price", "sales_total", "price_cap", "override_price_cap"})
_TEXT_COLUMNS = frozenset({"order_line_id", "store_name", "sales_po_no", "sku", "ship_method", "recipient_name",
                           "address1", "address2", "city", "state", "postal_code", "phone", "country", "district"})

FIELD_LABELS: dict[str, str] = {
    **{k: v for k, v in REQUIRED_FOR_EXECUTION},
    "order_line_id": "订单行 ID", "store_name": "店铺", "sales_po_no": "采购订单号", "sku": "SKU",
    "sales_total": "销售总额", "order_time": "下单时间", "ship_days": "配送天数", "ship_method": "配送方式",
    "address2": "地址2", "country": "国家", "district": "收货人区", "review_status": "人审结论",
    "review_value": "人工审核", "external_amz_order_no": "AMZ订单号", "upstream_cancelled": "上游取消",
}

_OVERRIDE_COLS = ("override_asin", "override_quantity", "override_price_cap")
_INFLIGHT = sorted(ATTEMPT_INFLIGHT)

INGEST_VERDICTS = ("new", "update", "unchanged", "conflict", "historical", "invalid")


@dataclass
class IngestOutcome:
    """ingest 的结论。verdict：new / update / unchanged / conflict / historical / invalid。

    changes：{字段: [任务现值, 来源新值]}（含因状态未应用的差异）；issues：规范化问题 + 状态提示；
    reason：结论说明；status：处理后（dry_run 时为预计）的任务状态。
    """

    verdict: str
    order_id: int | None = None
    changes: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    reason: str | None = None
    status: str | None = None


# ── 有效值与派生 ──────────────────────────────────────────────────────────────


def asin_eff(row: dict[str, Any]) -> str | None:
    """输入：任务行 → 输出：有效 ASIN（覆盖值优先）。"""
    return row.get("override_asin") or row.get("asin")


def quantity_eff(row: dict[str, Any]) -> int | None:
    """输入：任务行 → 输出：有效数量（覆盖值优先）。"""
    oq = row.get("override_quantity")
    return oq if oq is not None else row.get("quantity")


def price_cap_eff(row: dict[str, Any]) -> Decimal | None:
    """输入：任务行 → 输出：有效限价（覆盖值优先）。"""
    op = row.get("override_price_cap")
    return op if op is not None else row.get("price_cap")


def subtotal_eff(row: dict[str, Any]) -> Decimal | None:
    """输入：任务行 → 输出：商品小计 = 采购单价 × 有效数量（任一缺失 → None，不当 0）。"""
    up = row.get("unit_price")
    q = quantity_eff(row)
    if up is None or q is None:
        return None
    return (Decimal(up) * int(q)).quantize(Decimal("0.01"))


def has_override(row: dict[str, Any]) -> bool:
    """输入：任务行 → 输出：是否有任一控制台覆盖值。"""
    return any(row.get(k) is not None for k in _OVERRIDE_COLS)


def amount_in_range(subtotal: Decimal | None, account_row: dict[str, Any]) -> bool:
    """输入：商品小计、账号行 → 输出：是否在账号金额区间内（端点含；max 为空 = 不限；小计未知 → False）。"""
    if subtotal is None:
        return False
    if subtotal < account_row["min_amount"]:
        return False
    mx = account_row.get("max_amount")
    return mx is None or subtotal <= mx


def compute_suggested_date(order_time: datetime | None, ship_days: int | None) -> date | None:
    """输入：下单时刻、配送天数 → 输出：下单北京日期 + 6 − 配送天数；任一缺失 → None。"""
    if order_time is None or ship_days is None:
        return None
    if order_time.tzinfo is None:
        order_time = order_time.replace(tzinfo=clock.BJ_TZ)
    return clock.bj_date(order_time) + timedelta(days=6 - int(ship_days))


def compute_missing_fields(row: dict[str, Any]) -> list[str]:
    """输入：任务行（或同形状 dict）→ 输出：缺失的执行所需字段中文名列表（models.REQUIRED_FOR_EXECUTION 顺序）。"""
    missing: list[str] = []
    for key, label in REQUIRED_FOR_EXECUTION:
        if key == "asin":
            v = asin_eff(row)
            ok = isinstance(v, str) and bool(ASIN_RE.match(v))
        elif key == "quantity":
            v = quantity_eff(row)
            ok = isinstance(v, int) and not isinstance(v, bool) and v > 0
        elif key == "unit_price":
            v = row.get("unit_price")
            ok = v is not None and Decimal(v) > 0
        elif key == "price_cap":
            v = price_cap_eff(row)
            ok = v is not None and Decimal(v) > 0
        elif key == "suggested_purchase_date":
            ok = row.get("suggested_purchase_date") is not None
        else:
            v = row.get(key)
            ok = isinstance(v, str) and bool(v.strip())
        if not ok:
            missing.append(label)
    return missing


def derive_status(row: dict[str, Any]) -> str:
    """输入：任务行 → 输出：采购开始前的派生状态。

    优先级：上游取消 → cancelled；外部单号 → purchased；未通过人审 → pending_review；冲突未解决 → conflict；
    缺字段 → needs_info；已分配 → assigned；否则 queued。
    """
    if row.get("upstream_cancelled"):
        return "cancelled"
    if row.get("external_amz_order_no"):
        return "purchased"
    if row.get("review_status") != "approved":
        return "pending_review"
    if row.get("conflict"):
        return "conflict"
    if compute_missing_fields(row):
        return "needs_info"
    if row.get("assigned_account_id") is not None:
        return "assigned"
    return "queued"


def derived_reason(row: dict[str, Any], status: str) -> str | None:
    """输入：任务行、派生状态 → 输出：status_reason 文案（queued / assigned 为 None，由分配写原因）。"""
    if status == "pending_review":
        if row.get("review_status") == "rejected":
            return f"人审未通过（{row.get('review_value') or '拒绝'}）"
        if row.get("review_value"):
            return f"人审结论为「{row['review_value']}」，等待人审通过"
        return "缺少人审通过结论，等待人审"
    if status == "needs_info":
        missing = compute_missing_fields(row)
        return "缺少：" + "、".join(missing) if missing else None
    if status == "conflict":
        c = row.get("conflict") or {}
        if c.get("reason"):
            return c["reason"]
        fields = list((c.get("fields") or {}).keys())
        return "来源内容冲突：" + "、".join(FIELD_LABELS.get(f, f) for f in fields) if fields else "来源内容冲突"
    if status == "cancelled":
        note = row.get("upstream_cancel_note")
        return f"上游取消（{note}）" if note else "上游取消"
    if status == "purchased":
        no = row.get("external_amz_order_no")
        return f"来源已有 AMZ 订单号 {no}" if no else None
    return None


# ── 读取与写入基础 ────────────────────────────────────────────────────────────


def get_row(conn, workspace_id: int, order_id: int, for_update: bool = False) -> dict[str, Any]:
    """输入：工作区、任务 id、是否加行锁 → 输出：purchase_orders 行；不存在或属于别的工作区 → NOT_FOUND。"""
    row = conn.execute(
        "SELECT * FROM procure.purchase_orders WHERE id = %s AND workspace_id = %s"
        + (" FOR UPDATE" if for_update else ""),
        (order_id, workspace_id),
    ).fetchone()
    if row is None:
        raise not_found("采购任务")
    return row


def _set_clause(upd: dict[str, Any]) -> tuple[str, list[Any]]:
    bad = set(upd) - ORDER_COLUMNS
    if bad:
        raise ValueError(f"purchase_orders 不允许写的列：{sorted(bad)}")
    parts, params = [], []
    for k, v in upd.items():
        parts.append(f"{k} = %s")
        params.append(Jsonb(_j(v)) if isinstance(v, dict) else v)
    return ", ".join(parts), params


def _update(conn, workspace_id: int, order_id: int, upd: dict[str, Any]) -> dict[str, Any]:
    """输入：工作区、任务 id、列 → 值（不含状态）→ 输出：更新后的行。"""
    if not upd:
        return get_row(conn, workspace_id, order_id)
    sets, params = _set_clause(upd)
    row = conn.execute(
        f"UPDATE procure.purchase_orders SET {sets}, updated_at = %s WHERE id = %s AND workspace_id = %s RETURNING *",
        [*params, clock.now(), order_id, workspace_id],
    ).fetchone()
    if row is None:
        raise not_found("采购任务")
    return row


def _clears_assignment(frm: str, to: str) -> bool:
    if to in _CLEAR_ASSIGNMENT_ON:
        return True
    # 外部单号直接成「已采购」：分配的账号并没有买它
    return to == "purchased" and frm not in ("executing", "result_unknown")


def transition(conn, order_row: dict[str, Any], to_status: str, *, reason: str | None = None,
               actor_kind: str, actor_id: int | None = None, updates: dict[str, Any] | None = None,
               event_detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """输入：任务行、目标状态、原因、操作者、同时要写的列 → 输出：更新后的任务行。

    - 目标状态必须在 ALLOWED_TRANSITIONS[当前状态] 里，否则 INVALID_TRANSITION（409）；
    - 目标与当前相同：只写 updates（reason 非空时也写 status_reason），不记状态事件；
    - 进入 pending_review / needs_info / conflict / queued / cancelled / closed（以及外部单号的 purchased）
      时自动解除分配；进入 assigned 时必须有账号；
    - 以「当前状态仍是 order_row 里的状态」为条件更新，被并发改过 → CONFLICT（409）；
    - 写 'order.status_changed' 事件 {from, to, reason, ...event_detail}。
    """
    frm = order_row["status"]
    if to_status not in ORDER_STATUSES:
        raise ValueError(f"未知任务状态 {to_status}")
    upd = dict(updates or {})
    ws, oid = order_row["workspace_id"], order_row["id"]
    if to_status == frm:
        if reason is not None:
            upd["status_reason"] = reason
        return _update(conn, ws, oid, upd) if upd else order_row
    if to_status not in ALLOWED_TRANSITIONS.get(frm, frozenset()):
        raise DomainError(
            "INVALID_TRANSITION",
            f"任务不能从「{ORDER_STATUS_ZH.get(frm, frm)}」变为「{ORDER_STATUS_ZH.get(to_status, to_status)}」",
            409, {"from": frm, "to": to_status},
        )
    if _clears_assignment(frm, to_status) and "assigned_account_id" not in upd:
        upd.update(_CLEAR_ASSIGNMENT)
    final_account = upd["assigned_account_id"] if "assigned_account_id" in upd else order_row.get("assigned_account_id")
    if to_status == "assigned" and final_account is None:
        raise DomainError("INVALID_TRANSITION", "「已分配」需要指定买家账号", 409, {"from": frm, "to": to_status})
    upd["status_reason"] = reason
    sets, params = _set_clause(upd)
    now = clock.now()
    row = conn.execute(
        f"UPDATE procure.purchase_orders SET status = %s, status_changed_at = %s, updated_at = %s, {sets} "
        "WHERE id = %s AND workspace_id = %s AND status = %s RETURNING *",
        [to_status, now, now, *params, oid, ws, frm],
    ).fetchone()
    if row is None:
        current = conn.execute("SELECT status FROM procure.purchase_orders WHERE id = %s AND workspace_id = %s",
                               (oid, ws)).fetchone()
        if current is None:
            raise not_found("采购任务")
        raise conflict("任务状态已被其他操作改变，请刷新后重试", expected=frm, current=current["status"])
    events.record(conn, ws, "order", oid, "order.status_changed", actor_kind=actor_kind, actor_id=actor_id,
                  detail={"from": frm, "to": to_status, "reason": reason, **(event_detail or {})})
    return row


def _account_row(conn, workspace_id: int, account_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM procure.buyer_accounts WHERE id = %s AND workspace_id = %s",
                       (account_id, workspace_id)).fetchone()
    if row is None:
        raise not_found("买家账号")
    return row


def _assignment_problem(conn, workspace_id: int, row: dict[str, Any]) -> str | None:
    """输入：带分配的任务行 → 输出：分配已失效的原因（账号已归档 / 金额不在区间）；仍有效 → None。

    关闭采购开关或暂停执行的账号，已分配任务保留（契约 §3.2）。
    """
    acc = conn.execute("SELECT * FROM procure.buyer_accounts WHERE id = %s AND workspace_id = %s",
                       (row["assigned_account_id"], workspace_id)).fetchone()
    if acc is None or acc["archived_at"] is not None:
        return "原分配账号已归档，已解除分配"
    sub = subtotal_eff(row)
    if not amount_in_range(sub, acc):
        return f"金额 {_usd(sub)} 不在账号 {acc['name']} 的区间内，已解除分配"
    return None


def _rederive(conn, workspace_id: int, order: dict[str, Any], upd: dict[str, Any], *, actor_kind: str,
              actor_id: int | None, event_detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """输入：任务行、要同时写的列 → 输出：按派生规则重新校验后的任务行（必要时迁移状态、解除失效分配）。"""
    upd = dict(upd)
    merged = {**order, **upd}
    problem = None
    target = derive_status(merged)
    if target == "assigned":
        problem = _assignment_problem(conn, workspace_id, merged)
        if problem:
            upd.update(_CLEAR_ASSIGNMENT)
            merged.update(_CLEAR_ASSIGNMENT)
            target = "queued"
    missing = compute_missing_fields(merged)
    upd["missing_fields"] = missing
    merged["missing_fields"] = missing
    reason = problem or derived_reason(merged, target)
    if target == order["status"]:
        if target != "queued" or problem:
            upd["status_reason"] = reason
        return _update(conn, workspace_id, order["id"], upd)
    return transition(conn, order, target, reason=reason, actor_kind=actor_kind, actor_id=actor_id,
                      updates=upd, event_detail=event_detail)


def next_attempt_no(conn, order_id: int, *, workspace_id: int | None = None) -> int:
    """输入：任务 id → 输出：下一次尝试的序号（从 1 开始）。"""
    sql = "SELECT COALESCE(max(attempt_no), 0) + 1 AS n FROM procure.purchase_attempts WHERE purchase_order_id = %s"
    params: list[Any] = [order_id]
    if workspace_id is not None:
        sql += " AND workspace_id = %s"
        params.append(workspace_id)
    return int(conn.execute(sql, params).fetchone()["n"])


def next_attempt_kind(conn, order_row: dict[str, Any]) -> str:
    """输入：任务行 → 输出：下一次尝试的种类：曾成功过（含外部单号）→ 'repurchase'；
    最近一次本系统尝试失败或中止 → 'retry'；否则 'auto'。"""
    rows = conn.execute(
        "SELECT kind, status FROM procure.purchase_attempts WHERE purchase_order_id = %s AND workspace_id = %s "
        "ORDER BY attempt_no DESC",
        (order_row["id"], order_row["workspace_id"]),
    ).fetchall()
    if any(r["status"] == "succeeded" for r in rows):
        return "repurchase"
    own = [r for r in rows if r["kind"] != "external"]
    if own and own[0]["status"] in ("failed", "aborted"):
        return "retry"
    return "auto"


def inflight_attempt(conn, order_row: dict[str, Any]) -> dict[str, Any] | None:
    """输入：任务行 → 输出：它的在途尝试（claimed…verifying），没有 → None。"""
    return conn.execute(
        "SELECT * FROM procure.purchase_attempts WHERE purchase_order_id = %s AND workspace_id = %s "
        "AND status = ANY(%s) ORDER BY id DESC LIMIT 1",
        (order_row["id"], order_row["workspace_id"], _INFLIGHT),
    ).fetchone()


def _amazon_no_owner(conn, workspace_id: int, amazon_order_no: str, exclude_order_id: int | None) -> dict | None:
    """输入：亚马逊订单号 → 输出：已把它登记为真实成功结果的另一笔任务 {id, upstream_order_no}，没有 → None。"""
    return conn.execute(
        """
        SELECT o.id, o.upstream_order_no FROM procure.purchase_attempts a
        JOIN procure.purchase_orders o ON o.id = a.purchase_order_id AND o.workspace_id = a.workspace_id
        WHERE a.workspace_id = %s AND a.amazon_order_no = %s AND NOT a.simulated AND a.status = 'succeeded'
          AND a.purchase_order_id <> COALESCE(%s, -1)
        LIMIT 1
        """,
        (workspace_id, amazon_order_no, exclude_order_id),
    ).fetchone()


def _known_amazon_nos(conn, order: dict[str, Any]) -> set[str]:
    rows = conn.execute(
        "SELECT amazon_order_no FROM procure.purchase_attempts WHERE purchase_order_id = %s AND workspace_id = %s "
        "AND amazon_order_no IS NOT NULL",
        (order["id"], order["workspace_id"]),
    ).fetchall()
    nos = {r["amazon_order_no"] for r in rows}
    if order.get("external_amz_order_no"):
        nos.add(order["external_amz_order_no"])
    return nos


def _insert_external_attempt(conn, workspace_id: int, order_id: int, amazon_order_no: str,
                             buyer_account_name: str | None, source_desc: dict[str, Any]) -> int:
    """输入：任务、外部 AMZ 订单号、来源中的买家号 → 输出：新建的 kind='external' 成功尝试 id（不计额度）。"""
    account_id = None
    if buyer_account_name:
        acc = conn.execute(
            "SELECT id FROM procure.buyer_accounts WHERE workspace_id = %s AND name = %s",
            (workspace_id, buyer_account_name),
        ).fetchone()
        account_id = acc["id"] if acc else None
    now = clock.now()
    row = conn.execute(
        """
        INSERT INTO procure.purchase_attempts
            (workspace_id, purchase_order_id, attempt_no, kind, account_id, simulated, status, bj_date,
             claimed_at, finished_at, amazon_order_no, buyer_account_name, task_snapshot)
        VALUES (%s, %s, %s, 'external', %s, false, 'succeeded', %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (workspace_id, order_id, next_attempt_no(conn, order_id, workspace_id=workspace_id), account_id,
         clock.bj_today(), now, now, amazon_order_no, buyer_account_name, Jsonb(_j({"source": source_desc}))),
    ).fetchone()
    return row["id"]


# ── 来源接收 ──────────────────────────────────────────────────────────────────


def _clean_text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _record_values(rec: UpstreamRecord) -> dict[str, Any]:
    """输入：上游记录 → 输出：它对应的任务原值列（含下单北京日期、建议采购日期及依据、人审、取消、外部单号）。"""
    vals: dict[str, Any] = {}
    for f in PLAIN_COLUMNS:
        v = getattr(rec, f)
        if f in _TEXT_COLUMNS:
            v = _clean_text(v)
        vals[f] = v
    asin = _clean_text(rec.asin)
    asin = asin.upper() if asin else None
    vals["asin"] = asin if asin and ASIN_RE.match(asin) else None
    q = rec.quantity
    vals["quantity"] = q if isinstance(q, int) and not isinstance(q, bool) and q > 0 else None
    for f in ("unit_price", "sales_total", "price_cap"):
        vals[f] = money(getattr(rec, f))
    sd = rec.ship_days
    vals["ship_days"] = sd if isinstance(sd, int) and not isinstance(sd, bool) else None
    ot = rec.order_time
    if ot is not None and ot.tzinfo is None:
        ot = ot.replace(tzinfo=clock.BJ_TZ)
    vals["order_time"] = ot
    vals["country"] = (vals["country"] or "US").upper()
    vals["order_date_bj"] = clock.bj_date(ot) if ot is not None else None
    if rec.suggested_purchase_date is not None:
        vals["suggested_purchase_date"], vals["suggested_date_basis"] = rec.suggested_purchase_date, "source"
    else:
        d = compute_suggested_date(ot, vals["ship_days"])
        vals["suggested_purchase_date"], vals["suggested_date_basis"] = d, ("computed" if d else None)
    vals["review_value"] = _clean_text(rec.review_value)
    vals["review_status"] = rec.review if rec.review in ("approved", "rejected", "unknown") else "unknown"
    vals["external_amz_order_no"] = _clean_text(rec.external_amz_order_no)
    vals["upstream_cancelled"] = bool(rec.upstream_cancelled)
    return vals


def _norm_text(s: str) -> str:
    return " ".join(s.split()).casefold()


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) and isinstance(b, str):
        return _norm_text(a) == _norm_text(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (Decimal, int, float)) and isinstance(b, (Decimal, int, float)):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except InvalidOperation:
            return False
    return a == b


def _source_desc(source: SourceRef) -> dict[str, Any]:
    if source.kind == "feishu":
        return {"kind": "feishu", "connection_id": source.connection_id, "record_id": source.record_id}
    return {"kind": "file", "batch_id": source.batch_id, "row_no": source.row_no}


def _find_link(conn, workspace_id: int, source: SourceRef) -> dict[str, Any] | None:
    if source.kind == "feishu":
        return conn.execute(
            "SELECT * FROM procure.order_sources WHERE workspace_id = %s AND kind = 'feishu' "
            "AND feishu_connection_id = %s AND feishu_record_id = %s",
            (workspace_id, source.connection_id, source.record_id),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM procure.order_sources WHERE workspace_id = %s AND kind = 'file' "
        "AND import_batch_id = %s AND import_row_no = %s",
        (workspace_id, source.batch_id, source.row_no),
    ).fetchone()


def _upsert_link(conn, workspace_id: int, order_id: int, link: dict[str, Any] | None,
                 record: UpstreamRecord, source: SourceRef) -> None:
    now = clock.now()
    snapshot = Jsonb(_j(source.snapshot or {}))
    normalized = Jsonb(record.to_json())
    fp = record.execution_fingerprint()
    if link is not None:
        conn.execute(
            "UPDATE procure.order_sources SET snapshot = %s, normalized = %s, fingerprint = %s, last_seen_at = %s "
            "WHERE id = %s AND workspace_id = %s",
            (snapshot, normalized, fp, now, link["id"], workspace_id),
        )
        return
    has_target = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM procure.order_sources WHERE purchase_order_id = %s AND is_writeback_target) AS t",
        (order_id,),
    ).fetchone()["t"]
    conn.execute(
        """
        INSERT INTO procure.order_sources
            (workspace_id, purchase_order_id, kind, feishu_connection_id, feishu_record_id, import_batch_id,
             import_row_no, snapshot, normalized, fingerprint, is_writeback_target, first_seen_at, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (workspace_id, order_id, source.kind,
         source.connection_id if source.kind == "feishu" else None,
         source.record_id if source.kind == "feishu" else None,
         source.batch_id if source.kind == "file" else None,
         source.row_no if source.kind == "file" else None,
         snapshot, normalized, fp, source.kind == "feishu" and not has_target, now, now),
    )


def _status_issues(row: dict[str, Any], status: str) -> list[str]:
    if status == "pending_review":
        return [f"{derived_reason(row, status)}，停在待确认"]
    if status == "needs_info":
        return [f"缺少执行所需字段：{'、'.join(compute_missing_fields(row))}，停在资料待补齐"]
    return []


def _append_note(existing: str | None, note: str) -> str:
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing}；{note}"[:1000]


def ingest(conn, workspace_id: int, record: UpstreamRecord, source: SourceRef, *, actor_kind: str = "source",
           actor_id: int | None = None, dry_run: bool = False) -> IngestOutcome:
    """输入：工作区、规范化后的上游记录、来源引用、操作者、是否只预演 → 输出：IngestOutcome。

    规则（契约 §2「来源更新」、§7 预览结论）：

    - 无平台订单号 → invalid。按（工作区, 平台订单号）找任务；同一来源记录的平台订单号变了 → conflict
      （记在原任务上并置 needs_attention，不新建）。
    - 新任务：首次来源种类为主来源；第一个飞书来源为写回目标；建议采购日期取来源值（依据 source），否则
      下单北京日期 + 6 − 配送天数（依据 computed）；带 AMZ 订单号 → historical，直接「已采购」并建
      kind='external' 的成功尝试（单号已登记在别的任务 → conflict，不建尝试）。
    - 已有任务（只应用来源相对上次的变化；见模块说明）：
      * 同种来源（与主来源相同）：开始前改原值并重新派生（覆盖值不动；人审撤回 → 待确认并解除分配；
        金额已不在账号区间 → 解除分配回待分配）；异常时改原值但保持异常；采购开始后只改展示字段，
        执行相关变化记事件并置 needs_attention（结论 conflict）；已取消的只改原值。
      * 另一种来源：执行相关字段（ASIN、数量、单价、限价、收件信息、建议采购日期）不同 → 开始前转 conflict
        （不覆盖，来源新值存进 conflict.incoming 供「接受」）；相同只追加来源关联。
      * 上游取消：开始前 / 异常 → cancelled 并解除分配；执行中未登记提交 → 置 upstream_cancelled（插件下次
        进度上报得到 continue=false）；执行中已登记提交 → result_unknown（「执行中收到取消」）；
        待核实 / 已采购 / 待决定重采 / 已关闭 → 只置 upstream_cancelled 与 needs_attention。
      * 外部 AMZ 订单号：开始前 / 异常 → purchased（建 external 尝试）；单号已属别的任务、或任务正在执行 /
        已有不同单号 → conflict（开始前转 conflict 状态，其余置 needs_attention）。
    - 每次都 upsert order_sources（快照、规范化记录、指纹、last_seen_at）。dry_run=True 时给出同样的结论但不写库。
    """
    order_no = (record.upstream_order_no or "").strip()
    issues = list(record.issues or [])
    if not order_no:
        return IngestOutcome("invalid", issues=issues, reason="缺少平台订单号")
    if order_no != record.upstream_order_no:
        record = UpstreamRecord.from_json({**record.to_json(), "upstream_order_no": order_no})
    link = _find_link(conn, workspace_id, source)
    order = conn.execute(
        "SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = %s"
        + ("" if dry_run else " FOR UPDATE"),
        (workspace_id, order_no),
    ).fetchone()
    if link is not None and (order is None or link["purchase_order_id"] != order["id"]):
        return _ingest_key_changed(conn, workspace_id, link, order_no, source, issues, actor_kind, actor_id, dry_run)
    if order is None:
        return _ingest_new(conn, workspace_id, record, source, issues, actor_kind, actor_id, dry_run)
    return _ingest_existing(conn, workspace_id, order, link, record, source, issues, actor_kind, actor_id, dry_run)


def _ingest_key_changed(conn, ws, link, order_no, source, issues, actor_kind, actor_id, dry_run) -> IngestOutcome:
    old = conn.execute("SELECT * FROM procure.purchase_orders WHERE id = %s AND workspace_id = %s",
                       (link["purchase_order_id"], ws)).fetchone()
    reason = f"同一来源记录的平台订单号由 {old['upstream_order_no']} 变为 {order_no}，需人工核实"
    if not dry_run and reason not in (old["attention_note"] or ""):   # 定时同步反复送来时只提醒一次
        note = _append_note(old["attention_note"], reason)
        _update(conn, ws, old["id"], {"needs_attention": True, "attention_note": note})
        events.record(conn, ws, "order", old["id"], "order.source_key_changed", actor_kind=actor_kind,
                      actor_id=actor_id, detail={"source": _source_desc(source), "old": old["upstream_order_no"],
                                                 "new": order_no})
    return IngestOutcome("conflict", old["id"], {"upstream_order_no": [old["upstream_order_no"], order_no]},
                         issues, reason, old["status"])


def _ingest_new(conn, ws, record, source, issues, actor_kind, actor_id, dry_run) -> IngestOutcome:
    vals = _record_values(record)
    ext_no = vals.pop("external_amz_order_no")
    now = clock.now()
    row: dict[str, Any] = {
        **vals,
        "workspace_id": ws, "upstream_order_no": record.upstream_order_no,
        "primary_source_kind": source.kind,
        "review_source": source.kind if (vals["review_value"] or vals["review_status"] != "unknown") else None,
        "reviewed_at": now if vals["review_status"] != "unknown" else None,
        "upstream_cancel_note": (record.after_sale_value or "上游取消") if vals["upstream_cancelled"] else None,
        "upstream_cancelled_at": now if vals["upstream_cancelled"] else None,
        "external_amz_order_no": None, "conflict": None, "assigned_account_id": None,
        "override_asin": None, "override_quantity": None, "override_price_cap": None,
    }
    verdict, reason = "new", None
    if ext_no:
        dup = _amazon_no_owner(conn, ws, ext_no, None)
        if dup:
            verdict = "conflict"
            reason = f"AMZ 订单号 {ext_no} 已登记在任务 {dup['upstream_order_no']}，需人工核实"
            row["conflict"] = {
                "kind": "external_order_no", "source_kind": source.kind, "reason": reason,
                "fields": {"external_amz_order_no": [None, ext_no]},
                "incoming": {"external_amz_order_no": ext_no,
                             "external_buyer_account": _clean_text(record.external_buyer_account)},
                "source": _source_desc(source), "at": now,
            }
        else:
            verdict = "historical"
            row["external_amz_order_no"] = ext_no
            reason = f"来源已有 AMZ 订单号 {ext_no}，按历史采购登记，不重新下单"
    row["missing_fields"] = compute_missing_fields(row)
    status = derive_status(row)
    issues = issues + _status_issues(row, status)
    if dry_run:
        return IngestOutcome(verdict, None, {}, issues, reason, status)

    cols = {k: v for k, v in row.items() if k in ORDER_COLUMNS or k in ("workspace_id", "upstream_order_no")}
    cols.update(status=status, status_reason=derived_reason(row, status), status_changed_at=now,
                created_at=now, updated_at=now)
    names = list(cols)
    params = [Jsonb(_j(v)) if isinstance(v, dict) else v for v in cols.values()]
    inserted = conn.execute(
        f"INSERT INTO procure.purchase_orders ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) "
        "ON CONFLICT (workspace_id, upstream_order_no) DO NOTHING RETURNING *",
        params,
    ).fetchone()
    if inserted is None:
        # 并发的另一笔接收刚建好同键任务：按已有任务处理
        order = conn.execute("SELECT * FROM procure.purchase_orders WHERE workspace_id = %s AND upstream_order_no = %s "
                             "FOR UPDATE", (ws, record.upstream_order_no)).fetchone()
        return _ingest_existing(conn, ws, order, _find_link(conn, ws, source), record, source,
                                list(record.issues or []), actor_kind, actor_id, False)
    oid = inserted["id"]
    events.record(conn, ws, "order", oid, "order.created", actor_kind=actor_kind, actor_id=actor_id,
                  detail={"source": _source_desc(source), "verdict": verdict, "status": status})
    if row["external_amz_order_no"]:
        aid = _insert_external_attempt(conn, ws, oid, ext_no, _clean_text(record.external_buyer_account),
                                       _source_desc(source))
        _update(conn, ws, oid, {"purchased_attempt_id": aid})
        events.record(conn, ws, "order", oid, "order.external_purchase_recorded", actor_kind=actor_kind,
                      actor_id=actor_id, detail={"amazon_order_no": ext_no,
                                                 "buyer_account": _clean_text(record.external_buyer_account),
                                                 "attempt_id": aid})
    _upsert_link(conn, ws, oid, None, record, source)
    return IngestOutcome(verdict, oid, {}, issues, reason, status)


def _candidate_fields(order: dict[str, Any], inc: dict[str, Any], prv: dict[str, Any] | None) -> dict[str, Any]:
    """输入：任务行、来源新值、来源上次的值（首次关联为 None）→ 输出：{列: 新值}（只含与任务现值不同的）。"""
    cand: dict[str, Any] = {}
    if prv is not None:
        for f in PLAIN_COLUMNS:
            if not _same(inc[f], prv[f]):
                cand[f] = inc[f]
        if any(f in cand for f in ADDRESS_GROUP):
            for f in ADDRESS_GROUP:
                cand[f] = inc[f]
    else:
        for f in PLAIN_COLUMNS:
            if f not in ADDRESS_GROUP and inc[f] is not None:
                cand[f] = inc[f]
        if any(inc[f] is not None for f in ADDRESS_CORE):
            for f in ADDRESS_GROUP:
                cand[f] = inc[f]
    return {f: v for f, v in cand.items() if not _same(v, order[f])}


def _suggested_after(order: dict[str, Any], merged: dict[str, Any], inc: dict[str, Any],
                     prv: dict[str, Any] | None) -> tuple[date | None, str | None]:
    if inc["suggested_date_basis"] == "source":
        return inc["suggested_purchase_date"], "source"
    cleared = prv is not None and prv["suggested_date_basis"] == "source"
    if order["suggested_date_basis"] == "source" and not cleared:
        return order["suggested_purchase_date"], "source"
    d = compute_suggested_date(merged["order_time"], merged["ship_days"])
    return d, ("computed" if d else None)


def _review_change(order, inc, prv, same_kind: bool, source_kind: str) -> dict[str, Any]:
    new_status, new_value = inc["review_status"], inc["review_value"]
    if prv is not None:
        if new_status == prv["review_status"] and _same(new_value, prv["review_value"]):
            return {}
    elif new_value is None and new_status == "unknown":
        return {}
    if order["review_source"] == "console" and (prv is None or new_status == "unknown"):
        return {}   # 控制台人审结论只被来源的真实变化改写
    if not same_kind and (order["review_status"] != "unknown" or new_status == "unknown"):
        return {}   # 另一种来源只能补上「未知」的人审结论
    upd: dict[str, Any] = {}
    if new_status != order["review_status"]:
        upd.update(review_status=new_status, review_source=source_kind, reviewed_by=None, reviewed_at=clock.now())
    if same_kind and not _same(new_value, order["review_value"]):
        upd["review_value"] = new_value
    return upd


def _ingest_existing(conn, ws, order, link, record, source, issues, actor_kind, actor_id, dry_run) -> IngestOutcome:
    prev = UpstreamRecord.from_json(link["normalized"]) if link is not None else None
    inc = _record_values(record)
    prv = _record_values(prev) if prev is not None else None
    status = order["status"]
    same_kind = source.kind == order["primary_source_kind"]
    src = _source_desc(source)
    now = clock.now()

    upd: dict[str, Any] = {}
    changes: dict[str, Any] = {}
    notes: list[str] = []
    evs: list[tuple[str, dict[str, Any]]] = []
    verdict_flags: set[str] = set()
    reason: str | None = None
    to_status: str | None = None
    to_reason: str | None = None
    conflict_payload: dict[str, Any] | None = None
    ext_apply: str | None = None

    cand = _candidate_fields(order, inc, prv)
    review_upd = _review_change(order, inc, prv, same_kind, source.kind)
    mutable = status in PRE or status in ("exception", "cancelled")

    # ① 字段变化
    if same_kind:
        trial = {**order, **cand}
        sp, basis = _suggested_after(order, trial, inc, prv)
        date_upd = {k: v for k, v in (("suggested_purchase_date", sp), ("suggested_date_basis", basis),
                                      ("order_date_bj", clock.bj_date(trial["order_time"]) if trial["order_time"] else None))
                    if not _same(v, order[k])}
        field_upd = {**cand, **date_upd, **review_upd}
        for f, v in field_upd.items():
            if f not in ("suggested_date_basis", "order_date_bj", "review_source", "reviewed_by", "reviewed_at"):
                changes[f] = [order[f], v]
        if mutable:
            upd.update(field_upd)
            if any(f in changes for f in field_upd):
                overridden = [f for f in ("asin", "quantity", "price_cap")
                              if f in changes and order.get(f"override_{f}") is not None]
                evs.append(("order.source_updated", {"source": src, "changes": {f: changes[f] for f in changes},
                                                     "overrides_kept": overridden}))
                verdict_flags.add("update")
        else:
            display = {f: v for f, v in field_upd.items() if f in DISPLAY_COLUMNS}
            blocked = [f for f in changes if f not in DISPLAY_COLUMNS]
            upd.update(display)
            if display:
                verdict_flags.add("update")
            if blocked:
                labels = "、".join(FIELD_LABELS.get(f, f) for f in blocked)
                note = f"上游在采购开始后修改了{labels}"
                notes.append(note)
                reason = f"采购已开始，{note}，未应用"
                evs.append(("order.upstream_changed_after_start",
                            {"source": src, "changes": {f: changes[f] for f in blocked}}))
                verdict_flags.add("conflict")
    else:
        conf: dict[str, list[Any]] = {}
        for f in EXECUTION_FIELDS:
            if f == "suggested_purchase_date":
                new_sp = inc["suggested_purchase_date"]
                said = new_sp is not None and (prv is None or not _same(new_sp, prv["suggested_purchase_date"]))
                if said and not _same(new_sp, order["suggested_purchase_date"]):
                    conf[f] = [order[f], new_sp]
            elif f in cand and (cand[f] is not None or f in ADDRESS_GROUP):
                conf[f] = [order[f], cand[f]]
        if conf:
            changes.update(conf)
            labels = "、".join(FIELD_LABELS.get(f, f) for f in conf)
            reason = f"{'飞书' if source.kind == 'feishu' else '文件'}来源的{labels}与任务不同，需人工核实"
            verdict_flags.add("conflict")
            evs.append(("order.source_conflict", {"source": src, "fields": conf}))
            if status in PRE or status == "exception":
                incoming = {f: v[1] for f, v in conf.items()}
                if "suggested_purchase_date" in incoming:
                    incoming["suggested_date_basis"] = inc["suggested_date_basis"]
                conflict_payload = {"kind": "fields", "source_kind": source.kind, "reason": reason,
                                    "fields": conf, "incoming": incoming, "source": src, "at": now}
                if status == "exception":
                    notes.append(reason)
            else:
                notes.append(reason)
        elif review_upd and mutable:
            upd.update(review_upd)
            changes["review_status"] = [order["review_status"], review_upd.get("review_status")]
            verdict_flags.add("update")
            evs.append(("order.source_updated", {"source": src, "changes": {"review_status": changes["review_status"]},
                                                 "overrides_kept": []}))

    # ② 上游取消 / 撤销取消
    rec_cancel = inc["upstream_cancelled"]
    cancel = rec_cancel and not order["upstream_cancelled"] and (prv is None or not prv["upstream_cancelled"])
    uncancel = (order["upstream_cancelled"] and not rec_cancel
                and ((prv is not None and prv["upstream_cancelled"])
                     or (prv is None and _clean_text(record.after_sale_value) is not None)))
    if cancel:
        cancel_note = _clean_text(record.after_sale_value) or "上游取消"
        upd.update(upstream_cancelled=True, upstream_cancel_note=cancel_note, upstream_cancelled_at=now)
        changes["upstream_cancelled"] = [False, True]
        verdict_flags.add("update")
        detail = {"source": src, "after_sale_value": record.after_sale_value, "status": status}
        if status in PRE or status == "exception":
            to_status, to_reason = "cancelled", f"上游取消（{cancel_note}）"
        elif status == "executing":
            att = inflight_attempt(conn, order)
            submitted = att is not None and att["submitted_at"] is not None
            detail["submitted"] = submitted
            if submitted:
                to_status, to_reason = "result_unknown", "执行中收到取消（已登记提交，需人工核实是否已下单）"
                notes.append("执行中收到上游取消，已登记提交，请核实亚马逊订单")
            else:
                notes.append("执行中收到上游取消，插件将中止本次采购")
        elif status == "result_unknown":
            notes.append("结果待核实期间上游取消，核实后请处理亚马逊订单")
        elif status in ("purchased", "repurchase_pending", "closed"):
            notes.append("采购后上游取消，请人工处理对应的亚马逊订单")
        evs.append(("order.upstream_cancelled", detail))
    elif uncancel:
        upd.update(upstream_cancelled=False, upstream_cancel_note=None, upstream_cancelled_at=None)
        changes["upstream_cancelled"] = [True, False]
        verdict_flags.add("update")
        if status == "cancelled":
            notes.append("上游撤销了取消，如需继续采购请人工撤销取消")
        evs.append(("order.upstream_uncancelled", {"source": src, "after_sale_value": record.after_sale_value}))

    # ③ 外部 AMZ 订单号
    ext_in = inc["external_amz_order_no"]
    if ext_in and (prv is None or ext_in != prv["external_amz_order_no"]) and ext_in not in _known_amazon_nos(conn, order):
        changes["external_amz_order_no"] = [order["external_amz_order_no"], ext_in]
        buyer = _clean_text(record.external_buyer_account)
        if status in PRE or status == "exception":
            dup = _amazon_no_owner(conn, ws, ext_in, order["id"])
            if dup:
                r = f"AMZ 订单号 {ext_in} 已登记在任务 {dup['upstream_order_no']}，需人工核实"
                reason = reason or r
                verdict_flags.add("conflict")
                conflict_payload = {"kind": "external_order_no", "source_kind": source.kind, "reason": r,
                                    "fields": {"external_amz_order_no": [order["external_amz_order_no"], ext_in]},
                                    "incoming": {"external_amz_order_no": ext_in, "external_buyer_account": buyer},
                                    "source": src, "at": now}
                evs.append(("order.source_conflict", {"source": src, "fields": conflict_payload["fields"]}))
                if status == "exception":
                    notes.append(r)
            else:
                ext_apply = ext_in
                upd["external_amz_order_no"] = ext_in
                verdict_flags.add("historical")
                reason = reason or f"来源已有 AMZ 订单号 {ext_in}，按已采购登记，不重新下单"
        else:
            if status == "executing":
                r = f"采购进行中，来源出现 AMZ 订单号 {ext_in}，请核实是否已在别处采购"
            elif status == "result_unknown":
                r = f"来源出现 AMZ 订单号 {ext_in}，可据此核实结果"
            elif status == "cancelled":
                r = f"已取消的任务在来源出现 AMZ 订单号 {ext_in}，请核实"
            else:
                known = order["external_amz_order_no"] or _purchased_no(conn, order)
                r = f"来源的 AMZ 订单号 {ext_in} 与本系统记录 {known or '（无）'} 不同，请核实"
            notes.append(r)
            reason = reason or r
            verdict_flags.add("conflict")
            evs.append(("order.external_order_no_mismatch", {"source": src, "amazon_order_no": ext_in,
                                                             "status": status}))

    # ④ 最终状态
    if conflict_payload is not None:
        upd["conflict"] = conflict_payload
    merged = {**order, **upd}
    if ext_apply:
        merged["assigned_account_id"] = None
    problem = None
    if to_status is None and status in PRE:
        target = derive_status(merged)
        if target == "assigned":
            problem = _assignment_problem(conn, ws, merged)
            if problem:
                upd.update(_CLEAR_ASSIGNMENT)
                merged.update(_CLEAR_ASSIGNMENT)
                target = "queued"
                evs.append(("order.unassigned", {"reason": problem, "source": src}))
        if target != status:
            to_status = target
            to_reason = problem or derived_reason(merged, target)
        elif (upd or problem) and target != "queued":
            upd["status_reason"] = derived_reason(merged, target)
    elif to_status is None and status == "exception" and ext_apply:
        to_status, to_reason = derive_status(merged), derived_reason(merged, derive_status(merged))
    if to_status is not None and to_status not in ALLOWED_TRANSITIONS.get(status, frozenset()) and to_status != status:
        # 防御：理论上不会发生；不能迁移时保持原状态并提醒人工
        notes.append(f"来源变化要求任务变为「{ORDER_STATUS_ZH[to_status]}」，当前状态不允许，请人工处理")
        to_status = None
    if mutable:
        new_missing = compute_missing_fields(merged)
        if new_missing != list(order["missing_fields"] or []):
            upd["missing_fields"] = new_missing
    if notes:
        note = order["attention_note"]
        for n in notes:
            note = _append_note(note, n)
        upd.update(needs_attention=True, attention_note=note)
        evs.append(("order.attention", {"source": src, "notes": notes}))

    if "conflict" in verdict_flags:
        verdict = "conflict"
    elif "historical" in verdict_flags:
        verdict = "historical"
    elif verdict_flags or to_status or set(upd) - {"missing_fields", "status_reason"}:
        verdict = "update"
    else:
        verdict = "unchanged"
    final_status = to_status or status
    issues = issues + _status_issues({**merged, **upd}, final_status)
    if dry_run:
        return IngestOutcome(verdict, order["id"], _j(changes), issues, reason, final_status)

    # ⑤ 写库
    if ext_apply:
        aid = _insert_external_attempt(conn, ws, order["id"], ext_apply, _clean_text(record.external_buyer_account), src)
        upd["purchased_attempt_id"] = aid
        evs.append(("order.external_purchase_recorded", {"amazon_order_no": ext_apply, "attempt_id": aid,
                                                         "buyer_account": _clean_text(record.external_buyer_account)}))
    for kind, detail in evs:
        events.record(conn, ws, "order", order["id"], kind, actor_kind=actor_kind, actor_id=actor_id, detail=detail)
    if to_status is not None:
        row = transition(conn, order, to_status, reason=to_reason, actor_kind=actor_kind, actor_id=actor_id,
                         updates=upd, event_detail={"source": src})
    else:
        row = _update(conn, ws, order["id"], upd) if upd else order
    _upsert_link(conn, ws, order["id"], link, record, source)
    return IngestOutcome(verdict, order["id"], _j(changes), issues, reason, row["status"])


def _purchased_no(conn, order: dict[str, Any]) -> str | None:
    if not order.get("purchased_attempt_id"):
        return None
    r = conn.execute("SELECT amazon_order_no FROM procure.purchase_attempts WHERE id = %s AND workspace_id = %s",
                     (order["purchased_attempt_id"], order["workspace_id"])).fetchone()
    return r["amazon_order_no"] if r else None


# ── 列表与详情 ────────────────────────────────────────────────────────────────

ORDER_VIEWS = ("today", "future", "unassigned", "pending_review", "needs_info", "conflict", "executing",
               "result_unknown", "purchased", "exception", "cancelled", "repurchase_pending", "closed", "all")

_ROW_SELECT = """
SELECT o.*, a.name AS account_name,
       COALESCE((SELECT array_agg(DISTINCT s.kind) FROM procure.order_sources s
                 WHERE s.purchase_order_id = o.id AND s.workspace_id = o.workspace_id), '{}') AS source_kinds,
       la.id AS la_id, la.status AS la_status, la.amazon_order_no AS la_amazon_order_no, la.total AS la_total,
       la.simulated AS la_simulated, la.error_code AS la_error_code, la.finished_at AS la_finished_at
FROM procure.purchase_orders o
LEFT JOIN procure.buyer_accounts a ON a.id = o.assigned_account_id AND a.workspace_id = o.workspace_id
LEFT JOIN LATERAL (
    SELECT * FROM procure.purchase_attempts pa
    WHERE pa.purchase_order_id = o.id AND pa.workspace_id = o.workspace_id
    ORDER BY pa.id DESC LIMIT 1
) la ON true
"""


def _usd(v: Decimal | None) -> str:
    return "未知金额" if v is None else f"${v:.2f}"


def _m(v: Any) -> str | None:
    if v is None:
        return None
    return f"{Decimal(v):.2f}"


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def order_row_out(r: dict[str, Any]) -> dict[str, Any]:
    """输入：带账号名、来源种类、最近尝试列的任务行 → 输出：契约 §10「任务列表行」。"""
    latest = None
    if r.get("la_id") is not None:
        latest = {"id": r["la_id"], "status": r["la_status"], "amazon_order_no": r["la_amazon_order_no"],
                  "total": _m(r["la_total"]), "simulated": r["la_simulated"], "error_code": r["la_error_code"],
                  "finished_at": _iso(r["la_finished_at"])}
    kinds = list(r.get("source_kinds") or [])
    kinds.sort(key=lambda k: (k != r["primary_source_kind"], k))
    return {
        "id": r["id"], "upstream_order_no": r["upstream_order_no"], "store_name": r["store_name"], "sku": r["sku"],
        "asin": r["asin"], "asin_eff": asin_eff(r), "quantity": r["quantity"], "quantity_eff": quantity_eff(r),
        "unit_price": _m(r["unit_price"]), "subtotal_eff": _m(subtotal_eff(r)),
        "price_cap": _m(r["price_cap"]), "price_cap_eff": _m(price_cap_eff(r)), "has_override": has_override(r),
        "suggested_purchase_date": _iso(r["suggested_purchase_date"]), "order_time": _iso(r["order_time"]),
        "recipient_name": r["recipient_name"], "state": r["state"], "postal_code": r["postal_code"],
        "status": r["status"], "status_zh": ORDER_STATUS_ZH.get(r["status"]), "status_reason": r["status_reason"],
        "missing_fields": list(r["missing_fields"] or []), "review_status": r["review_status"],
        "needs_attention": r["needs_attention"], "attention_note": r["attention_note"],
        "assigned_account": ({"id": r["assigned_account_id"], "name": r.get("account_name")}
                             if r["assigned_account_id"] is not None else None),
        "assignment_kind": r["assignment_kind"], "assignment_note": r["assignment_note"],
        "primary_source_kind": r["primary_source_kind"], "source_kinds": kinds,
        "latest_attempt": latest, "updated_at": _iso(r["updated_at"]),
    }


def list_orders(conn, workspace_id: int, *, view: str = "all", q: str | None = None, account_id: int | None = None,
                source: str | None = None, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """输入：工作区、视图、搜索词、账号、来源种类、分页 → 输出：{total, rows}（行为契约 §10 列表行）。

    视图：today = 已分配且建议采购日期 ≤ 北京今天；future = 已分配且日期在今天之后；unassigned = 待分配；
    其余为同名状态；all = 全部。q 搜平台订单号 / ASIN（含覆盖值）/ 收件人。
    """
    view = view or "all"
    if view not in ORDER_VIEWS:
        raise validation(f"未知视图：{view}", field="view")
    if source is not None and source not in ("feishu", "file"):
        raise validation(f"未知来源种类：{source}", field="source")
    try:
        page, page_size = int(page), int(page_size)
    except (TypeError, ValueError):
        raise validation("分页参数必须是整数") from None
    if page < 1:
        raise validation("页码从 1 开始", field="page")
    page_size = max(1, min(page_size, 200))
    where = ["o.workspace_id = %s"]
    params: list[Any] = [workspace_id]
    today = clock.bj_today()
    if view == "today":
        where.append("o.status = 'assigned' AND o.suggested_purchase_date <= %s")
        params.append(today)
    elif view == "future":
        where.append("o.status = 'assigned' AND o.suggested_purchase_date > %s")
        params.append(today)
    elif view == "unassigned":
        where.append("o.status = 'queued'")
    elif view != "all":
        where.append("o.status = %s")
        params.append(view)
    if q and q.strip():
        pat = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(o.upstream_order_no ILIKE %s OR o.asin ILIKE %s OR o.override_asin ILIKE %s "
                     "OR o.recipient_name ILIKE %s)")
        params.extend([pat] * 4)
    if account_id is not None:
        where.append("o.assigned_account_id = %s")
        params.append(int(account_id))
    if source is not None:
        where.append("EXISTS (SELECT 1 FROM procure.order_sources s WHERE s.purchase_order_id = o.id "
                     "AND s.workspace_id = o.workspace_id AND s.kind = %s)")
        params.append(source)
    where_sql = " AND ".join(where)
    total = conn.execute(f"SELECT count(*) AS n FROM procure.purchase_orders o WHERE {where_sql}",
                         params).fetchone()["n"]
    if view in ("today", "future", "unassigned"):
        order_by = "o.suggested_purchase_date ASC NULLS LAST, o.order_time ASC NULLS LAST, o.id ASC"
    else:
        order_by = "o.updated_at DESC, o.id DESC"
    rows = conn.execute(f"{_ROW_SELECT} WHERE {where_sql} ORDER BY {order_by} LIMIT %s OFFSET %s",
                        [*params, page_size, (page - 1) * page_size]).fetchall()
    return {"total": int(total), "rows": [order_row_out(r) for r in rows]}


def allowed_actions(row: dict[str, Any]) -> list[str]:
    """输入：任务行 → 输出：当前可做的人工动作（契约 §10 allowed_actions，前端据此显示按钮）。"""
    s = row["status"]
    acts: list[str] = []
    if s == "pending_review":
        acts.append("review")
    if s in ("queued", "assigned"):
        acts.append("assign")
    if s == "assigned":
        acts.append("unassign")
    if s in PRE or s == "exception":
        acts.append("override")
    if s == "exception":
        acts.append("resume")
    if s in PRE or s == "exception":
        acts.append("cancel")
    if s == "cancelled" and not row.get("upstream_cancelled"):
        acts.append("uncancel")
    if s == "result_unknown":
        acts.append("resolve_unknown")
    if s == "purchased":
        acts.append("mark_amazon_cancelled")
    if s == "repurchase_pending":
        acts.append("repurchase")
    if row.get("conflict") and (s in PRE or s == "exception"):
        acts.append("resolve_conflict")
    return acts


def _attempt_out(a: dict[str, Any]) -> dict[str, Any]:
    out = {k: a.get(k) for k in (
        "id", "attempt_no", "kind", "account_id", "account_name", "device_id", "device_name", "run_id", "simulated",
        "status", "stage", "task_snapshot", "guard_input", "guard_verdict", "amazon_order_no", "buyer_account_name",
        "currency", "card_last4", "delivery_text", "delivery_days", "error_code", "error_detail", "resolution",
        "resolution_note", "resolved_by", "resolved_by_name")}
    for k in ("subtotal", "shipping", "tax", "total_before_tax", "total"):
        out[k] = _m(a.get(k))
    for k in ("bj_date", "delivery_date", "claimed_at", "heartbeat_at", "submitted_at", "finished_at", "resolved_at"):
        out[k] = _iso(a.get(k))
    return out


def get_detail(conn, workspace_id: int, order_id: int) -> dict[str, Any]:
    """输入：工作区、任务 id → 输出：契约 §10 任务详情（列表行 + 原值、覆盖值、收件信息、冲突、来源、尝试、事件、
    allowed_actions）；跨工作区 → NOT_FOUND。"""
    r = conn.execute(f"{_ROW_SELECT} WHERE o.workspace_id = %s AND o.id = %s", (workspace_id, order_id)).fetchone()
    if r is None:
        raise not_found("采购任务")
    out = order_row_out(r)
    address = {k: r[k] for k in ADDRESS_GROUP}
    out["original"] = {
        "asin": r["asin"], "quantity": r["quantity"], "price_cap": _m(r["price_cap"]),
        "unit_price": _m(r["unit_price"]), "sales_total": _m(r["sales_total"]), "ship_days": r["ship_days"],
        "order_time": _iso(r["order_time"]), "order_date_bj": _iso(r["order_date_bj"]),
        "suggested_purchase_date": _iso(r["suggested_purchase_date"]),
        "suggested_date_basis": r["suggested_date_basis"], "ship_method": r["ship_method"],
        "store_name": r["store_name"], "sales_po_no": r["sales_po_no"], "sku": r["sku"],
        "order_line_id": r["order_line_id"], "review_value": r["review_value"],
        "external_amz_order_no": r["external_amz_order_no"], **address,
    }
    override = None
    if has_override(r) or r["override_note"]:
        by = None
        if r["override_by"] is not None:
            u = conn.execute("SELECT username, display_name FROM procure.users WHERE id = %s",
                             (r["override_by"],)).fetchone()
            by = (u["display_name"] or u["username"]) if u else None
        override = {"asin": r["override_asin"], "quantity": r["override_quantity"],
                    "price_cap": _m(r["override_price_cap"]), "note": r["override_note"], "by_name": by,
                    "at": _iso(r["override_at"])}
    out["override"] = override
    out["address"] = address
    out["conflict"] = r["conflict"]
    out["upstream_cancelled"] = r["upstream_cancelled"]
    out["upstream_cancel_note"] = r["upstream_cancel_note"]
    out["review_source"] = r["review_source"]
    out["order_date_bj"] = _iso(r["order_date_bj"])
    out["result_version"] = r["result_version"]
    sources = conn.execute(
        """
        SELECT s.*, fc.name AS connection_name, ib.filename
        FROM procure.order_sources s
        LEFT JOIN procure.feishu_connections fc ON fc.id = s.feishu_connection_id AND fc.workspace_id = s.workspace_id
        LEFT JOIN procure.import_batches ib ON ib.id = s.import_batch_id AND ib.workspace_id = s.workspace_id
        WHERE s.workspace_id = %s AND s.purchase_order_id = %s
        ORDER BY s.first_seen_at, s.id
        """,
        (workspace_id, order_id),
    ).fetchall()
    out["sources"] = [{
        "id": s["id"], "kind": s["kind"], "connection_id": s["feishu_connection_id"],
        "connection_name": s["connection_name"], "record_id": s["feishu_record_id"], "batch_id": s["import_batch_id"],
        "row_no": s["import_row_no"], "filename": s["filename"], "is_writeback_target": s["is_writeback_target"],
        "first_seen_at": _iso(s["first_seen_at"]), "last_seen_at": _iso(s["last_seen_at"]), "snapshot": s["snapshot"],
    } for s in sources]
    attempts = conn.execute(
        """
        SELECT pa.*, ba.name AS account_name, d.name AS device_name,
               COALESCE(u.display_name, u.username) AS resolved_by_name
        FROM procure.purchase_attempts pa
        LEFT JOIN procure.buyer_accounts ba ON ba.id = pa.account_id AND ba.workspace_id = pa.workspace_id
        LEFT JOIN procure.devices d ON d.id = pa.device_id AND d.workspace_id = pa.workspace_id
        LEFT JOIN procure.users u ON u.id = pa.resolved_by
        WHERE pa.workspace_id = %s AND pa.purchase_order_id = %s
        ORDER BY pa.attempt_no DESC, pa.id DESC
        """,
        (workspace_id, order_id),
    ).fetchall()
    out["attempts"] = [_attempt_out(a) for a in attempts]
    evs = conn.execute(
        """
        SELECT e.*, CASE WHEN e.actor_kind = 'user' THEN COALESCE(u.display_name, u.username)
                         WHEN e.actor_kind = 'device' THEN d.name END AS actor_name
        FROM procure.events e
        LEFT JOIN procure.users u ON e.actor_kind = 'user' AND u.id = e.actor_id
        LEFT JOIN procure.devices d ON e.actor_kind = 'device' AND d.id = e.actor_id AND d.workspace_id = e.workspace_id
        WHERE e.workspace_id = %s AND e.order_id = %s
        ORDER BY e.at DESC, e.id DESC
        LIMIT 200
        """,
        (workspace_id, order_id),
    ).fetchall()
    out["events"] = [{"id": e["id"], "entity": e["entity"], "entity_id": e["entity_id"], "order_id": e["order_id"],
                      "at": _iso(e["at"]), "actor_kind": e["actor_kind"], "actor_id": e["actor_id"],
                      "actor_name": e["actor_name"], "kind": e["kind"], "detail": e["detail"]} for e in evs]
    out["allowed_actions"] = allowed_actions(r)
    return out


# ── 人工动作 ──────────────────────────────────────────────────────────────────


def _actor(actor_user_id: int | None) -> tuple[str, int | None]:
    return ("user", actor_user_id) if actor_user_id is not None else ("system", None)


def _note(note: Any) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str):
        raise validation("备注必须是文本", field="note")
    s = note.strip()
    if len(s) > 1000:
        raise validation("备注不能超过 1000 字", field="note")
    return s or None


def _invalid_for(row: dict[str, Any], what: str) -> DomainError:
    zh = ORDER_STATUS_ZH.get(row["status"], row["status"])
    return DomainError("INVALID_TRANSITION", f"「{zh}」状态的任务不能{what}", 409, {"status": row["status"]})


def _record_action(conn, ws: int, order_id: int, kind: str, actor_user_id: int | None, detail: dict[str, Any]) -> None:
    ak, aid = _actor(actor_user_id)
    events.record(conn, ws, "order", order_id, kind, actor_kind=ak, actor_id=aid, detail=detail)


def _review_one(conn, ws: int, order: dict[str, Any], decision: str, note: str | None,
                actor_user_id: int | None) -> dict[str, Any]:
    if order["status"] not in PRE:
        raise _invalid_for(order, "人审")
    ak, aid = _actor(actor_user_id)
    upd = {"review_status": "approved" if decision == "approve" else "rejected", "review_source": "console",
           "reviewed_by": actor_user_id, "reviewed_at": clock.now()}
    _record_action(conn, ws, order["id"], "order.reviewed", actor_user_id, {"decision": decision, "note": note})
    return _rederive(conn, ws, order, upd, actor_kind=ak, actor_id=aid, event_detail={"action": "review"})


def review(conn, workspace_id: int, order_id: int, decision: str, note: str | None = None,
           actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、approve / reject、备注、操作人 → 输出：任务详情。只限开始前的状态；通过后按派生规则
    进入待分配 / 资料待补齐等（不自动分配）。"""
    if decision not in ("approve", "reject"):
        raise validation("人审结论只能是 approve 或 reject", field="decision")
    order = get_row(conn, workspace_id, order_id, for_update=True)
    _review_one(conn, workspace_id, order, decision, _note(note), actor_user_id)
    return get_detail(conn, workspace_id, order_id)


def bulk_review(conn, workspace_id: int, ids: list[int], decision: str, note: str | None = None,
                actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务 id 列表、approve / reject、备注、操作人 → 输出：{updated: [id], skipped: [{id, reason}]}。"""
    if decision not in ("approve", "reject"):
        raise validation("人审结论只能是 approve 或 reject", field="decision")
    if not isinstance(ids, list) or not ids:
        raise validation("请选择要人审的任务", field="ids")
    if len(ids) > 500:
        raise validation("一次最多人审 500 笔", field="ids")
    note = _note(note)
    updated: list[int] = []
    skipped: list[dict[str, Any]] = []
    for raw in dict.fromkeys(ids):
        if isinstance(raw, bool) or not isinstance(raw, int):
            skipped.append({"id": raw, "reason": "任务 id 格式不对"})
            continue
        row = conn.execute("SELECT * FROM procure.purchase_orders WHERE id = %s AND workspace_id = %s FOR UPDATE",
                           (raw, workspace_id)).fetchone()
        if row is None:
            skipped.append({"id": raw, "reason": "采购任务不存在"})
            continue
        if row["status"] not in PRE:
            skipped.append({"id": raw, "reason": f"「{ORDER_STATUS_ZH[row['status']]}」状态的任务不能人审"})
            continue
        _review_one(conn, workspace_id, row, decision, note, actor_user_id)
        updated.append(raw)
    return {"updated": updated, "skipped": skipped}


def assign_manual(conn, workspace_id: int, order_id: int, account_id: int,
                  actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、目标账号、操作人 → 输出：任务详情。校验见 allocation.check_manual_assignment；
    成功后 assignment_kind='manual'（之后的自动分配不再移动它）。"""
    from services import allocation

    order = get_row(conn, workspace_id, order_id, for_update=True)
    account = _account_row(conn, workspace_id, account_id)
    if order["status"] not in ("queued", "assigned"):
        raise _invalid_for(order, "改派账号")
    if inflight_attempt(conn, order) is not None:
        raise conflict("任务正在执行，不能改派", code="ATTEMPT_IN_FLIGHT")
    allocation.check_manual_assignment(conn, workspace_id, order, account, clock.bj_today())
    ak, aid = _actor(actor_user_id)
    upd = {"assigned_account_id": account["id"], "assignment_kind": "manual", "assigned_at": clock.now(),
           "assignment_note": "人工改派"}
    _record_action(conn, workspace_id, order_id, "order.assigned", actor_user_id,
                   {"account_id": account["id"], "account_name": account["name"], "kind": "manual",
                    "from_account_id": order["assigned_account_id"]})
    if order["status"] == "assigned":
        _update(conn, workspace_id, order_id, {**upd, "status_reason": None})
    else:
        transition(conn, order, "assigned", reason=None, actor_kind=ak, actor_id=aid, updates=upd,
                   event_detail={"action": "assign"})
    return get_detail(conn, workspace_id, order_id)


def unassign(conn, workspace_id: int, order_id: int, actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、操作人 → 输出：任务详情（已分配 → 待分配；下次自动分配可能重新分给某个账号）。"""
    order = get_row(conn, workspace_id, order_id, for_update=True)
    if order["status"] != "assigned":
        raise _invalid_for(order, "解除分配")
    if inflight_attempt(conn, order) is not None:
        raise conflict("任务正在执行，不能解除分配", code="ATTEMPT_IN_FLIGHT")
    ak, aid = _actor(actor_user_id)
    _record_action(conn, workspace_id, order_id, "order.unassigned", actor_user_id,
                   {"from_account_id": order["assigned_account_id"]})
    transition(conn, order, "queued", reason="人工解除分配", actor_kind=ak, actor_id=aid,
               event_detail={"action": "unassign"})
    return get_detail(conn, workspace_id, order_id)


def set_override(conn, workspace_id: int, order_id: int, *, asin: Any = UNSET, quantity: Any = UNSET,
                 price_cap: Any = UNSET, note: str | None = None, actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、要覆盖的 ASIN / 数量 / 限价（UNSET = 不改，None = 清除）、备注、操作人 → 输出：任务详情。

    只限开始前或异常状态；覆盖值只作用于采购，不回填来源，后续导入不覆盖。开始前的任务按新有效值
    重新派生并校验分配（金额不在区间 → 解除分配）；异常任务只更新缺失字段，恢复时再校验。
    """
    order = get_row(conn, workspace_id, order_id, for_update=True)
    if order["status"] not in PRE and order["status"] != "exception":
        raise _invalid_for(order, "修改商品或限价")
    upd: dict[str, Any] = {}
    if asin is not UNSET:
        if asin is None or (isinstance(asin, str) and not asin.strip()):
            upd["override_asin"] = None
        else:
            if not isinstance(asin, str) or not ASIN_RE.match(asin.strip().upper()):
                raise validation("ASIN 应为 10 位字母或数字", field="asin")
            upd["override_asin"] = asin.strip().upper()
    if quantity is not UNSET:
        if quantity is None:
            upd["override_quantity"] = None
        else:
            qv = quantity
            if isinstance(qv, str) and qv.strip().isdigit():
                qv = int(qv.strip())
            if isinstance(qv, bool) or not isinstance(qv, int) or qv <= 0 or qv > 999:
                raise validation("数量应为 1 到 999 的整数", field="quantity")
            upd["override_quantity"] = qv
    if price_cap is not UNSET:
        if price_cap is None or (isinstance(price_cap, str) and not price_cap.strip()):
            upd["override_price_cap"] = None
        else:
            pv = None if isinstance(price_cap, bool) else money(price_cap)
            if pv is None or pv <= 0:
                raise validation("限价应为大于 0 的美元金额", field="price_cap")
            upd["override_price_cap"] = pv
    if not upd:
        raise validation("没有要修改的内容")
    merged = {**order, **upd}
    any_override = has_override(merged)
    note = _note(note)
    upd.update(override_note=note if any_override else None,
               override_by=actor_user_id if any_override else None,
               override_at=clock.now() if any_override else None)
    before = {k: order[k] for k in _OVERRIDE_COLS}
    after = {k: merged[k] for k in _OVERRIDE_COLS}
    _record_action(conn, workspace_id, order_id, "order.override_set", actor_user_id,
                   {"before": before, "after": after, "note": note})
    ak, aid = _actor(actor_user_id)
    if order["status"] in PRE:
        _rederive(conn, workspace_id, order, upd, actor_kind=ak, actor_id=aid, event_detail={"action": "override"})
    else:
        upd["missing_fields"] = compute_missing_fields({**order, **upd})
        _update(conn, workspace_id, order_id, upd)
    return get_detail(conn, workspace_id, order_id)


def resume(conn, workspace_id: int, order_id: int, note: str | None = None,
           actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、备注、操作人 → 输出：任务详情（异常 → 按派生规则重新校验；页面错误重试计数清零）。"""
    order = get_row(conn, workspace_id, order_id, for_update=True)
    if order["status"] != "exception":
        raise _invalid_for(order, "恢复")
    ak, aid = _actor(actor_user_id)
    _record_action(conn, workspace_id, order_id, "order.resumed", actor_user_id,
                   {"note": _note(note), "previous_reason": order["status_reason"]})
    _rederive(conn, workspace_id, order, {"page_load_retries": 0, "current_attempt_id": None},
              actor_kind=ak, actor_id=aid, event_detail={"action": "resume"})
    return get_detail(conn, workspace_id, order_id)


def cancel(conn, workspace_id: int, order_id: int, note: str | None = None,
           actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、备注、操作人 → 输出：任务详情（只限开始前或异常；解除分配）。"""
    order = get_row(conn, workspace_id, order_id, for_update=True)
    if order["status"] not in PRE and order["status"] != "exception":
        raise _invalid_for(order, "取消")
    note = _note(note)
    ak, aid = _actor(actor_user_id)
    _record_action(conn, workspace_id, order_id, "order.cancelled_manually", actor_user_id, {"note": note})
    transition(conn, order, "cancelled", reason=f"人工取消：{note}" if note else "人工取消",
               actor_kind=ak, actor_id=aid, event_detail={"action": "cancel"})
    return get_detail(conn, workspace_id, order_id)


def uncancel(conn, workspace_id: int, order_id: int, note: str | None = None,
             actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、备注、操作人 → 输出：任务详情（已取消 → 派生状态；上游仍为取消时拒绝）。"""
    order = get_row(conn, workspace_id, order_id, for_update=True)
    if order["status"] != "cancelled":
        raise _invalid_for(order, "撤销取消")
    if order["upstream_cancelled"]:
        raise validation("上游仍为取消状态，不能撤销取消")
    ak, aid = _actor(actor_user_id)
    _record_action(conn, workspace_id, order_id, "order.uncancelled", actor_user_id, {"note": _note(note)})
    _rederive(conn, workspace_id, order, {}, actor_kind=ak, actor_id=aid, event_detail={"action": "uncancel"})
    return get_detail(conn, workspace_id, order_id)


_AMOUNT_KEYS = ("subtotal", "shipping", "tax", "total_before_tax", "total")


def resolve_unknown(conn, workspace_id: int, order_id: int, outcome: str, *, amazon_order_no: str | None = None,
                    amounts: dict[str, Any] | None = None, note: str | None = None,
                    actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、ordered / not_ordered、亚马逊订单号、金额、备注、操作人 → 输出：任务详情。

    - ordered：待核实尝试 → succeeded（resolution='ordered'，记单号与金额）；任务 → purchased，结果版本 +1，
      记 purchased_attempt_id，并调用 writeback.enqueue_for_order。单号已登记在别的任务 → VALIDATION。
    - not_ordered：尝试 → failed（resolution='not_ordered'，释放额度）；任务回开始前的派生状态，保留分配。
    - 都**不**自动恢复被暂停的账号（由控制台显式恢复）。尝试仍在途 → ATTEMPT_IN_FLIGHT。
    """
    if outcome not in ("ordered", "not_ordered"):
        raise validation("核实结论只能是 ordered 或 not_ordered", field="outcome")
    order = get_row(conn, workspace_id, order_id, for_update=True)
    if order["status"] != "result_unknown":
        raise _invalid_for(order, "核实结果")
    att = conn.execute(
        "SELECT * FROM procure.purchase_attempts WHERE purchase_order_id = %s AND workspace_id = %s "
        "AND status = 'unknown' ORDER BY id DESC LIMIT 1 FOR UPDATE",
        (order_id, workspace_id),
    ).fetchone()
    if att is None:
        if inflight_attempt(conn, order) is not None:
            raise conflict("该任务的采购尝试仍在进行中，等插件回报或执行权超时后再核实", code="ATTEMPT_IN_FLIGHT")
        raise validation("没有待核实的采购尝试")
    note = _note(note)
    now = clock.now()
    ak, aid = _actor(actor_user_id)
    if outcome == "ordered":
        no = (amazon_order_no or "").strip() if isinstance(amazon_order_no, str) else ""
        if not no:
            raise validation("核实已下单需要填写亚马逊订单号", field="amazon_order_no")
        if not att["simulated"]:
            dup = _amazon_no_owner(conn, workspace_id, no, order_id)
            if dup:
                raise validation(f"亚马逊订单号 {no} 已登记在任务 {dup['upstream_order_no']}", field="amazon_order_no")
        amts: dict[str, Decimal | None] = {}
        for k, v in (amounts or {}).items():
            if k not in _AMOUNT_KEYS:
                raise validation(f"未知金额项：{k}", field="amounts")
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            mv = None if isinstance(v, bool) else money(v)
            if mv is None or mv < 0:
                raise validation(f"金额格式不对：{k}", field="amounts")
            amts[k] = mv
        sets = ", ".join(f"{k} = %s" for k in amts)
        conn.execute(
            "UPDATE procure.purchase_attempts SET status = 'succeeded', resolution = 'ordered', resolution_note = %s, "
            "resolved_by = %s, resolved_at = %s, amazon_order_no = %s, finished_at = COALESCE(finished_at, %s)"
            + (f", {sets}" if sets else "") + " WHERE id = %s AND workspace_id = %s",
            [note, actor_user_id, now, no, now, *amts.values(), att["id"], workspace_id],
        )
        _record_action(conn, workspace_id, order_id, "order.unknown_resolved", actor_user_id,
                       {"outcome": outcome, "attempt_id": att["id"], "amazon_order_no": no, "note": note})
        transition(conn, order, "purchased", reason=None, actor_kind=ak, actor_id=aid,
                   updates={"purchased_attempt_id": att["id"], "current_attempt_id": None,
                            "result_version": order["result_version"] + 1},
                   event_detail={"action": "resolve_unknown", "outcome": outcome})
        writeback.enqueue_for_order(conn, workspace_id, order_id)
    else:
        conn.execute(
            "UPDATE procure.purchase_attempts SET status = 'failed', resolution = 'not_ordered', resolution_note = %s, "
            "resolved_by = %s, resolved_at = %s, finished_at = COALESCE(finished_at, %s) WHERE id = %s AND workspace_id = %s",
            (note, actor_user_id, now, now, att["id"], workspace_id),
        )
        _record_action(conn, workspace_id, order_id, "order.unknown_resolved", actor_user_id,
                       {"outcome": outcome, "attempt_id": att["id"], "note": note})
        first = "assigned" if order["assigned_account_id"] is not None else "queued"
        row = transition(conn, order, first, reason="人工核实未下单", actor_kind=ak, actor_id=aid,
                         updates={"current_attempt_id": None},
                         event_detail={"action": "resolve_unknown", "outcome": outcome})
        _rederive(conn, workspace_id, row, {}, actor_kind=ak, actor_id=aid)
    return get_detail(conn, workspace_id, order_id)


def mark_amazon_cancelled(conn, workspace_id: int, order_id: int, note: str | None = None,
                          actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、备注、操作人 → 输出：任务详情（已采购 → 待决定重采；不自动重买）。"""
    order = get_row(conn, workspace_id, order_id, for_update=True)
    if order["status"] != "purchased":
        raise _invalid_for(order, "登记亚马逊取消")
    note = _note(note)
    ak, aid = _actor(actor_user_id)
    _record_action(conn, workspace_id, order_id, "order.amazon_cancelled", actor_user_id, {"note": note})
    transition(conn, order, "repurchase_pending",
               reason=f"亚马逊取消了采购订单：{note}" if note else "亚马逊取消了采购订单",
               actor_kind=ak, actor_id=aid, event_detail={"action": "mark_amazon_cancelled"})
    return get_detail(conn, workspace_id, order_id)


def decide_repurchase(conn, workspace_id: int, order_id: int, decision: str, note: str | None = None,
                      actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、approve / decline、备注、操作人 → 输出：任务详情。

    approve：解除原分配、清掉外部单号标记，按派生规则回到待分配等（下一次尝试的种类为 'repurchase'，
    见 next_attempt_kind）；decline：→ closed。
    """
    if decision not in ("approve", "decline"):
        raise validation("重采决定只能是 approve 或 decline", field="decision")
    order = get_row(conn, workspace_id, order_id, for_update=True)
    if order["status"] != "repurchase_pending":
        raise _invalid_for(order, "决定重采")
    note = _note(note)
    ak, aid = _actor(actor_user_id)
    _record_action(conn, workspace_id, order_id, "order.repurchase_decided", actor_user_id,
                   {"decision": decision, "note": note})
    if decision == "approve":
        _rederive(conn, workspace_id, order,
                  {**_CLEAR_ASSIGNMENT, "external_amz_order_no": None, "page_load_retries": 0,
                   "current_attempt_id": None},
                  actor_kind=ak, actor_id=aid, event_detail={"action": "repurchase", "decision": decision})
    else:
        transition(conn, order, "closed", reason=f"人工决定不重采：{note}" if note else "人工决定不重采",
                   actor_kind=ak, actor_id=aid, event_detail={"action": "repurchase", "decision": decision})
    return get_detail(conn, workspace_id, order_id)


def _from_j(f: str, v: Any) -> Any:
    if v is None:
        return None
    if f in _MONEY_COLUMNS:
        return money(v)
    if f in ("suggested_purchase_date", "order_date_bj"):
        return date.fromisoformat(v) if isinstance(v, str) else v
    if f == "order_time":
        return datetime.fromisoformat(v) if isinstance(v, str) else v
    if f in ("quantity", "ship_days"):
        return int(v)
    return v


def resolve_conflict(conn, workspace_id: int, order_id: int, choice: str, note: str | None = None,
                     actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、keep / accept、备注、操作人 → 输出：任务详情。

    keep：保留任务现值，清除冲突；accept：把 conflict.incoming 里的来源新值写进原值（外部单号冲突时登记为
    已采购，单号仍属别的任务 → VALIDATION），清除冲突。之后按派生规则重新校验。
    """
    if choice not in ("keep", "accept"):
        raise validation("冲突处理只能是 keep 或 accept", field="choice")
    order = get_row(conn, workspace_id, order_id, for_update=True)
    c = order["conflict"]
    if not c:
        raise validation("该任务没有待核实的冲突")
    if order["status"] not in PRE and order["status"] != "exception":
        raise _invalid_for(order, "处理冲突")
    note = _note(note)
    ak, aid = _actor(actor_user_id)
    upd: dict[str, Any] = {"conflict": None}
    applied: dict[str, Any] = {}
    if choice == "accept":
        incoming = c.get("incoming") or {}
        if c.get("kind") == "external_order_no":
            no = incoming.get("external_amz_order_no")
            dup = _amazon_no_owner(conn, workspace_id, no, order_id) if no else None
            if dup:
                raise validation(f"AMZ 订单号 {no} 仍登记在任务 {dup['upstream_order_no']}，不能接受")
            if no:
                aid_ext = _insert_external_attempt(conn, workspace_id, order_id, no,
                                                   incoming.get("external_buyer_account"), c.get("source") or {})
                upd.update(external_amz_order_no=no, purchased_attempt_id=aid_ext)
                applied["external_amz_order_no"] = no
        else:
            for f, v in incoming.items():
                if f in ORDER_COLUMNS and f not in ("conflict",):
                    upd[f] = _from_j(f, v)
                    applied[f] = v
            if "order_time" in upd:
                upd["order_date_bj"] = clock.bj_date(upd["order_time"]) if upd["order_time"] else None
    _record_action(conn, workspace_id, order_id, "order.conflict_resolved", actor_user_id,
                   {"choice": choice, "note": note, "fields": c.get("fields"), "applied": applied})
    if order["status"] in PRE:
        _rederive(conn, workspace_id, order, upd, actor_kind=ak, actor_id=aid,
                  event_detail={"action": "resolve_conflict", "choice": choice})
    else:
        merged = {**order, **upd}
        upd["missing_fields"] = compute_missing_fields(merged)
        if merged.get("external_amz_order_no"):
            transition(conn, order, "purchased", reason=derived_reason(merged, "purchased"), actor_kind=ak,
                       actor_id=aid, updates=upd, event_detail={"action": "resolve_conflict", "choice": choice})
        else:
            _update(conn, workspace_id, order_id, upd)
    return get_detail(conn, workspace_id, order_id)


def clear_attention(conn, workspace_id: int, order_id: int, note: str | None = None,
                    actor_user_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、备注、操作人 → 输出：任务详情（清除 needs_attention 提醒，原提醒写进事件）。"""
    order = get_row(conn, workspace_id, order_id, for_update=True)
    if not order["needs_attention"]:
        return get_detail(conn, workspace_id, order_id)
    _record_action(conn, workspace_id, order_id, "order.attention_cleared", actor_user_id,
                   {"note": _note(note), "previous": order["attention_note"]})
    _update(conn, workspace_id, order_id, {"needs_attention": False, "attention_note": None})
    return get_detail(conn, workspace_id, order_id)


def revert_simulation(conn, workspace_id: int, order_id: int, *, actor_kind: str = "user",
                      actor_id: int | None = None) -> dict[str, Any]:
    """输入：工作区、任务、操作者 → 输出：任务行。契约 §5「清除模拟结果」专用：把只经过模拟执行的任务直接退回
    开始前的派生状态（保留分配；绕过迁移表，这是唯一的例外）。

    调用前须先把该任务的模拟尝试改成 aborted；任务有真实尝试（成功 / 待核实 / 在途）→ CONFLICT。
    开始前或已取消的任务原样返回。
    """
    order = get_row(conn, workspace_id, order_id, for_update=True)
    if order["status"] in PRE or order["status"] == "cancelled":
        return order
    real = conn.execute(
        "SELECT 1 FROM procure.purchase_attempts WHERE purchase_order_id = %s AND workspace_id = %s "
        "AND NOT simulated AND kind <> 'external' AND status NOT IN ('failed', 'aborted') LIMIT 1",
        (order_id, workspace_id),
    ).fetchone()
    if real is not None:
        raise conflict("任务有真实采购尝试，不能按模拟结果回退")
    if inflight_attempt(conn, order) is not None:
        raise conflict("任务仍有在途的采购尝试，请先中止", code="ATTEMPT_IN_FLIGHT")
    upd: dict[str, Any] = {"current_attempt_id": None, "page_load_retries": 0}
    if order["purchased_attempt_id"] is not None:
        pa = conn.execute("SELECT simulated FROM procure.purchase_attempts WHERE id = %s",
                          (order["purchased_attempt_id"],)).fetchone()
        if pa is not None and pa["simulated"]:
            upd["purchased_attempt_id"] = None
    merged = {**order, **upd}
    target = derive_status(merged)
    problem = None
    if target == "assigned":
        problem = _assignment_problem(conn, workspace_id, merged)
        if problem:
            upd.update(_CLEAR_ASSIGNMENT)
            merged.update(_CLEAR_ASSIGNMENT)
            target = "queued"
    if target in _CLEAR_ASSIGNMENT_ON:
        upd.update(_CLEAR_ASSIGNMENT)
    upd["missing_fields"] = compute_missing_fields(merged)
    reason = problem or derived_reason(merged, target)
    upd["status_reason"] = reason
    sets, params = _set_clause(upd)
    now = clock.now()
    row = conn.execute(
        f"UPDATE procure.purchase_orders SET status = %s, status_changed_at = %s, updated_at = %s, {sets} "
        "WHERE id = %s AND workspace_id = %s RETURNING *",
        [target, now, now, *params, order_id, workspace_id],
    ).fetchone()
    events.record(conn, workspace_id, "order", order_id, "order.status_changed", actor_kind=actor_kind,
                  actor_id=actor_id, detail={"from": order["status"], "to": target, "reason": "已清除模拟结果",
                                             "simulation_reset": True})
    return row


def _j(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _j(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_j(x) for x in v]
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v
