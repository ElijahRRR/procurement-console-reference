"""建库 + 建表：库不存在时先建库（连同一服务器的维护库 postgres 执行 CREATE DATABASE），再执行
refdata/schema.sql（幂等，可反复执行；旧库靠脚本里的「追加」段升级到当前结构）。

建表脚本与随后的一次性回填在**同一个事务**里：要么都生效，要么都不生效。

- **来源指定买家号的回填**（backfill_designations）：只在 purchase_orders.designated_customer_id 这一列**第一次加上**
  的那一次执行（执行脚本之前先记下列在不在）。旧库里来源早已填好买家号、**没有真实采购成功**的任务由此带上指定
  （与实时接收同一条规则：模拟采购成功、已取消的任务也补——清除模拟结果、撤销取消后它们会回到分配，不能落到别的
  账号）；之后重复 db_init 不再回填、不改数据。回填不触发分配：升级后启动服务，点一次「重新分配」；在那之前领取时的
  复核保证这些任务不会被别的账号买走。

另提供 schema_problem()：serve 启动前检查库结构是不是当前版本，不是就让入口层打印说明并以非零码退出
（升级顺序：停服务 → python cli.py db_init → 启动 → 点一次「重新分配」→ 重新加载插件）。
"""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace
from typing import Any

import psycopg
from psycopg import sql

from registry import db, paths
from services import designation, events, normalize

#: 库不存在（invalid_catalog_name）
_DB_MISSING = "3D000"

SCHEMA_OUTDATED_MESSAGE = "数据库结构是旧版：请先运行 python cli.py db_init"
SCHEMA_MISSING_MESSAGE = "数据库还没有建表：请先运行 python cli.py db_init"
#: 当前结构的标志：这些（表, 列）必须都在。每次 schema.sql 加了服务代码依赖的列，就在这里补一项。
_REQUIRED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("devices", "login_customer_id"),
    ("devices", "hidden_at"),
    ("execution_runs", "bj_date"),
    ("execution_runs", "scheduled_slot"),
    ("ignored_buyer_ids", "customer_id"),
    ("purchase_orders", "designated_customer_id"),
)
#: 回填时不看的任务状态：已关闭是终态，回不到分配（已采购、已取消都可能回来：清除模拟结果、撤销取消、同意重采）
_BACKFILL_SKIP_STATUSES = ("closed",)
#: 旧版规范化把买家号两列的表格错误值存成 null，只在 issues 里留一句「买家号是表格错误值 X，按没读到处理」
#: （买家号 ID 那列写作「买家号ID」；当前版本对带可信单号的行写「买家号 ID 是…」）
_LEGACY_ERROR_RE = re.compile(r"^(买家号 ?ID|买家号) ?是表格错误值 (\S+)，按没读到处理$")
#: 预筛用：issues 里有没有这样一句（LIKE 模式，作为参数传入）
_LEGACY_ERROR_LIKE = "买家号%是表格错误值%"
#: 旧结构的标志：这些（表, 列）必须都不在（升级时删掉的列）。
_REMOVED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("devices", "bound_account_id"),
    ("devices", "identity_status"),
    ("devices", "revoked_at"),
    ("buyer_accounts", "default_device_id"),
    ("device_pairings", "code_hash"),
)


def _missing_dbname(exc: psycopg.OperationalError, dsn: str | None) -> str | None:
    """输入：连目标库失败的异常、DSN → 输出：要建的库名；不是「库不存在」→ None。

    libpq 的连接错误常常不带 SQLSTATE（文字还随服务器语言变化），所以不只看 3D000：拿到库名后到维护库里
    查 pg_database，确实没有才算缺库。维护库也连不上 → None（照常抛出原来的错误）。
    """
    name = db.dsn_dbname(dsn)
    if not name and exc.pgconn is not None and exc.pgconn.db:
        name = exc.pgconn.db.decode("utf-8", "replace")
    if not name or name == db.MAINTENANCE_DB:
        return None
    if exc.sqlstate not in (None, _DB_MISSING):
        return None
    try:
        with db.admin_conn(dsn) as admin:
            found = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    except psycopg.Error:
        return None
    return None if found else name


def _create_database(dsn: str | None, name: str) -> bool:
    """输入：DSN、库名 → 输出：是否由本次创建（并发下别人先建好了 → False）。"""
    with db.admin_conn(dsn) as admin:
        try:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except psycopg.errors.DuplicateDatabase:
            return False
    return True


def _ensure_database(dsn: str | None) -> tuple[str | None, bool]:
    """输入：DSN → 输出：(库名, 是否由本次创建)。目标库连得上 → 不动；库不存在 → 建库并打印一行说明；
    其他连接错误照常抛出。"""
    try:
        with db.pg_conn(dsn, autocommit=True):
            return db.dsn_dbname(dsn), False
    except psycopg.OperationalError as exc:
        missing = _missing_dbname(exc, dsn)
        if missing is None:
            raise
    created = _create_database(dsn, missing)
    print(f"数据库 {missing} 不存在，已创建" if created else f"数据库 {missing} 已由其他进程创建", file=sys.stderr)
    return missing, created


def run(dsn: str | None = None) -> dict[str, Any]:
    """输入：可选 DSN（缺省 PC_PG_DSN）→ 输出：摘要 {schema, ok, database, database_created, designations_backfilled}
    （schema = 建表脚本文件名；database = 库名；database_created = 库是不是这次建的；designations_backfilled =
    这次补上来源指定的任务数，只有旧库第一次升级时可能非 0）。

    库不存在 → 先建库（库名按标识符安全加引号），并在标准错误输出打印一行说明；随后在**一个事务**里执行建表脚本与
    回填（任何一步出错整体回滚：库结构仍是旧版，下次 db_init 从头再来）。标准错误输出另有「已按 schema.sql 建表或
    升级到当前结构」，补了指定时再提醒启动后点一次「重新分配」。
    """
    script = paths.SCHEMA_SQL.read_text(encoding="utf-8")
    name, created = _ensure_database(dsn)
    with db.pg_conn(dsn, statement_timeout_s=0) as conn:        # 一个事务：建表脚本与回填一起提交
        had_designation = _has_column(conn, "purchase_orders", "designated_customer_id")
        conn.execute(script)
        backfilled = 0 if had_designation else backfill_designations(conn)
    print(f"已按 {paths.SCHEMA_SQL.name} 建表或升级到当前结构", file=sys.stderr)
    if backfilled:
        print(f"已为 {backfilled} 笔没有真实采购成功的任务补上来源里早已填好的买家号（来源指定）；"
              "启动服务后请点一次「重新分配」", file=sys.stderr)
    return {"schema": str(paths.SCHEMA_SQL.name), "ok": True, "database": name, "database_created": created,
            "designations_backfilled": backfilled}


def _has_column(conn, table: str, column: str) -> bool:
    """输入：连接、procure 下的表名、列名 → 输出：这一列现在在不在（表不存在也算不在）。"""
    return conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'procure' AND table_name = %s "
        "AND column_name = %s", (table, column)).fetchone() is not None


def backfill_designations(conn) -> int:
    """输入：连接（库已是当前结构）→ 输出：补上来源指定的任务数。旧库升级时的一次性回填，不 commit、不触发分配。

    逐个工作区，看两列都为空的任务，规则与实时接收（orders._designation_change）一致：

    - 跳过已关闭的任务，以及有本系统**真实**采购成功的尝试的任务（非模拟、非来源带来的外部单号；与
      orders._has_real_success 同一个条件）。模拟采购成功、已取消、采购中、异常等状态都照样补：清除模拟结果、撤销取消、
      同意重采之后它们会回到分配，没有指定就会被自动分给别的账号。
    - 看任务**主来源种类**的来源关联里存下的规范化记录，按 last_seen_at 从新到旧（同刻取 id 大的）取第一条「表态且
      非空」的（与接收用同一套 designation.statement：带可信 AMZ 订单号的行不表态，占位写法按没写）。行上的 AMZ
      订单号就是这笔任务登记过的单号（含模拟单号，例如导出的采购结果再上传）→ 这条关联不算表态，接着看更早的。
    - 旧版规范化把这两列的表格错误值存成 null、只在 issues 里留「买家号是表格错误值 X，按没读到处理」（ID 列写作
      「买家号ID」）：这一列为空时把 X 还原回去再判断，与当前版本一样成为一个读不出的指定（任务留在待分配并写明原因）。

    把清洗后的买家号 ID / 买家号写进任务，并记一条 'order.designation_changed' 事件（actor system，
    detail.source = "upgrade"）。只填两列都为空的任务，单独再调用也不会覆盖已有的指定。
    """
    done = 0
    for ws in conn.execute("SELECT id FROM procure.workspaces ORDER BY id").fetchall():
        done += _backfill_workspace(conn, ws["id"])
    return done


def _legacy_buyer_fields(n: dict[str, Any]) -> tuple[Any, Any]:
    """输入：一条规范化记录 → 输出：(买家号 ID, 买家号)：字段为空、而 issues 里有旧版「…是表格错误值 X，按没读到处理」
    时还原成 X（只认 normalize.EXCEL_ERRORS 里的错误值）。"""
    cid, name = n.get("external_buyer_customer_id"), n.get("external_buyer_account")
    issues = n.get("issues")
    for text in issues if isinstance(issues, list) else ():
        m = _LEGACY_ERROR_RE.match(text) if isinstance(text, str) else None
        if m is None or not normalize.is_error_value(m.group(2)):
            continue
        if "ID" in m.group(1):
            if not str(cid or "").strip():
                cid = m.group(2)
        elif not str(name or "").strip():
            name = m.group(2)
    return cid, name


def _backfill_workspace(conn, workspace_id: int) -> int:
    rows = conn.execute(
        """
        SELECT o.id, s.normalized,
               ARRAY(SELECT a.amazon_order_no FROM procure.purchase_attempts a
                     WHERE a.purchase_order_id = o.id AND a.workspace_id = o.workspace_id
                       AND a.amazon_order_no IS NOT NULL)
               || CASE WHEN o.external_amz_order_no IS NULL THEN ARRAY[]::text[]
                       ELSE ARRAY[o.external_amz_order_no] END AS own_nos
        FROM procure.purchase_orders o
        JOIN procure.order_sources s
          ON s.purchase_order_id = o.id AND s.workspace_id = o.workspace_id AND s.kind = o.primary_source_kind
        WHERE o.workspace_id = %s AND o.status <> ALL(%s)
          AND o.designated_customer_id IS NULL AND o.designated_buyer_name IS NULL
          AND NOT EXISTS (SELECT 1 FROM procure.purchase_attempts a
                          WHERE a.purchase_order_id = o.id AND a.workspace_id = o.workspace_id
                            AND a.status = 'succeeded' AND NOT a.simulated AND a.kind <> 'external')
          AND (COALESCE(btrim(s.normalized->>'external_buyer_account'), '') <> ''
               OR COALESCE(btrim(s.normalized->>'external_buyer_customer_id'), '') <> ''
               OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(
                              CASE WHEN jsonb_typeof(s.normalized->'issues') = 'array' THEN s.normalized->'issues'
                                   ELSE '[]'::jsonb END) AS i(text)
                          WHERE i.text LIKE %s))
        ORDER BY o.id, s.last_seen_at DESC, s.id DESC
        """,
        (workspace_id, list(_BACKFILL_SKIP_STATUSES), _LEGACY_ERROR_LIKE),
    ).fetchall()
    picked: dict[int, tuple[str | None, str | None]] = {}
    for r in rows:
        if r["id"] in picked:
            continue
        n = r["normalized"] or {}
        amz = n.get("external_amz_order_no")
        own = str(amz).strip() if amz is not None else ""
        if own and own in set(r["own_nos"] or ()):
            continue                    # 行上的单号就是这笔任务登记过的单号：这两列只是「谁买的」，不是指定
        cid, name = _legacy_buyer_fields(n)
        said = designation.statement(SimpleNamespace(
            external_amz_order_no=amz, external_buyer_customer_id=cid, external_buyer_account=name))
        if said is designation.SILENT or designation.is_empty(said):
            continue
        picked[r["id"]] = said
    done = 0
    for order_id, (cid, name) in picked.items():
        hit = conn.execute(
            "UPDATE procure.purchase_orders SET designated_customer_id = %s, designated_buyer_name = %s "
            "WHERE id = %s AND workspace_id = %s AND designated_customer_id IS NULL AND designated_buyer_name IS NULL "
            "RETURNING id",
            (cid, name, order_id, workspace_id),
        ).fetchone()
        if hit is None:
            continue
        changes = {col: [None, v] for col, v in zip(designation.ORDER_COLUMNS, (cid, name)) if v is not None}
        events.record(conn, workspace_id, "order", order_id, designation.EVENT_KIND, actor_kind="system",
                      detail={"changes": changes, "source": "upgrade"})
        done += 1
    return done


def schema_problem(dsn: str | None = None, conn=None) -> str | None:
    """输入：可选 DSN（缺省 PC_PG_DSN）或现成的连接 → 输出：库结构不是当前版本时的中文说明；是当前版本 → None。

    只读 information_schema，不改任何东西。还没建过表 → SCHEMA_MISSING_MESSAGE；缺当前结构的列、或旧结构的列还在
    （例如 devices.login_customer_id 不存在）→ SCHEMA_OUTDATED_MESSAGE。连不上数据库时照常抛出连接错误。
    `python cli.py serve` 启动时调用：有说明就打印并以非零码退出。
    """
    if conn is None:
        with db.pg_conn(dsn) as c:
            return schema_problem(conn=c)
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'procure'"
    ).fetchall()
    have = {(r["table_name"], r["column_name"]) for r in rows}
    if not any(table == "devices" for table, _ in have):
        return SCHEMA_MISSING_MESSAGE
    if any(col not in have for col in _REQUIRED_COLUMNS) or any(col in have for col in _REMOVED_COLUMNS):
        return SCHEMA_OUTDATED_MESSAGE
    return None
