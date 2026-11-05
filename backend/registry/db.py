"""数据库连接唯一入口（其他文件禁止自行 psycopg.connect）。

约定：services 的函数接收调用方给的 conn，**不自行 commit / rollback**；
事务边界由入口层（server 路由、workflows、cli）用 `with db.pg_conn() as conn:` 管：
正常退出 commit，异常 rollback。行以 dict 返回（dict_row）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

from registry import settings

#: 单条 SQL 的缺省超时（秒）。卡死的查询响亮失败好过无声挂住。
STATEMENT_TIMEOUT_S = 60


@contextlib.contextmanager
def pg_conn(dsn: str | None = None, autocommit: bool = False,
            statement_timeout_s: float | None = None) -> Iterator[psycopg.Connection]:
    """输入：可选 DSN / autocommit / 超时 → 输出：psycopg 连接上下文（dict 行；总是 close）。"""
    timeout = STATEMENT_TIMEOUT_S if statement_timeout_s is None else statement_timeout_s
    conn = psycopg.connect(
        dsn or settings.get().pg_dsn,
        autocommit=autocommit,
        row_factory=dict_row,
        options=f"-c statement_timeout={int(timeout * 1000)} -c TimeZone=UTC",
        application_name="procurement-console",
    )
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except BaseException:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()
