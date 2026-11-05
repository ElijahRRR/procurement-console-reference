"""测试基座：每个测试会话建一个临时库，每个测试前清空全部表。

需要一个可连的 PostgreSQL（16/17）。管理连接默认 `dbname=postgres`，可用环境变量
PC_TEST_ADMIN_DSN 覆盖。连不上时整体跳过（并打印原因），不会假装通过。
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from registry import db, paths, settings  # noqa: E402
from services import clock  # noqa: E402

ADMIN_DSN = os.environ.get("PC_TEST_ADMIN_DSN", "dbname=postgres")


def _tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'procure'"
    ).fetchall()
    return [f"procure.{r['tablename']}" for r in rows]


@pytest.fixture(scope="session")
def db_dsn():
    """输入：无 → 输出：临时测试库的 DSN（会话结束时删除）。"""
    name = f"procure_test_{os.getpid()}_{secrets.token_hex(3)}"
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except Exception as exc:  # pragma: no cover - 环境问题
        pytest.skip(f"连不上 PostgreSQL（{ADMIN_DSN}）：{exc}")
    admin.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    dsn = make_conninfo(ADMIN_DSN, dbname=name)
    os.environ["PC_PG_DSN"] = dsn
    settings.reset_for_tests()
    with db.pg_conn(dsn, autocommit=True, statement_timeout_s=0) as conn:
        conn.execute(paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    yield dsn
    settings.reset_for_tests()
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture()
def clean_db(db_dsn):
    """输入：无 → 输出：DSN（测试前清空全部表、解冻时钟）。"""
    with db.pg_conn(db_dsn, autocommit=True) as conn:
        tables = _tables(conn)
        if tables:
            conn.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
    clock.unfreeze()
    yield db_dsn
    clock.unfreeze()


@pytest.fixture()
def conn(clean_db):
    """输入：无 → 输出：一条事务连接（测试结束 commit；用来准备数据和断言）。"""
    with db.pg_conn(clean_db) as c:
        yield c


@pytest.fixture()
def workspace(conn):
    """输入：无 → 输出：{id, user_id}：一个工作区和它的所有者（密码不可登录，仅供服务层测试）。"""
    ws = conn.execute(
        "INSERT INTO procure.workspaces (name) VALUES ('测试工作区') RETURNING id"
    ).fetchone()["id"]
    uid = conn.execute(
        "INSERT INTO procure.users (username, display_name, password_hash) "
        "VALUES (%s, '测试所有者', 'x') RETURNING id",
        (f"owner_{ws}",),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO procure.workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'owner')",
        (ws, uid),
    )
    conn.execute("INSERT INTO procure.workspace_settings (workspace_id) VALUES (%s)", (ws,))
    conn.commit()
    return {"id": ws, "user_id": uid}
