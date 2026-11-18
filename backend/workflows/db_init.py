"""建库 + 建表：库不存在时先建库（连同一服务器的维护库 postgres 执行 CREATE DATABASE），再执行
refdata/schema.sql（幂等，可反复执行；旧库靠脚本里的「追加」段升级到当前结构）。

另提供 schema_problem()：serve 启动前检查库结构是不是当前版本，不是就让入口层打印说明并以非零码退出
（升级顺序：停服务 → python cli.py db_init → 启动 → 重新加载插件）。
"""

from __future__ import annotations

import sys
from typing import Any

import psycopg
from psycopg import sql

from registry import db, paths

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
)
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
    """输入：可选 DSN（缺省 PC_PG_DSN）→ 输出：摘要 {schema, ok, database, database_created}。

    库不存在 → 先建库（库名按标识符安全加引号），并在标准错误输出打印一行说明；随后执行建表脚本。
    """
    script = paths.SCHEMA_SQL.read_text(encoding="utf-8")
    name, created = _ensure_database(dsn)
    with db.pg_conn(dsn, autocommit=True, statement_timeout_s=0) as conn:
        conn.execute(script)
    print(f"已按 {paths.SCHEMA_SQL.name} 建表（幂等，已有的表不受影响）", file=sys.stderr)
    return {"schema": str(paths.SCHEMA_SQL.name), "ok": True, "database": name, "database_created": created}


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
