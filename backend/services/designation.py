"""来源指定买家号（方案 v2「修订二」、契约 §3.2）：清洗、来源行是否表态、解析到账号、账号能不能接。

叶子模块：只依赖 models 与 normalize，不导入 orders / allocation / accounts / execution（它们导入本模块）。
除了 load_context / target 各读一次账号表与忽略名单，其余都是纯函数。

语义（先读这几条）：

- 来源行**没有可信的 AMZ 订单号**、且「买家号 ID」或「买家号」非空 → 这一行**指定**了采购账号；两列都空 → 自动分配。
- 来源行**有可信的 AMZ 订单号** → 对指定**不表态**（SILENT）：既不设置也不清除任务上的指定（这两列此时只是「谁买的」）。
- **没读到不等于清空**：来源表里找不到买家号列（SourceRef.buyer_readable=False）同样是 SILENT；表格错误值由
  normalize 原样留在字段里，成为一个解析不了的指定（任务留在待分配），不会被当成留空。
- 指定的任务绝不悄悄落到别的账号：解析不出账号、或指定账号此刻接不了 → 留在待分配并写明原因（resolve 的 problem、
  placement_problem）。优先级：人工改派（固定）> 来源指定 > 自动。

三处共用同一套解析（resolve_order / target）：分配第 0 步（services/allocation）、「分配仍有效吗」
（orders._assignment_problem）、领取时复核（execution.next_attempt）。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from services import normalize
from services.models import CUSTOMER_ID_RE


class _Silent:
    """「这一行对指定不表态」的标记（与「表态为空」(None, None) 区分）。"""

    def __repr__(self) -> str:
        return "SILENT"

    def __bool__(self) -> bool:
        return False


SILENT: Any = _Silent()

#: 任务上存指定的两列（清洗后的买家号 ID、买家号）
ORDER_COLUMNS = ("designated_customer_id", "designated_buyer_name")
#: 上游记录里对应的两个字段（同序）
RECORD_KEYS = ("external_buyer_customer_id", "external_buyer_account")
#: 事件：来源指定的买家号变了 {changes: {designated_customer_id: [旧, 新], designated_buyer_name: [旧, 新]}, source}
EVENT_KIND = "order.designation_changed"
#: assignment_kind 取值：按来源指定分配
KIND = "source"
#: 指定被来源清空、任务留在不参与自动分配的账号上时的分配说明
NOTE_CLEARED_KEPT = "来源取消了指定，保留在原账号"
EMPTY_LABEL = "（未指定）"
_SHOW_MAX = 40


# ── 清洗 ─────────────────────────────────────────────────────────────────────


def clean_customer_id(v: Any) -> str | None:
    """输入：买家号 ID 原文 → 输出：NFKC → 去掉全部空白 → 转大写；空 → None（不校验形状）。"""
    if v is None:
        return None
    s = "".join(unicodedata.normalize("NFKC", str(v)).split()).upper()
    return s or None


def clean_name(v: Any) -> str | None:
    """输入：买家号原文 → 输出：去首尾空白；空 → None。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def name_key(v: Any) -> str:
    """输入：名称 → 输出：宽松匹配用的键：NFKC → casefold → 去掉全部空白（忽略空格、全半角、大小写差异）。"""
    if v is None:
        return ""
    return "".join(unicodedata.normalize("NFKC", str(v)).casefold().split())


def id_shape_ok(customer_id: str | None) -> bool:
    """输入：清洗后的买家号 ID → 输出：是否符合 ^A[0-9A-Z]{8,}$（与账号登记的形状同一条）。"""
    return bool(customer_id) and bool(CUSTOMER_ID_RE.match(customer_id))


def _show(v: Any) -> str:
    s = str(v)
    return s if len(s) <= _SHOW_MAX else s[:_SHOW_MAX] + "…"


def label(customer_id: str | None, name: str | None) -> str | None:
    """输入：指定的两列 → 输出：给人看的一个值：有 ID 写 ID，否则写名称；都空 → None。"""
    return customer_id or name or None


def label_or_empty(customer_id: str | None, name: str | None) -> str:
    """输入：指定的两列 → 输出：同 label，空写「（未指定）」。"""
    return label(customer_id, name) or EMPTY_LABEL


# ── 来源行是否表态 ────────────────────────────────────────────────────────────


def _said(v: Any) -> Any:
    """占位写法（无、—、N/A、0、/ 等）按没写处理（normalize 已经这样做；旧版存下的规范化记录在这里补上）。"""
    return None if normalize.is_placeholder(v) else v


def statement(record: Any, readable: bool = True) -> Any:
    """输入：规范化后的上游记录（UpstreamRecord 或同形状对象）、来源这次是否读得到买家号两列
    → 输出：SILENT，或清洗后的 (买家号 ID, 买家号)（可以是 (None, None) = 表态为「没有指定」）。

    SILENT：记录带可信 AMZ 订单号（normalize.amazon_order_no_ok），或 readable=False。格式不对的单号不算可信，
    指定照常生效。
    """
    if not readable or record is None:
        return SILENT
    if normalize.amazon_order_no_ok(getattr(record, "external_amz_order_no", None)):
        return SILENT
    return (clean_customer_id(_said(getattr(record, "external_buyer_customer_id", None))),
            clean_name(_said(getattr(record, "external_buyer_account", None))))


def is_empty(stmt: Any) -> bool:
    """输入：statement 的结果 → 输出：是否「表态为空」（SILENT 不算）。"""
    return stmt is not SILENT and not any(stmt)


def of_order(order_row: dict[str, Any]) -> tuple[str | None, str | None]:
    """输入：任务行 → 输出：任务上的指定 (买家号 ID, 买家号)。"""
    return order_row.get("designated_customer_id"), order_row.get("designated_buyer_name")


def has(order_row: dict[str, Any]) -> bool:
    """输入：任务行 → 输出：任务有没有来源指定（任一列非空）。"""
    return bool(order_row.get("designated_customer_id") or order_row.get("designated_buyer_name"))


# ── 解析到账号 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Resolution:
    """resolve 的结果。account：命中的**未归档**账号行（没命中为 None）；problem：解析不了的原因（None = 解析成功）；
    note：分配说明（解析成功时非空）。"""

    account: dict[str, Any] | None = None
    problem: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class Context:
    """解析要用的工作区数据：全部账号行（含已归档）与忽略名单里的买家号 ID。一次分配 / 一次列表查询只读一次。"""

    accounts: tuple[dict[str, Any], ...] = ()
    ignored: frozenset[str] = frozenset()


def _by_name(accounts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(accounts, key=lambda a: (a["name"], a["id"]))


def _name_matches(name: str, pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """名称 → 对应的账号：先找名称（去首尾空白后）逐字相同的；没有再按 name_key 宽松匹配（可能多个）。"""
    exact = [a for a in pool if (a["name"] or "").strip() == name]
    if exact:
        return exact
    key = name_key(name)
    if not key:
        return []
    return [a for a in pool if name_key(a["name"]) == key]


def _names(accounts: list[dict[str, Any]]) -> str:
    return "、".join(a["name"] for a in accounts)


def resolve(customer_id: str | None, name: str | None, accounts: Iterable[dict[str, Any]],
            ignored_ids: Iterable[str] = ()) -> Resolution:
    """输入：清洗后的买家号 ID、买家号、本工作区**全部**账号行（含已归档）、忽略名单里的 ID
    → 输出：Resolution（两列都空 → 全为 None 的结果）。

    1. 有买家号 ID：形状不对 → 原因；未归档账号 A 登记着它 → 命中 A（同时写了名称：名称按第 2 条对应到别的未归档
       账号 → 原因「不是同一个账号」；名称谁也对不上 → 仍命中 A，说明里注明名称不同）；没有未归档账号登记：
       只有已归档账号登记着 → 原因；名称对应到某个未归档账号 → 原因（那个账号没填 ID / 填的是别的 ID）；
       其余 → 原因「还没有登记为账号」（ID 在忽略名单里时另注明）。
    2. 只有名称：先找名称逐字相同的未归档账号，没有再宽松匹配（name_key）：恰好一个 → 命中；多个 → 原因；
       只对应到已归档账号 → 原因；都没有 → 原因。
    """
    if not customer_id and not name:
        return Resolution()
    rows = _by_name(accounts)
    live = [a for a in rows if a.get("archived_at") is None]
    gone = [a for a in rows if a.get("archived_at") is not None]
    if customer_id:
        x = customer_id
        if not id_shape_ok(x):
            return Resolution(problem=f"来源指定的买家号 ID「{_show(x)}」格式不对（应以 A 开头，至少 9 位大写字母或数字）；"
                                      "在来源里改正或清空后会重新分配")
        note = f"按来源的买家号 ID {x}"
        hit = next((a for a in live if a.get("amazon_customer_id") == x), None)
        named = _name_matches(name, live) if name else []
        if hit is not None:
            if not name or any(a["id"] == hit["id"] for a in named):
                return Resolution(account=hit, note=note)
            if named:
                return Resolution(problem=f"来源指定的买家号「{_show(name)}」（账号 {_names(named)}）与买家号 ID {x}"
                                          f"（账号 {hit['name']}）不是同一个账号；在来源里改正其中一个后会重新分配")
            return Resolution(account=hit, note=f"{note}（来源写的买家号「{_show(name)}」与账号名称不同）")
        archived = [a for a in gone if a.get("amazon_customer_id") == x]
        if archived:
            last = max(archived, key=lambda a: (a["archived_at"], a["id"]))
            return Resolution(problem=f"来源指定的买家号 ID {x} 对应的账号 {last['name']} 已归档；"
                                      "重新登记这个买家号，或改派到其他账号")
        if len(named) == 1:
            c = named[0]
            if not c.get("amazon_customer_id"):
                return Resolution(problem=f"来源指定的买家号 ID {x} 还没有登记；同名账号 {c['name']} 还没有填买家号 ID，"
                                          "填上后会自动分配")
            return Resolution(problem=f"账号 {c['name']} 登记的买家号 ID 是 {c['amazon_customer_id']}，"
                                      f"与来源指定的 {x} 不同；在来源里改正，或修改账号的买家号 ID")
        shown = f"（买家号「{_show(name)}」）" if name else " "
        tail = "（这个买家号 ID 已被忽略，恢复后才能登记）" if x in set(ignored_ids) else ""
        return Resolution(problem=f"来源指定的买家号 ID {x}{shown}还没有登记为账号；"
                                  f"在「账号与浏览器」登记后会自动分配{tail}")
    n = _show(name)
    note = f"按来源的买家号「{n}」"
    exact = [a for a in live if (a["name"] or "").strip() == name]
    if exact:
        return Resolution(account=exact[0], note=note)
    loose = _name_matches(name, live)
    if len(loose) == 1:
        return Resolution(account=loose[0], note=f"{note}（对应账号 {loose[0]['name']}）")
    if loose:
        return Resolution(problem=f"来源指定的买家号「{n}」对应多个账号（{_names(loose)}）；在来源里填买家号 ID")
    if _name_matches(name, gone):
        return Resolution(problem=f"来源指定的买家号「{n}」对应的账号已归档；在来源里改成其他买家号，或改派到其他账号")
    return Resolution(problem=f"来源指定的买家号「{n}」没有同名账号；新建同名账号、在来源里改正，或改派到其他账号")


def resolve_order(order_row: dict[str, Any], ctx: Context) -> Resolution:
    """输入：任务行、解析用的工作区数据 → 输出：任务上的指定解析到的 Resolution（没有指定 → 全为 None）。"""
    cid, name = of_order(order_row)
    return resolve(cid, name, ctx.accounts, ctx.ignored)


def load_context(conn, workspace_id: int) -> Context:
    """输入：连接、工作区 → 输出：Context（本工作区全部账号行，含已归档；忽略名单里的买家号 ID）。"""
    accounts = conn.execute("SELECT * FROM procure.buyer_accounts WHERE workspace_id = %s ORDER BY id",
                            (workspace_id,)).fetchall()
    ignored = conn.execute("SELECT customer_id FROM procure.ignored_buyer_ids WHERE workspace_id = %s",
                           (workspace_id,)).fetchall()
    return Context(accounts=tuple(accounts), ignored=frozenset(r["customer_id"] for r in ignored))


def target(conn, workspace_id: int, order_row: dict[str, Any], ctx: Context | None = None) -> Resolution:
    """输入：连接、工作区、任务行（可给现成的 Context）→ 输出：任务上的指定解析到的 Resolution。
    没有指定时不读库，直接给全为 None 的结果。"""
    if not has(order_row):
        return Resolution()
    return resolve_order(order_row, ctx if ctx is not None else load_context(conn, workspace_id))


def off_target(order_row: dict[str, Any], resolution: Resolution) -> bool:
    """输入：带分配的任务行、它的指定解析结果 → 输出：分配是否已不符合来源指定。

    任务有指定、分配不是人工改派（manual 不受来源约束），而解析结果不是当前分配的账号（含解析不出账号）→ True。
    orders._assignment_problem 与领取时复核共用这一个判断；分配第 0 步用同一套解析，所以被解除的任务只会再落到
    领取时认可的账号上。
    """
    if not has(order_row) or order_row.get("assignment_kind") == "manual":
        return False
    current = order_row.get("assigned_account_id")
    if current is None:
        return False
    return resolution.account is None or resolution.account["id"] != current


# ── 指定的账号此刻能不能接 ────────────────────────────────────────────────────


def _usd(v: Decimal | None) -> str:
    return "未知金额" if v is None else f"${v:.2f}"


def placement_problem(account: dict[str, Any], *, site_ok: bool, subtotal: Decimal | None, in_range: bool,
                      on_account: bool, cap: int, day: date) -> str | None:
    """输入：解析到的账号行、站点是否相符、商品小计、金额是否在账号区间内、任务当前是否就分配在这个账号上（非人工）、
    账号在计划日期的剩余额度 cap、计划日期 → 输出：账号接不了的原因（按下面的顺序取第一条不满足的）；能接 → None。

    站点不符 → 小计未知 → 金额不在区间 → 采购开关关闭（任务当前不在它上面时）→ 暂停执行（同前）→ 每日上限为 0 →
    额度已满。原因里不带会变的计数，重复分配得到同样的文字。
    """
    name = account["name"]
    if not site_ok:
        return f"任务收货国家与来源指定的账号 {name} 的站点不符"
    if subtotal is None:
        return f"商品小计未知（缺少采购单价或数量），不能分给来源指定的账号 {name}"
    if not in_range:
        mx = account.get("max_amount")
        rng = f"{_usd(account['min_amount'])}–{_usd(mx) if mx is not None else '不限'}"
        return f"金额 {_usd(subtotal)} 不在来源指定的账号 {name} 的区间 {rng} 内；调整账号的金额区间，或改派到其他账号"
    if not on_account:
        if not account["purchase_enabled"]:
            return f"来源指定的账号 {name} 已关闭采购开关，不接收新分配；打开开关后会自动分配"
        if account["execution_paused"]:
            return f"来源指定的账号 {name} 已暂停执行；恢复执行后会自动分配"
    if account["daily_limit"] <= 0:
        return f"来源指定的账号 {name} 的每日上限为 0"
    if cap <= 0:
        return (f"来源指定的账号 {name} 在 {day.isoformat()} 的额度已满（上限 {account['daily_limit']}）；"
                "有空位后的分配会再安排")
    return None


def out_of(order_row: dict[str, Any], resolution: Resolution | None) -> dict[str, Any] | None:
    """输入：任务行、它的指定解析结果（None = 没解析）→ 输出：接口里的 designated_buyer
    {customer_id, name, account: {id, name} | None}；任务没有指定 → None。"""
    if not has(order_row):
        return None
    acc = resolution.account if resolution is not None else None
    return {"customer_id": order_row.get("designated_customer_id"), "name": order_row.get("designated_buyer_name"),
            "account": {"id": acc["id"], "name": acc["name"]} if acc is not None else None}
