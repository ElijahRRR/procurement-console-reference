"""建表：执行 refdata/schema.sql（幂等）。"""

from __future__ import annotations

from registry import db, paths


def run(dsn: str | None = None) -> dict:
    """输入：可选 DSN → 输出：摘要 {schema, statements}。"""
    sql = paths.SCHEMA_SQL.read_text(encoding="utf-8")
    with db.pg_conn(dsn, autocommit=True, statement_timeout_s=0) as conn:
        conn.execute(sql)
    return {"schema": str(paths.SCHEMA_SQL.name), "ok": True}
