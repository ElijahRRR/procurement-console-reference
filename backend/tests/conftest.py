"""测试基座：每个测试会话建一个临时库，每个测试前清空全部表。

需要一个可连的 PostgreSQL（16/17）。管理连接默认 `dbname=postgres`，可用环境变量
PC_TEST_ADMIN_DSN 覆盖。连不上时整体跳过（并打印原因），不会假装通过。
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
from collections.abc import Iterator
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

# ── 测试与本机配置隔离 ────────────────────────────────────────────────────────
# 测试只认缺省设置：不读 backend/.env，也不让 shell 里的 PC_* / FEISHU_* 调参漏进来（例如本机 .env 里改大了
# PC_LEASE_TTL_S，执行权超时类测试就会失败）。只保留 PC_TEST_ADMIN_DSN；PC_PG_DSN 由 db_dsn 夹具指向临时库。
#: 测试会话里不加载的 .env（模块常量，测试可改成临时文件来验证这条隔离）
BLOCKED_DOTENV = (paths.BACKEND_DIR / ".env").resolve()
_real_load_dotenv = settings._load_dotenv


def _load_dotenv_except_local(path: Path) -> None:
    """settings._load_dotenv 的测试替身：本机的 backend/.env（BLOCKED_DOTENV）一律跳过，其他路径照常解析
    （test_registry_settings 用临时 .env 测解析规则）。"""
    if Path(path).resolve() == BLOCKED_DOTENV:
        return
    _real_load_dotenv(path)


def isolate_settings() -> None:
    """输入：无 → 输出：无。删掉进程环境里的 PC_* / FEISHU_*（PC_TEST_ADMIN_DSN 除外），屏蔽本机 backend/.env，
    并清掉已缓存的设置。导入本文件时执行一次。"""
    for key in [k for k in os.environ if k.startswith(("PC_", "FEISHU_")) and k != "PC_TEST_ADMIN_DSN"]:
        del os.environ[key]
    settings._load_dotenv = _load_dotenv_except_local
    settings.reset_for_tests()


isolate_settings()


# ── 分层标记 ──────────────────────────────────────────────────────────────────
# 经过 HTTP（server/ 的应用）的测试自动带 `http` 标记：test_api_*.py 里的全部用例，以及其他文件里用到 `api` 夹具
# （含依赖它的 owner / other）的用例。只跑服务层：python -m pytest -m "not http"；只跑 HTTP 层：-m http。


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "http: 经过 HTTP 层（server 应用）的测试；其余是只走服务层 / 工作流的测试")


def pytest_collection_modifyitems(config, items) -> None:
    for item in items:
        module = getattr(item, "module", None)
        name = module.__name__.rsplit(".", 1)[-1] if module is not None else ""
        if name.startswith("test_api_") or "api" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.http)


def _tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'procure'"
    ).fetchall()
    return [f"procure.{r['tablename']}" for r in rows]


@contextlib.contextmanager
def temp_database(prefix: str = "procure_tmp") -> Iterator[str]:
    """输入：库名前缀 → 输出：一个全新的空临时库的 DSN（退出时删除）。

    给需要自己建表的测试用（旧结构升级、db_init 重复执行）：与会话共用的测试库互不影响。库里什么都没有，
    连 procure 这个 schema 也没有。
    """
    name = f"{prefix}_{os.getpid()}_{secrets.token_hex(3)}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(ADMIN_DSN, dbname=name)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def run_schema(dsn: str, script_path: Path | None = None) -> None:
    """输入：DSN、建表脚本路径（缺省当前的 refdata/schema.sql）→ 输出：无（整份脚本作为一条多语句查询在 autocommit
    连接上执行一遍：脚本本身要么全部生效、要么全不生效）。

    只执行脚本：不建库，也不做 db_init 的来源指定回填（db_init.run 把脚本和回填放在同一个事务里，要测回填用它）。
    """
    script = (script_path or paths.SCHEMA_SQL).read_text(encoding="utf-8")
    with db.pg_conn(dsn, autocommit=True, statement_timeout_s=0) as conn:
        conn.execute(script)


@pytest.fixture()
def scratch_db() -> Iterator[str]:
    """输入：无 → 输出：一个空临时库的 DSN（测试结束时删除）。"""
    with temp_database() as dsn:
        yield dsn


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
