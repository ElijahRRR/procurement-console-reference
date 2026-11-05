"""飞书连接（契约 §6、§8、§10）：应用状态、链接解析、字段读取、样本预览、连接增改停用、写回验收、读取记录。

- 飞书客户端只经本模块的 make_client() 创建（测试 monkeypatch 它换成假客户端）。应用凭证只从 registry.settings 取；
  未配置时抛 FEISHU_NOT_CONFIGURED（409），文件导入不受影响。需要客户端的函数都接受可选的 client 参数，
  不传时用 make_client() 建一个并在用完后关闭。
- 飞书接口错误统一转成 DomainError('FEISHU_ERROR', 「动作 + 失败：原因（飞书错误码）」, 502)。
- 字段对应 field_map：{系统字段: 飞书列名 | [候选列名]}，必须对应「平台订单号」，所有列都必须在来源表里。
- 写回对应 writeback_map：{结果字段: 飞书列名}，列必须在结果表里且可写（公式、查找引用、关联等不可写）；
  建 / 改连接时把这些列的字段类型存进 writeback_field_types，入队时据此转换写回值。
- 结果表默认就是来源表；不同时，结果表里必须有平台订单号列（写回前按它定位记录）。
- 改结果表或写回列 → 写回验收作废（writeback_verified_at 置空）。改结果表时，旧目标上 pending 的写回标 failed，
  不改投新表（v1 §6.1）；只改写回列时，pending 行按新对应重建。
- 写回验收：在指定的测试记录上写测试值、读回比对、再恢复原值；一致才记 writeback_verified_at。
- **表格授权**（v1 §6.1 第 3 步、§6.3）：飞书应用凭证是全部署共用的，能读到某张表不等于这个工作区有权使用它。
  一个多维表格（app_token）同一时刻只授权给一个工作区（procure.feishu_base_grants）。解析链接、读字段、样本预览、
  建 / 改连接、立即读取、写回验收之前都检查授权：已授权给本工作区 → 放行；尚未授权、且本工作区是部署所有者的工作区
  （第一个工作区）→ 放行（建连接时登记授权）；其余一律 403 FORBIDDEN（不透露表格是否已归别的工作区）。
  其他工作区要用某个表格，由部署所有者在服务器上运行 ``python cli.py feishu_grant`` 授权（grant_base）。

每个查询都带工作区条件；别的工作区的连接一律 NOT_FOUND（授权登记表除外，它是跨工作区的）。本模块不 commit。
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from api.feishu import FeishuClient, FeishuError
from registry import settings
from services import clock, events, feishu_fields, normalize, writeback
from services.errors import DomainError, conflict, not_found, validation

NOT_CONFIGURED_MESSAGE = "未配置飞书应用：请在 backend/.env 设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET"
#: 预览时（还没有连接）给 SourceRef 用的连接 id：bigserial 从 1 开始，0 永远查不到来源关联；dry_run 不写库
PREVIEW_CONNECTION_ID = 0
#: 飞书读取的咨询锁命名空间（feishu_intake 用 pg_try_advisory_xact_lock；写回验收用阻塞版与之互斥）
INTAKE_LOCK_NS = 7_310_432
PREVIEW_LIMIT_MAX = 200

_FEISHU_HINTS: dict[Any, str] = {
    91402: "多维表格不存在或链接不对",
    91403: "应用没有这张多维表格的权限：请在多维表格右上角「…」→「更多」→「添加文档应用」里添加本应用",
    1254302: "应用没有这张多维表格的权限：请在多维表格右上角「…」→「更多」→「添加文档应用」里添加本应用，"
             "启用了高级权限的表还要给应用分配可读写的角色",
    1254040: "多维表格不存在或链接不对",
    1254041: "数据表不存在（可能已被删除）",
    1254042: "视图不存在（可能已被删除）",
    1254043: "记录不存在（可能已被删除）",
    1254045: "字段对应里的列在表里不存在（可能被改名或删除）",
    131005: "知识库节点不存在或应用无权访问",
    "NETWORK": "连不上飞书开放平台，请检查网络",
}

_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_TABLE_RE = re.compile(r"^tbl[A-Za-z0-9]{4,40}$")
_VIEW_RE = re.compile(r"^vew[A-Za-z0-9]{4,40}$")

_CONNECTION_FIELDS = ("name", "link", "app_token", "source_table_id", "source_view_id", "result_table_id",
                      "field_map", "admission", "writeback_map", "writeback_enabled", "poll_minutes", "enabled")


# ── 客户端与错误 ──────────────────────────────────────────────────────────────


def make_client() -> FeishuClient:
    """输入：无 → 输出：用 registry.settings 里的应用凭证建好的飞书客户端；未配置 → FEISHU_NOT_CONFIGURED（409）。

    这是本模块（以及写回队列、读取任务）获取客户端的唯一钩子，测试用 monkeypatch 替换成假客户端。
    """
    if not settings.get().feishu_configured:
        raise DomainError("FEISHU_NOT_CONFIGURED", NOT_CONFIGURED_MESSAGE, 409)
    try:
        return FeishuClient.from_settings()
    except FeishuError as exc:   # 只有凭证为空时构造函数会抛
        raise DomainError("FEISHU_NOT_CONFIGURED", NOT_CONFIGURED_MESSAGE, 409) from exc


@contextlib.contextmanager
def client_scope(client: Any | None) -> Iterator[Any]:
    """输入：调用方给的客户端或 None → 输出：上下文里可用的客户端（None 时用 make_client() 新建，退出时关闭）。"""
    if client is not None:
        yield client
        return
    own = make_client()
    try:
        yield own
    finally:
        own.close()


def feishu_error(exc: FeishuError, action: str) -> DomainError:
    """输入：飞书接口异常、动作（如「读取字段」）→ 输出：DomainError('FEISHU_ERROR', 「动作失败：原因（飞书错误码 X）」, 502)。"""
    if exc.code == "NOT_CONFIGURED":
        return DomainError("FEISHU_NOT_CONFIGURED", NOT_CONFIGURED_MESSAGE, 409)
    reason = _FEISHU_HINTS.get(exc.code) or exc.msg or "未知错误"
    return DomainError("FEISHU_ERROR", f"{action}失败：{reason}（飞书错误码 {exc.code}）", 502,
                       {"feishu_code": exc.code, "feishu_msg": exc.msg})


@contextlib.contextmanager
def feishu_guard(action: str) -> Iterator[None]:
    """输入：动作名 → 输出：上下文；其中抛出的 FeishuError 转成 feishu_error(exc, action)。"""
    try:
        yield
    except FeishuError as exc:
        raise feishu_error(exc, action) from exc


# ── 状态、系统字段、链接、字段 ────────────────────────────────────────────────────


def status() -> dict[str, Any]:
    """输入：无 → 输出：{configured}：服务端是否配置了飞书应用凭证。"""
    return {"configured": settings.get().feishu_configured}


def system_fields() -> dict[str, Any]:
    """输入：无 → 输出：{source_fields: [{key, label, required}], result_fields: [{key, label}]}（字段对应下拉用）。"""
    return {
        "source_fields": [{"key": f["key"], "label": f["label"], "required": f["required"]}
                          for f in normalize.SYSTEM_FIELDS],
        "result_fields": [{"key": f["key"], "label": f["label"]} for f in feishu_fields.RESULT_FIELDS],
    }


# ── 表格授权 ──────────────────────────────────────────────────────────────────

NO_ACCESS_MESSAGE = ("这个多维表格还没有授权给当前工作区使用。链接只用来定位表格，不等于授权；"
                     "请让部署管理员在服务器上运行 python cli.py feishu_grant 授权")
GRANT_SOURCES = ("owner_workspace", "cli", "migrated")


def _no_access() -> DomainError:
    return DomainError("FORBIDDEN", NO_ACCESS_MESSAGE, 403)


def owner_workspace_id(conn) -> int | None:
    """输入：连接 → 输出：部署所有者的工作区 id（第一个工作区，首次设置时创建）；还没有工作区 → None。"""
    row = conn.execute("SELECT min(id) AS id FROM procure.workspaces").fetchone()
    return row["id"] if row else None


def require_base_access(conn, workspace_id: int, app_token: str, *, claim: bool = False) -> None:
    """输入：工作区、多维表格 app_token、是否在放行时登记授权 → 输出：无；无权使用 → 403 FORBIDDEN。

    已授权给本工作区 → 放行；已授权给别的工作区 → 拒绝；尚未授权：本工作区是部署所有者的工作区 → 放行
    （claim=True 时登记为 owner_workspace 授权），否则拒绝。拒绝时的提示不区分「没人授权」与「归别人」。
    """
    if not isinstance(app_token, str) or not app_token:
        raise _no_access()
    g = conn.execute("SELECT workspace_id FROM procure.feishu_base_grants WHERE app_token = %s",
                     (app_token,)).fetchone()
    if g is not None:
        if g["workspace_id"] != workspace_id:
            raise _no_access()
        return
    if workspace_id != owner_workspace_id(conn):
        raise _no_access()
    if claim:
        conn.execute("INSERT INTO procure.feishu_base_grants (app_token, workspace_id, granted_by, granted_at) "
                     "VALUES (%s, %s, 'owner_workspace', %s) ON CONFLICT (app_token) DO NOTHING",
                     (app_token, workspace_id, clock.now()))
        # 并发的另一个工作区刚登记了同一个表格：以库里为准
        require_base_access(conn, workspace_id, app_token, claim=False)


def grant_base(conn, workspace_id: int, app_token: str, *, transfer: bool = False,
               granted_by: str = "cli") -> dict[str, Any]:
    """输入：工作区、多维表格 app_token、是否从别的工作区转过来 → 输出：{app_token, workspace_id, granted_by,
    previous_workspace_id}。部署所有者在服务器上用 CLI 授权（不经控制台）。

    工作区不存在 → NOT_FOUND；表格已授权给别的工作区且没有 transfer → VALIDATION（转授后原工作区在这个表格上的
    连接被停用，读取、验收、写回发送都会被拒绝）。一个表格同一时刻只归一个工作区：授权后**其他所有工作区**在这个
    表格上仍启用的连接一律停用（含原授权工作区、表格尚未登记授权时部署所有者工作区的连接、迁移遗留的同表连接），
    每个受影响的工作区记一条 'feishu.base_revoked' 事件，disabled_connections 列出全部被停用的连接。
    """
    if not isinstance(app_token, str) or not _TOKEN_RE.match(app_token):
        raise validation("多维表格标识（app_token）格式不对", field="app_token")
    if granted_by not in GRANT_SOURCES:
        raise validation(f"未知的授权来源：{granted_by}")
    ws = conn.execute("SELECT id FROM procure.workspaces WHERE id = %s", (workspace_id,)).fetchone()
    if ws is None:
        raise not_found("工作区")
    cur = conn.execute("SELECT workspace_id FROM procure.feishu_base_grants WHERE app_token = %s FOR UPDATE",
                       (app_token,)).fetchone()
    prev = cur["workspace_id"] if cur else None
    if prev is not None and prev != workspace_id and not transfer:
        raise validation(f"这个多维表格已授权给工作区 {prev}；确需转给工作区 {workspace_id} 请加 -p transfer=1",
                         field="app_token")
    conn.execute(
        "INSERT INTO procure.feishu_base_grants (app_token, workspace_id, granted_by, granted_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (app_token) DO UPDATE SET workspace_id = EXCLUDED.workspace_id, granted_by = EXCLUDED.granted_by, "
        "granted_at = EXCLUDED.granted_at",
        (app_token, workspace_id, granted_by, clock.now()))
    events.record(conn, workspace_id, "settings", workspace_id, "feishu.base_granted", actor_kind="system",
                  detail={"app_token": app_token, "previous_workspace_id": prev, "granted_by": granted_by})
    disabled: list[int] = []
    affected = {r["workspace_id"] for r in conn.execute(
        "SELECT DISTINCT workspace_id FROM procure.feishu_connections WHERE app_token = %s AND enabled "
        "AND workspace_id <> %s", (app_token, workspace_id)).fetchall()}
    if prev is not None and prev != workspace_id:
        affected.add(prev)
    for other in sorted(affected):
        ids = _disable_base_connections(conn, other, app_token)
        disabled.extend(ids)
        events.record(conn, other, "settings", other, "feishu.base_revoked", actor_kind="system",
                      detail={"app_token": app_token, "reason": "转授给其他工作区", "disabled_connections": ids})
    return {"app_token": app_token, "workspace_id": workspace_id, "granted_by": granted_by,
            "previous_workspace_id": prev, "disabled_connections": disabled}


def _disable_base_connections(conn, workspace_id: int, app_token: str) -> list[int]:
    """输入：工作区、多维表格 → 输出：被停用的连接 id（授权撤销 / 转走后，原工作区的连接不再定时读取）。"""
    rows = conn.execute(
        "UPDATE procure.feishu_connections SET enabled = false, updated_at = %s "
        "WHERE workspace_id = %s AND app_token = %s AND enabled RETURNING id",
        (clock.now(), workspace_id, app_token),
    ).fetchall()
    for r in rows:
        events.record(conn, workspace_id, "connection", r["id"], "connection.disabled", actor_kind="system",
                      detail={"reason": "多维表格的授权已撤销或转给其他工作区"})
    return [r["id"] for r in rows]


def revoke_base(conn, app_token: str) -> dict[str, Any]:
    """输入：多维表格 app_token → 输出：{app_token, workspace_id | None, disabled_connections}（撤销授权；
    该工作区在这个表格上的连接随即停用，重新授权后可再启用）。"""
    row = conn.execute("DELETE FROM procure.feishu_base_grants WHERE app_token = %s RETURNING workspace_id",
                       (app_token,)).fetchone()
    disabled: list[int] = []
    if row is not None:
        disabled = _disable_base_connections(conn, row["workspace_id"], app_token)
        events.record(conn, row["workspace_id"], "settings", row["workspace_id"], "feishu.base_revoked",
                      actor_kind="system", detail={"app_token": app_token, "reason": "部署管理员撤销",
                                                   "disabled_connections": disabled})
    return {"app_token": app_token, "workspace_id": row["workspace_id"] if row else None,
            "disabled_connections": disabled}


def resolve_link(conn, workspace_id: int, link: str, client: Any | None = None) -> dict[str, Any]:
    """输入：工作区、用户粘贴的多维表格 / 知识库链接、可选客户端 → 输出：{app_token, table_id, view_id,
    tables: [{table_id, name}]}。

    知识库链接先换出实际多维表格的 app_token（节点不是多维表格 → VALIDATION）。列出数据表之前检查表格授权
    （见模块说明，无权 → FORBIDDEN）。链接里没有 table= 且只有一张数据表时取这张表；链接里的数据表不在列表里 → VALIDATION。
    """
    parsed = feishu_fields.parse_link(link)
    if parsed["kind"] != "wiki":
        require_base_access(conn, workspace_id, parsed["token"])
    with client_scope(client) as c, feishu_guard("解析飞书链接"):
        app_token = parsed["token"]
        if parsed["kind"] == "wiki":
            node = c.wiki_get_node(parsed["token"])
            if node.get("obj_type") != "bitable" or not node.get("obj_token"):
                raise validation("这个知识库页面不是多维表格")
            app_token = node["obj_token"]
            require_base_access(conn, workspace_id, app_token)
        tables = c.list_tables(app_token)
    table_id = parsed["table_id"]
    ids = [t["table_id"] for t in tables]
    if table_id is not None and table_id not in ids:
        raise validation("链接里的数据表不在这个多维表格里，或应用没有它的权限")
    if table_id is None and len(tables) == 1:
        table_id = tables[0]["table_id"]
    return {"app_token": app_token, "table_id": table_id, "view_id": parsed["view_id"], "tables": tables}


def _field_out(f: dict[str, Any]) -> dict[str, Any]:
    t = f.get("type")
    out = {"field_id": f.get("field_id"), "field_name": f.get("field_name"), "type": t, "ui_type": f.get("ui_type"),
           "is_primary": bool(f.get("is_primary")), "writable": feishu_fields.writable(f),
           "type_zh": feishu_fields.FIELD_TYPE_ZH.get(t, "其他") if isinstance(t, int) else "其他"}
    opts = (f.get("property") or {}).get("options") if isinstance(f.get("property"), dict) else None
    if opts:
        out["options"] = [o.get("name") for o in opts if o.get("name")]
    return out


def _list_fields(c: Any, app_token: str, table_id: str, action: str = "读取字段") -> list[dict[str, Any]]:
    with feishu_guard(action):
        return c.list_fields(app_token, table_id)


def fields(conn, workspace_id: int, app_token: str, table_id: str, client: Any | None = None) -> dict[str, Any]:
    """输入：工作区、app_token、table_id、可选客户端 → 输出：{fields: [{field_id, field_name, type, ui_type, is_primary,
    writable, type_zh, options?}], suggested_field_map, suggested_writeback_map}。无权使用该表格 → FORBIDDEN。"""
    _check_ids(app_token, table_id)
    require_base_access(conn, workspace_id, app_token)
    with client_scope(client) as c:
        raw = _list_fields(c, app_token, table_id)
    names = [f["field_name"] for f in raw if f.get("field_name")]
    return {"fields": [_field_out(f) for f in raw],
            "suggested_field_map": feishu_fields.suggest_field_map(names),
            "suggested_writeback_map": feishu_fields.suggest_writeback_map(raw)}


def _check_ids(app_token: Any, table_id: Any) -> None:
    if not isinstance(app_token, str) or not _TOKEN_RE.match(app_token):
        raise validation("多维表格标识（app_token）格式不对", field="app_token")
    if not isinstance(table_id, str) or not _TABLE_RE.match(table_id):
        raise validation("数据表标识（table_id）格式不对", field="table_id")


# ── 字段对应 / 准入 / 写回对应的校验 ─────────────────────────────────────────────


def _columns(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]
    raise validation("字段对应的值必须是列名或列名列表")


def clean_field_map(field_map: Any, table_columns: set[str] | None = None) -> dict[str, Any]:
    """输入：{系统字段: 列名 | [候选列名]}、来源表的全部列名（None = 不核对）→ 输出：去空白、去空项后的 field_map。

    未知系统字段、没有对应平台订单号、列名不在表里 → VALIDATION。
    """
    if not isinstance(field_map, dict):
        raise validation("字段对应格式不对", field="field_map")
    out: dict[str, Any] = {}
    missing: list[str] = []
    for key, val in field_map.items():
        if key not in normalize.SYSTEM_FIELD_KEYS:
            raise validation(f"字段对应里有未知的系统字段：{key}", field="field_map")
        cols = _columns(val)
        if not cols:
            continue
        if table_columns is not None:
            missing.extend(c for c in cols if c not in table_columns and c not in missing)
        out[key] = cols[0] if len(cols) == 1 else cols
    if "upstream_order_no" not in out:
        raise validation("请为「平台订单号」选择一列", field="field_map")
    if missing:
        raise validation(f"来源表里没有这些列：{'、'.join(missing)}", field="field_map", columns=missing)
    return out


def clean_admission(admission: Any) -> dict[str, Any]:
    """输入：准入规则（可缺键）→ 输出：与 normalize.DEFAULT_ADMISSION 合并后的完整规则；未知键、取值不合格 → VALIDATION。"""
    return normalize.effective_admission({**normalize.DEFAULT_ADMISSION, **normalize.check_admission(admission)})


def clean_writeback_map(writeback_map: Any, result_fields: list[dict[str, Any]] | None) -> tuple[dict[str, str],
                                                                                                   dict[str, int]]:
    """输入：{结果字段: 列名}、结果表的字段定义（None = 不核对）→ 输出：(去空项后的写回对应, {列名: 字段类型})。

    未知结果字段、列不在结果表、列不可写、同一列对应多个结果字段 → VALIDATION。
    """
    if writeback_map is None:
        return {}, {}
    if not isinstance(writeback_map, dict):
        raise validation("写回对应格式不对", field="writeback_map")
    by_name = {f["field_name"]: f for f in (result_fields or []) if f.get("field_name")}
    out: dict[str, str] = {}
    types: dict[str, int] = {}
    used: dict[str, str] = {}
    for key, col in writeback_map.items():
        if key not in feishu_fields.RESULT_FIELD_KEYS:
            raise validation(f"写回对应里有未知的结果字段：{key}", field="writeback_map")
        if col is None or (isinstance(col, str) and not col.strip()):
            continue
        if not isinstance(col, str):
            raise validation("写回对应的值必须是列名", field="writeback_map")
        col = col.strip()
        if col in used:
            raise validation(f"「{col}」同时对应了两个结果字段", field="writeback_map")
        if result_fields is not None:
            f = by_name.get(col)
            if f is None:
                raise validation(f"结果表里没有「{col}」这一列", field="writeback_map", column=col)
            if not feishu_fields.writable(f):
                tz = feishu_fields.FIELD_TYPE_ZH.get(f.get("type"), "系统")
                raise validation(f"「{col}」是{tz}列，不能作为写回目标", field="writeback_map", column=col)
            types[col] = int(f["type"])
        used[col] = key
        out[key] = col
    return out, types


def _text(v: Any, label: str, field: str, max_len: int, *, required: bool = False) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise validation(f"请填写{label}", field=field)
        return None
    if not isinstance(v, str):
        raise validation(f"{label}必须是文本", field=field)
    v = v.strip()
    if len(v) > max_len:
        raise validation(f"{label}不能超过 {max_len} 个字", field=field)
    return v


def _poll(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise validation("读取间隔必须是整数分钟", field="poll_minutes") from None
    if isinstance(v, bool) or not 1 <= n <= 1440:
        raise validation("读取间隔须在 1 到 1440 分钟之间", field="poll_minutes")
    return n


def _bool(v: Any, label: str, field: str) -> bool:
    if not isinstance(v, bool):
        raise validation(f"{label}只能是是或否", field=field)
    return v


# ── 连接的读写 ────────────────────────────────────────────────────────────────


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def writeback_state(row: dict[str, Any]) -> str:
    """输入：连接行 → 输出：'disabled'（停用 / 未开启写回 / 写回对应为空）、'unverified'（回填待验证）或 'active'。"""
    if not (row["enabled"] and row["writeback_enabled"] and row["writeback_map"]):
        return "disabled"
    return "active" if row["writeback_verified_at"] is not None else "unverified"


def _out(row: dict[str, Any], counts: dict[str, int] | None = None) -> dict[str, Any]:
    """输入：连接行、写回计数 → 输出：契约 §10「飞书连接」形状（另附 writeback_counts）。"""
    return {
        "id": row["id"], "name": row["name"], "link": row["link"], "app_token": row["app_token"],
        "source_table_id": row["source_table_id"], "source_view_id": row["source_view_id"],
        "result_table_id": row["result_table_id"], "field_map": row["field_map"], "admission": row["admission"],
        "writeback_map": row["writeback_map"], "writeback_enabled": row["writeback_enabled"],
        "writeback_verified_at": _iso(row["writeback_verified_at"]), "writeback_state": writeback_state(row),
        "writeback_test_record": row["writeback_test_record"], "poll_minutes": row["poll_minutes"],
        "enabled": row["enabled"], "last_sync_at": _iso(row["last_sync_at"]),
        "last_sync_ok_at": _iso(row["last_sync_ok_at"]), "last_error": row["last_error"],
        "last_stats": row["last_stats"], "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
        "writeback_counts": counts or {"pending": 0, "failed": 0, "done": 0},
    }


def get_row(conn, workspace_id: int, connection_id: int, for_update: bool = False) -> dict[str, Any]:
    """输入：工作区、连接 id、是否加行锁 → 输出：feishu_connections 行；不存在或属于别的工作区 → NOT_FOUND。"""
    try:
        cid = int(connection_id)
    except (TypeError, ValueError):
        raise not_found("飞书连接") from None
    row = conn.execute(
        "SELECT * FROM procure.feishu_connections WHERE id = %s AND workspace_id = %s"
        + (" FOR UPDATE" if for_update else ""),
        (cid, workspace_id),
    ).fetchone()
    if row is None:
        raise not_found("飞书连接")
    return row


def get(conn, workspace_id: int, connection_id: int) -> dict[str, Any]:
    """输入：工作区、连接 id → 输出：连接形状；跨工作区 → NOT_FOUND。"""
    row = get_row(conn, workspace_id, connection_id)
    return _out(row, writeback.counts_by_connection(conn, workspace_id).get(row["id"]))


def list_connections(conn, workspace_id: int) -> list[dict[str, Any]]:
    """输入：工作区 → 输出：该工作区的全部连接（启用的在前，再按 id）。"""
    counts = writeback.counts_by_connection(conn, workspace_id)
    rows = conn.execute(
        "SELECT * FROM procure.feishu_connections WHERE workspace_id = %s ORDER BY enabled DESC, id",
        (workspace_id,),
    ).fetchall()
    return [_out(r, counts.get(r["id"])) for r in rows]


def find_by_table(conn, workspace_id: int, app_token: str, table_id: str) -> dict[str, Any] | None:
    """输入：工作区、app_token、来源表 → 输出：已有的连接行（含停用的），没有 → None。"""
    return conn.execute(
        "SELECT * FROM procure.feishu_connections WHERE workspace_id = %s AND app_token = %s AND source_table_id = %s",
        (workspace_id, app_token, table_id),
    ).fetchone()


def _actor(actor_user_id: int | None) -> tuple[str, int | None]:
    return ("user", actor_user_id) if actor_user_id is not None else ("system", None)


def _key_column_in(field_map: dict[str, Any], columns: set[str]) -> bool:
    return any(c in columns for c in _columns(field_map.get("upstream_order_no")))


def create(conn, workspace_id: int, data: dict[str, Any], actor_user_id: int | None,
           client: Any | None = None) -> dict[str, Any]:
    """输入：工作区、{name, link, app_token?, source_table_id?, source_view_id?, result_table_id?, field_map,
    admission?, writeback_map?, writeback_enabled?, poll_minutes?, enabled?}、操作人、可选客户端 → 输出：连接形状。

    app_token / source_table_id 缺省时从链接解析（知识库链接经飞书换出 app_token）。会向飞书读取来源表与结果表的字段，
    核对字段对应与写回对应（见模块说明）。准入规则与缺省值合并后保存。writeback_enabled 缺省 = 写回对应非空
    （未验收前写回只积压不发送）。表格未授权给本工作区 → FORBIDDEN（部署所有者的工作区首次绑定时登记授权）。
    同一工作区同一张来源表只能有一个连接（含已停用的）→ CONFLICT，detail 带已有连接 id。
    """
    if not isinstance(data, dict):
        raise validation("连接参数格式不对")
    unknown = set(data) - set(_CONNECTION_FIELDS) - {"table_id", "view_id"}
    if unknown:
        raise validation(f"未知的连接参数：{'、'.join(sorted(unknown))}")
    link = _text(data.get("link"), "飞书链接", "link", 2000, required=True)
    parsed = feishu_fields.parse_link(link)
    name = _text(data.get("name"), "连接名称", "name", 100) or "飞书订单表"
    app_token = data.get("app_token") or (parsed["token"] if parsed["kind"] == "base" else None)
    table_id = data.get("source_table_id") or data.get("table_id") or parsed["table_id"]
    view_id = data.get("source_view_id") if "source_view_id" in data else data.get("view_id", parsed["view_id"])
    view_id = _text(view_id, "视图", "source_view_id", 64)
    if view_id is not None and not _VIEW_RE.match(view_id):
        raise validation("视图标识格式不对", field="source_view_id")
    poll = _poll(data.get("poll_minutes", 10))
    enabled = _bool(data.get("enabled", True), "启用", "enabled")
    admission = clean_admission(data.get("admission"))

    with client_scope(client) as c:
        if app_token is None:
            resolved = resolve_link(conn, workspace_id, link, c)
            app_token, table_id = resolved["app_token"], table_id or resolved["table_id"]
        if table_id is None:
            raise validation("请选择订单所在的数据表", field="source_table_id")
        _check_ids(app_token, table_id)
        result_table = data.get("result_table_id") or table_id
        _check_ids(app_token, result_table)
        require_base_access(conn, workspace_id, app_token, claim=True)
        existing = find_by_table(conn, workspace_id, app_token, table_id)
        if existing is not None:
            raise conflict("这张数据表已经连接过了" + ("" if existing["enabled"] else "（已停用，可重新启用）"),
                           connection_id=existing["id"], enabled=existing["enabled"])
        source_fields = _list_fields(c, app_token, table_id, "读取来源表字段")
        source_cols = {f["field_name"] for f in source_fields if f.get("field_name")}
        field_map = clean_field_map(data.get("field_map"), source_cols)
        if result_table == table_id:
            result_fields = source_fields
        else:
            result_fields = _list_fields(c, app_token, result_table, "读取结果表字段")
            if not _key_column_in(field_map, {f["field_name"] for f in result_fields if f.get("field_name")}):
                raise validation("结果表里没有与来源表相同的平台订单号列，无法定位要写回的记录",
                                 field="result_table_id")
        wb_map, wb_types = clean_writeback_map(data.get("writeback_map"), result_fields)
    wb_enabled = _bool(data.get("writeback_enabled", bool(wb_map)), "开启写回", "writeback_enabled")

    now = clock.now()
    try:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO procure.feishu_connections
                    (workspace_id, name, link, app_token, source_table_id, source_view_id, result_table_id,
                     field_map, admission, writeback_map, writeback_field_types, writeback_enabled, poll_minutes,
                     enabled, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (workspace_id, name, link, app_token, table_id, view_id, result_table, Jsonb(field_map),
                 Jsonb(admission), Jsonb(wb_map), Jsonb(wb_types), wb_enabled, poll, enabled, actor_user_id, now, now),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        existing = find_by_table(conn, workspace_id, app_token, table_id)
        raise conflict("这张数据表已经连接过了", connection_id=existing["id"] if existing else None) from None
    ak, aid = _actor(actor_user_id)
    events.record(conn, workspace_id, "connection", row["id"], "connection.created", actor_kind=ak, actor_id=aid,
                  detail={"name": name, "source_table_id": table_id, "result_table_id": result_table,
                          "writeback_columns": sorted(wb_map.values())})
    return _out(row)


def update(conn, workspace_id: int, connection_id: int, patch: dict[str, Any], actor_user_id: int | None,
           client: Any | None = None) -> dict[str, Any]:
    """输入：工作区、连接 id、要改的项（name、link、source_view_id、result_table_id、field_map、admission、writeback_map、
    writeback_enabled、poll_minutes、enabled）、操作人、可选客户端 → 输出：连接形状。

    app_token / source_table_id 不能改（换表请新建连接）。改字段对应、结果表或写回列时向飞书读取字段核对。
    改结果表或写回列 → 写回验收作废；改结果表 → 旧目标 pending 写回标 failed（不改投新表）；
    只改写回列 → pending 写回按新对应重建。
    """
    if not isinstance(patch, dict):
        raise validation("修改参数格式不对")
    row = get_row(conn, workspace_id, connection_id, for_update=True)
    unknown = set(patch) - set(_CONNECTION_FIELDS)
    if unknown:
        raise validation(f"未知的连接参数：{'、'.join(sorted(unknown))}")
    for k in ("app_token", "source_table_id"):
        if k in patch and patch[k] not in (None, row[k]):
            raise validation("连接的多维表格和来源数据表不能修改；换表请新建连接", field=k)
    upd: dict[str, Any] = {}
    if "name" in patch:
        upd["name"] = _text(patch["name"], "连接名称", "name", 100, required=True)
    if "link" in patch:
        upd["link"] = _text(patch["link"], "飞书链接", "link", 2000, required=True)
        feishu_fields.parse_link(upd["link"])
    if "source_view_id" in patch:
        v = _text(patch["source_view_id"], "视图", "source_view_id", 64)
        if v is not None and not _VIEW_RE.match(v):
            raise validation("视图标识格式不对", field="source_view_id")
        upd["source_view_id"] = v
    if "poll_minutes" in patch:
        upd["poll_minutes"] = _poll(patch["poll_minutes"])
    if "enabled" in patch:
        upd["enabled"] = _bool(patch["enabled"], "启用", "enabled")
    if "writeback_enabled" in patch:
        upd["writeback_enabled"] = _bool(patch["writeback_enabled"], "开启写回", "writeback_enabled")
    if "admission" in patch:
        upd["admission"] = clean_admission(patch["admission"])

    new_result = patch.get("result_table_id") or row["result_table_id"]
    result_changed = new_result != row["result_table_id"]
    map_given = "writeback_map" in patch
    field_map_given = "field_map" in patch
    if result_changed or map_given or field_map_given or upd.get("enabled") is True:
        # 部署所有者的工作区（重新）启用、改对应时登记授权：之后别的工作区要用这个表格须显式转授，
        # 转授会停用这里的连接（写回发送也按授权把关，见 writeback）
        require_base_access(conn, workspace_id, row["app_token"], claim=True)
    if result_changed or map_given or field_map_given:
        _check_ids(row["app_token"], new_result)
        with client_scope(client) as c:
            source_fields = _list_fields(c, row["app_token"], row["source_table_id"], "读取来源表字段")
            source_cols = {f["field_name"] for f in source_fields if f.get("field_name")}
            field_map = clean_field_map(patch["field_map"] if field_map_given else row["field_map"],
                                        source_cols if field_map_given else None)
            if field_map_given:
                upd["field_map"] = field_map
            if new_result == row["source_table_id"]:
                result_fields = source_fields
            else:
                result_fields = _list_fields(c, row["app_token"], new_result, "读取结果表字段")
                if not _key_column_in(field_map, {f["field_name"] for f in result_fields if f.get("field_name")}):
                    raise validation("结果表里没有与来源表相同的平台订单号列，无法定位要写回的记录",
                                     field="result_table_id")
            wb_map, wb_types = clean_writeback_map(patch["writeback_map"] if map_given else row["writeback_map"],
                                                   result_fields)
        map_changed = wb_map != (row["writeback_map"] or {})
        if result_changed:
            upd["result_table_id"] = new_result
        if map_given or result_changed:
            upd["writeback_map"] = wb_map
            upd["writeback_field_types"] = wb_types
        if result_changed or map_changed:
            upd["writeback_verified_at"] = None
        if result_changed:
            upd["writeback_test_record"] = None
    else:
        map_changed = False

    changed = sorted(k for k in upd if k != "writeback_field_types" and upd[k] != row[k])
    if not upd:
        return get(conn, workspace_id, row["id"])
    sets = ", ".join(f"{k} = %s" for k in upd)
    params = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in upd.values()]
    conn.execute(f"UPDATE procure.feishu_connections SET {sets}, updated_at = %s WHERE id = %s AND workspace_id = %s",
                 [*params, clock.now(), row["id"], workspace_id])
    detail: dict[str, Any] = {"changed": changed}
    if result_changed:
        detail["failed_pending_writebacks"] = writeback.fail_pending_for_target(
            conn, workspace_id, row["id"], row["result_table_id"],
            "连接的结果表已改绑：旧目标的待回填不会改投新表，请到原结果表核对后人工处理")
        detail["old_result_table_id"] = row["result_table_id"]
    elif map_changed:
        detail["rebuilt_pending_writebacks"] = writeback.rebuild_pending(conn, workspace_id, row["id"])
    if map_changed or upd.get("writeback_enabled") is True or upd.get("enabled") is True:
        # 停用 / 关写回 / 没设写回列期间登记的真实采购结果：补入写回队列（已入过队的版本不重复）
        detail["backfilled_writebacks"] = writeback.backfill_connection(conn, workspace_id, row["id"])
    ak, aid = _actor(actor_user_id)
    events.record(conn, workspace_id, "connection", row["id"], "connection.updated", actor_kind=ak, actor_id=aid,
                  detail=detail)
    return get(conn, workspace_id, row["id"])


def disable(conn, workspace_id: int, connection_id: int, actor_user_id: int | None) -> dict[str, Any]:
    """输入：工作区、连接 id、操作人 → 输出：连接形状（enabled=false：不再定时读取，写回只积压——停用期间登记的
    真实采购结果照常入队，只是不发送；可再 PATCH 启用）。"""
    row = get_row(conn, workspace_id, connection_id, for_update=True)
    if row["enabled"]:
        conn.execute("UPDATE procure.feishu_connections SET enabled = false, updated_at = %s WHERE id = %s "
                     "AND workspace_id = %s", (clock.now(), row["id"], workspace_id))
        ak, aid = _actor(actor_user_id)
        events.record(conn, workspace_id, "connection", row["id"], "connection.disabled", actor_kind=ak, actor_id=aid)
    return get(conn, workspace_id, row["id"])


def list_runs(conn, workspace_id: int, connection_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """输入：工作区、连接 id、条数 → 输出：最近的读取记录 [{id, trigger, started_at, finished_at, ok, stats, error}]。"""
    get_row(conn, workspace_id, connection_id)
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise validation("条数必须是整数", field="limit") from None
    rows = conn.execute(
        "SELECT * FROM procure.feishu_sync_runs WHERE workspace_id = %s AND connection_id = %s "
        "ORDER BY started_at DESC, id DESC LIMIT %s",
        (workspace_id, int(connection_id), limit),
    ).fetchall()
    return [{"id": r["id"], "connection_id": r["connection_id"], "trigger": r["trigger"],
             "started_at": _iso(r["started_at"]), "finished_at": _iso(r["finished_at"]), "ok": r["ok"],
             "stats": r["stats"], "error": r["error"]} for r in rows]


# ── 样本预览（只读）─────────────────────────────────────────────────────────────


def read_fields_for(c: Any, app_token: str, table_id: str, field_map: dict[str, Any]) -> tuple[dict[str, int],
                                                                                                list[str]]:
    """输入：客户端、来源表、字段对应 → 输出：({列名: 类型}, 要读的列)；平台订单号列不在表里 → VALIDATION。"""
    raw = _list_fields(c, app_token, table_id, "读取来源表字段")
    types = feishu_fields.field_types_of(raw)
    if not _key_column_in(field_map, set(types)):
        raise validation("字段对应里的平台订单号列在来源表里不存在（可能被改名或删除）", field="field_map")
    cols = [col for col in feishu_fields.used_columns(field_map) if col in types]
    return types, cols


def new_record_skip_reason(record: Any, admission: dict[str, Any]) -> str | None:
    """输入：规范化后的飞书记录、准入规则 → 输出：不为它**新建**任务的原因；可以新建 → None。

    在 normalize.admission_skip_reason（采购方、上游取消、历史单号、已拍单）之外，飞书来源只接收人审「通过」的行
    （准入 = 人审通过，方案 v2 §3）：未审或未通过的新行不建任务，通过后下次读取自动接收。已有任务的同键记录不受此限，
    照常作为更新交给 orders.ingest（人审撤回、取消等都要传达）。带可信 AMZ 订单号的历史行不看人审（格式不对的单号不算）。
    """
    reason = normalize.admission_skip_reason(record, admission)
    if reason:
        return reason
    if not normalize.amazon_order_no_ok(record.external_amz_order_no) and record.review != "approved":
        if record.review == "rejected":
            return f"人审结论为「{record.review_value}」，不新建采购"
        if record.review_value:
            return f"人审结论为「{record.review_value}」，尚未通过，暂不接收；通过后下次读取自动接收"
        return "尚未人审（人工审核为空），暂不接收；人审通过后下次读取自动接收"
    return None


def preview(conn, workspace_id: int, *, app_token: str, table_id: str, view_id: str | None = None,
            field_map: dict[str, Any], admission: dict[str, Any] | None = None, limit: int = 20,
            client: Any | None = None) -> dict[str, Any]:
    """输入：工作区、来源表定位、字段对应、准入规则、样本条数（≤200）、可选客户端 →
    输出：{stats: {read, new, update, unchanged, conflict, historical, skipped, invalid, pending_review, needs_info},
    records: [{record_id, upstream_order_no, verdict, reason, record, issues}]}。**不写库，也不写飞书。**

    结论与定时读取（feishu_intake）一致：缺平台订单号或样本内同键重复 → invalid；库里还没有的单按准入规则可能 skipped；
    其余交给 orders.ingest(dry_run=True)。已有同表连接时用它的连接 id，因而能认出「这条记录上次读过」。
    """
    _check_ids(app_token, table_id)
    require_base_access(conn, workspace_id, app_token)
    fm = clean_field_map(field_map)
    adm = clean_admission(admission)
    try:
        limit = max(1, min(int(limit), PREVIEW_LIMIT_MAX))
    except (TypeError, ValueError):
        raise validation("样本条数必须是整数", field="limit") from None
    existing = find_by_table(conn, workspace_id, app_token, table_id)
    cid = existing["id"] if existing is not None else PREVIEW_CONNECTION_ID
    with client_scope(client) as c:
        types, cols = read_fields_for(c, app_token, table_id, fm)
        records: list[dict[str, Any]] = []
        with feishu_guard("读取样本记录"):
            for rec in c.search_records(app_token, table_id, field_names=cols, view_id=view_id,
                                        page_size=min(limit, 500)):
                records.append(rec)
                if len(records) >= limit:
                    break
    from services import feishu_intake  # 延迟导入：intake 也导入本模块

    skip = {existing["writeback_test_record"]} if existing is not None and existing["writeback_test_record"] else set()
    results = feishu_intake.evaluate_records(conn, workspace_id, cid, records, fm, types, adm, dry_run=True,
                                             skip_record_ids=skip)
    stats = feishu_intake.empty_stats()
    stats.update(pending_review=0, needs_info=0)
    out = []
    for r in results:
        stats["read"] += 1
        stats[r["verdict"]] += 1
        if r.get("status") in ("pending_review", "needs_info") and r["verdict"] != "invalid":
            stats[r["status"]] += 1
        out.append({"record_id": r["record_id"], "upstream_order_no": r["upstream_order_no"] or None,
                    "verdict": r["verdict"], "reason": r["reason"], "record": r["record"], "issues": r["issues"],
                    "status": r.get("status")})
    return {"stats": stats, "records": out}


# ── 写回验收 ──────────────────────────────────────────────────────────────────

#: 验收写入的测试值（按结果字段）。数字写 0；采购状态写真实回填会写的「已拍单」，证明这个取值能写进去。
def _test_result(stamp: str) -> dict[str, Any]:
    text = f"回填验收 {stamp}"
    return {"buyer_account": text, "buyer_customer_id": text, "amazon_order_no": text, "purchase_status": "succeeded",
            "total_before_tax": Decimal("0.00"), "shipping": Decimal("0.00"), "tax": Decimal("0.00"),
            "total": Decimal("0.00"), "card_last4": "0000"}


def _comparable(v: Any, ftype: int | None) -> Any:
    """读回值 / 写入值 → 便于比较的形式（文本去空白、数字转 Decimal、列表转元组）。"""
    v = feishu_fields.cell_to_python(v, ftype)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        try:
            return Decimal(str(v)).normalize()
        except InvalidOperation:
            return str(v)
    if isinstance(v, datetime):
        return int(v.timestamp() * 1000)
    if isinstance(v, list):
        return tuple(str(x).strip() for x in v)
    return str(v).strip()


def _restore_value(raw: Any, ftype: int | None) -> Any:
    """原记录的读取值 → 写回去时用的写入值（None = 清空该列）。"""
    if raw is None:
        return None
    t = int(ftype) if ftype is not None else None
    if t == feishu_fields.URL and isinstance(raw, dict):
        return {"text": raw.get("text") or raw.get("link"), "link": raw.get("link") or raw.get("text")}
    v = feishu_fields.cell_to_python(raw, t)
    if v is None:
        return None
    if t == feishu_fields.NUMBER:
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if t == feishu_fields.DATETIME:
        return int(v.timestamp() * 1000) if isinstance(v, datetime) else v
    if t == feishu_fields.CHECKBOX:
        return bool(v)
    if t == feishu_fields.MULTI_SELECT:
        return [str(x) for x in v] if isinstance(v, list) else [str(v)]
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def verify_writeback(conn, workspace_id: int, connection_id: int, record_id: str, actor_user_id: int | None,
                     client: Any | None = None) -> dict[str, Any]:
    """输入：工作区、连接 id、结果表里的测试记录 id、操作人、可选客户端 →
    输出：{ok, mismatches: [{result_field, column, expected, actual}], written, restored, restore_error, error, connection}。

    测试记录已关联采购任务（本工作区同一张表的任何来源记录）→ VALIDATION：验收只能在专用测试记录上做（v1 §6.1）。
    验收期间持有该连接的读取锁，读取不会在「写入测试值 → 恢复原值」之间进行；读取也永远跳过这条测试记录。
    步骤：读结果表字段（核对每个写回列仍存在且可写，并刷新列类型）→ 读测试记录原值 → 写入测试值（文本「回填验收 <时间>」、
    数字 0、采购状态「已拍单」、卡尾号 0000）→ 读回比对 → 无论成败都把原值写回去（原来为空的列清空）。
    全部一致 → 记 writeback_verified_at 与测试记录；否则验收作废（writeback_verified_at 置空）。
    写入 / 读回时飞书报错不抛异常，结果为 ok=false 并带 error；读字段失败、写回对应为空 → 抛错。
    """
    row = get_row(conn, workspace_id, connection_id)
    require_base_access(conn, workspace_id, row["app_token"], claim=True)
    # 与读取互斥：验收写入测试值到恢复原值之间，不让同一连接的读取把测试值当成订单结果接收。
    # 先取读取锁、再锁连接行（与读取的加锁顺序一致，避免死锁）。
    conn.execute("SELECT pg_advisory_xact_lock(%s::int, %s::int)", (INTAKE_LOCK_NS, row["id"] % 2_147_483_647))
    row = get_row(conn, workspace_id, connection_id, for_update=True)
    record_id = _text(record_id, "测试记录", "record_id", 64, required=True)
    if not row["writeback_map"]:
        raise validation("还没有设置写回列，请先在连接里选择要写回的列")
    linked = conn.execute(
        "SELECT o.upstream_order_no FROM procure.order_sources s "
        "JOIN procure.purchase_orders o ON o.id = s.purchase_order_id AND o.workspace_id = s.workspace_id "
        "WHERE s.workspace_id = %s AND s.kind = 'feishu' AND s.feishu_record_id = %s "
        "AND s.feishu_connection_id IN (SELECT id FROM procure.feishu_connections WHERE workspace_id = %s "
        "AND app_token = %s AND (source_table_id = %s OR result_table_id = %s)) LIMIT 1",
        (workspace_id, record_id, workspace_id, row["app_token"], row["result_table_id"], row["result_table_id"]),
    ).fetchone()
    if linked is not None:
        raise validation(f"这条记录已关联采购任务 {linked['upstream_order_no']}，验收会写入测试值，"
                         "请改用一条专用测试记录", field="record_id")
    app_token, table_id = row["app_token"], row["result_table_id"]
    stamp = clock.now().astimezone(clock.BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    result: dict[str, Any] = {"ok": False, "mismatches": [], "written": {}, "restored": False, "restore_error": None,
                              "error": None}
    with client_scope(client) as c:
        result_fields = _list_fields(c, app_token, table_id, "读取结果表字段")
        wb_map, wb_types = clean_writeback_map(row["writeback_map"], result_fields)
        test_fields = feishu_fields.build_writeback_fields(_test_result(stamp), wb_map, wb_types)
        result["written"] = test_fields
        original: dict[str, Any] | None = None
        wrote = False
        try:
            original = c.get_record(app_token, table_id, record_id)["fields"]
            c.batch_update_records(app_token, table_id, [{"record_id": record_id, "fields": test_fields}])
            wrote = True
            back = c.get_record(app_token, table_id, record_id)["fields"]
            col_to_key = {col: key for key, col in wb_map.items()}
            for col, expected in test_fields.items():
                t = wb_types.get(col)
                if _comparable(expected, t) != _comparable(back.get(col), t):
                    result["mismatches"].append({
                        "result_field": col_to_key.get(col), "column": col,
                        "expected": _jsonable(feishu_fields.cell_to_python(expected, t)),
                        "actual": _jsonable(feishu_fields.cell_to_python(back.get(col), t))})
            result["ok"] = not result["mismatches"]
        except FeishuError as exc:
            result["error"] = feishu_error(exc, "写回验收").message
        finally:
            if wrote and original is not None:
                restore = {col: _restore_value(original.get(col), wb_types.get(col)) for col in test_fields}
                try:
                    c.batch_update_records(app_token, table_id, [{"record_id": record_id, "fields": restore}])
                    result["restored"] = True
                except FeishuError as exc:
                    result["restore_error"] = feishu_error(exc, "恢复测试记录原值").message

    now = clock.now()
    ak, aid = _actor(actor_user_id)
    if result["ok"]:
        conn.execute(
            "UPDATE procure.feishu_connections SET writeback_verified_at = %s, writeback_test_record = %s, "
            "writeback_field_types = %s, updated_at = %s WHERE id = %s AND workspace_id = %s",
            (now, record_id, Jsonb(wb_types), now, row["id"], workspace_id))
        events.record(conn, workspace_id, "connection", row["id"], "connection.writeback_verified", actor_kind=ak,
                      actor_id=aid, detail={"record_id": record_id, "columns": sorted(test_fields),
                                            "restored": result["restored"]})
    else:
        conn.execute(
            "UPDATE procure.feishu_connections SET writeback_verified_at = NULL, writeback_test_record = %s, "
            "writeback_field_types = %s, updated_at = %s WHERE id = %s AND workspace_id = %s",
            (record_id, Jsonb(wb_types), now, row["id"], workspace_id))
        events.record(conn, workspace_id, "connection", row["id"], "connection.writeback_verify_failed",
                      actor_kind=ak, actor_id=aid, detail={"record_id": record_id, "mismatches": result["mismatches"],
                                                           "error": result["error"]})
    result["connection"] = get(conn, workspace_id, row["id"])
    return result


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    return v
