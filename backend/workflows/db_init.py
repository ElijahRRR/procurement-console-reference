"""建库 + 建表：库不存在时先建库（连同一服务器的维护库 postgres 执行 CREATE DATABASE），再执行
refdata/schema.sql（幂等，可反复执行）。"""

from __future__ import annotations

import sys
from typing import Any

import psycopg
from psycopg import sql

from registry import db, paths

#: 库不存在（invalid_catalog_name）
_DB_MISSING = "3D000"


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
