"""建表脚本的升级路径（契约 §1）：在合并前的旧结构（tests/data/schema_v1.sql）上执行当前的 refdata/schema.sql，
旧绑定回填成账号的买家号 ID、浏览器行换成新形状、配对码表删除、批次补上北京日期；脚本在旧库、新库上都能重复执行。
另测 serve 启动前的库结构检查（workflows/db_init.schema_problem）。

每个用例用自己的临时库（conftest.temp_database），不碰会话共用的测试库。全部数据虚构。"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from registry import db, paths
from services import accounts, clock, devices, execution
from tests import factories_exec as fx
from tests.conftest import run_schema
from workflows import db_init

SCHEMA_V1 = Path(__file__).resolve().parent / "data" / "schema_v1.sql"
NOW = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)        # 北京 2026-11-18 10:00
OLD_TOKEN = "DEMO-paired-token-from-old-version-000000001"   # 旧版配对得到的令牌（虚构）


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _ident(cid, signed_in=True):
    """旧版存下的身份：没有 conflict 键。"""
    return Jsonb({"signed_in": signed_in, "customer_id": cid, "site": "US",
                  "page_url": "https://www.amazon.com/gp/css/order-history", "at": NOW.isoformat()})


def _seed_v1(conn) -> dict:
    """往旧结构里插旧形状的数据 → 输出：各行的 id（键名即场景）。"""
    ids: dict = {}

    def one(sql, params=()):
        return conn.execute(sql, params).fetchone()["id"]

    ws = one("INSERT INTO procure.workspaces (name) VALUES ('DEMO 工作区 A') RETURNING id")
    ws2 = one("INSERT INTO procure.workspaces (name) VALUES ('DEMO 工作区 B') RETURNING id")
    uid = one("INSERT INTO procure.users (username, password_hash) VALUES ('demo_owner', 'x') RETURNING id")
    ids.update(ws=ws, ws2=ws2, uid=uid)

    def account(key, name, workspace=ws, customer_id=None, archived=False):
        ids[key] = one(
            "INSERT INTO procure.buyer_accounts (workspace_id, name, amazon_customer_id, archived_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (workspace, name, customer_id, NOW if archived else None))
        return ids[key]

    def device(key, name, account_id=None, identity=None, status="unknown", workspace=ws, revoked=False, token=None):
        ids[key] = one(
            """
            INSERT INTO procure.devices (workspace_id, name, browser, token_hash, bound_account_id, ext_version,
                                         last_seen_at, identity, identity_status, identity_at, revoked_at)
            VALUES (%s, %s, 'adspower', %s, %s, '0.1.0', %s, %s, %s, %s, %s) RETURNING id
            """,
            (workspace, name, None if revoked else _sha(token or f"demo-token-{key}"), account_id, NOW, identity,
             status, NOW if identity is not None else None, NOW - timedelta(days=1) if revoked else None))
        return ids[key]

    # ① 绑定了一台已登录设备、账号还没登记 ID → 回填
    device("d_fill", "Mac-DEMO-1", account("a_fill", "环境-DEMO-回填"), _ident("A1DEMO00001"), token=OLD_TOKEN)
    # ② 两台设备报了两个不同的 ID → 拿不准，不动
    account("a_two", "环境-DEMO-两个ID")
    device("d_two1", "Mac-DEMO-2", ids["a_two"], _ident("A1DEMO00002"))
    device("d_two2", "Mac-DEMO-3", ids["a_two"], _ident("A1DEMO00003"))
    # ③ 两台设备报了同一个 ID（一个小写、带空白）→ 回填，事件里记编号小的那台
    account("a_same", "环境-DEMO-同一个ID")
    device("d_same1", "Mac-DEMO-4", ids["a_same"], _ident(" a1demo00004 "))
    device("d_same2", "Mac-DEMO-5", ids["a_same"], _ident("A1DEMO00004"))
    # ④ 账号已经登记了 ID → 不动（设备上报的是另一个 ID）
    device("d_has", "Mac-DEMO-6", account("a_has", "环境-DEMO-已登记", customer_id="A1DEMO00009"),
           _ident("A1DEMO00008"), status="mismatch")
    # ⑤ 绑定的设备已撤销 → 不用它的 ID；设备变成「已移除」
    device("d_revoked", "Mac-DEMO-已撤销", account("a_revoked", "环境-DEMO-设备已撤销"), _ident("A1DEMO00005"),
           revoked=True)
    # ⑥ 设备报的 ID 已被同工作区另一个账号登记 → 不动
    device("d_taken", "Mac-DEMO-7", account("a_taken", "环境-DEMO-ID被占"), _ident("A1DEMO00009"))
    # ⑦ 两个账号的设备报了同一个 ID → 都不动
    device("d_dup1", "Mac-DEMO-8", account("a_dup1", "环境-DEMO-撞车1"), _ident("A1DEMO00007"))
    device("d_dup2", "Mac-DEMO-9", account("a_dup2", "环境-DEMO-撞车2"), _ident("A1DEMO00007"))
    # ⑧ 已归档账号、未登录的设备、ID 形状不对、从未上报 → 都不动
    device("d_arch", "Mac-DEMO-10", account("a_arch", "环境-DEMO-已归档", archived=True), _ident("A1DEMO00010"))
    device("d_out", "Mac-DEMO-11", account("a_out", "环境-DEMO-未登录"), _ident("A1DEMO00011", signed_in=False),
           status="signed_out")
    device("d_bad", "Mac-DEMO-12", account("a_bad", "环境-DEMO-坏ID"), _ident("not-an-id"))
    device("d_none", "Mac-DEMO-13", account("a_none", "环境-DEMO-没上报"))
    device("d_unbound", "Mac-DEMO-未绑定", None, _ident("A1DEMO00012"))
    # ⑨ 另一个工作区：同一个 ID 互不影响 → 回填
    device("d_ws2", "PC-DEMO-1", account("a_ws2", "环境-DEMO-另一个工作区", workspace=ws2), _ident("A1DEMO00001"),
           workspace=ws2)

    conn.execute("UPDATE procure.buyer_accounts SET default_device_id = %s WHERE id = %s",
                 (ids["d_fill"], ids["a_fill"]))
    for code, used in (("demo-code-used", ids["d_fill"]), ("demo-code-open", None)):
        conn.execute(
            "INSERT INTO procure.device_pairings (code_hash, workspace_id, created_by, expires_at, used_at, device_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (_sha(code), ws, uid, NOW + timedelta(minutes=10), NOW if used else None, used))

    def run(key, account_id, device_id, started, ended=None, reason=None):
        ids[key] = one(
            """
            INSERT INTO procure.execution_runs (workspace_id, account_id, device_id, trigger, simulated, started_at,
                                                heartbeat_at, lease_expires_at, ended_at, end_reason)
            VALUES (%s, %s, %s, 'manual', true, %s, %s, %s, %s, %s) RETURNING id
            """,
            (ws, account_id, device_id, started, started, started + timedelta(seconds=180), ended, reason))

    late = datetime(2026, 11, 16, 17, 30, tzinfo=timezone.utc)      # 北京 11-17 01:30：北京日期比 UTC 日期晚一天
    run("r_done", ids["a_has"], ids["d_has"], late, late + timedelta(minutes=5), "finished")
    run("r_revoked", ids["a_revoked"], ids["d_revoked"], NOW - timedelta(days=1), NOW - timedelta(days=1),
        "device_revoked")
    run("r_open", ids["a_has"], ids["d_has"], NOW - timedelta(minutes=1))                    # 升级时还没结束的批次
    # 旧结构不拦「一台设备多个未结束批次」：留最新的，其余按租约过期结束
    run("r_twin_old", ids["a_two"], ids["d_two1"], NOW - timedelta(minutes=3))
    run("r_twin_new", ids["a_dup1"], ids["d_two1"], NOW - timedelta(minutes=2))
    conn.execute("INSERT INTO procure.events (workspace_id, entity, entity_id, actor_kind, actor_id, kind) "
                 "VALUES (%s, 'device', %s, 'device', %s, 'device.paired')", (ws, ids["d_fill"], ids["d_fill"]))
    return ids


def _columns(conn) -> set[tuple]:
    rows = conn.execute(
        "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'procure'").fetchall()
    return {tuple(r.values()) for r in rows}


def _indexes(conn) -> set[tuple]:
    rows = conn.execute("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'procure'").fetchall()
    return {tuple(r.values()) for r in rows}


def _constraints(conn) -> set[tuple]:
    rows = conn.execute(
        """
        SELECT c.conrelid::regclass::text AS tbl, c.conname, c.contype::text AS contype,
               pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'procure'
        """).fetchall()
    return {tuple(r.values()) for r in rows}


def _adopted(conn) -> dict:
    rows = conn.execute("SELECT * FROM procure.events WHERE kind = 'account.customer_id_adopted' ORDER BY id").fetchall()
    return {r["entity_id"]: r for r in rows}


@pytest.fixture()
def old_db(scratch_db):
    """旧结构 + 旧数据的临时库 → (DSN, 各行 id)。"""
    run_schema(scratch_db, SCHEMA_V1)
    with db.pg_conn(scratch_db) as conn:
        ids = _seed_v1(conn)
    return scratch_db, ids


@pytest.fixture()
def _clock():
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def test_fixture_is_the_pre_change_schema(scratch_db):
    run_schema(scratch_db, SCHEMA_V1)
    with db.pg_conn(scratch_db) as conn:
        cols = {(c[0], c[1]) for c in _columns(conn)}
    assert {("devices", "bound_account_id"), ("devices", "identity_status"), ("devices", "revoked_at"),
            ("buyer_accounts", "default_device_id"), ("device_pairings", "code_hash")} <= cols
    assert ("devices", "login_customer_id") not in cols and ("execution_runs", "bj_date") not in cols


def test_upgrade_backfills_legacy_bindings_once(old_db):
    dsn, ids = old_db
    run_schema(dsn)
    with db.pg_conn(dsn) as conn:
        got = {r["id"]: r["amazon_customer_id"] for r in conn.execute(
            "SELECT id, amazon_customer_id FROM procure.buyer_accounts").fetchall()}
        expected = {ids["a_fill"]: "A1DEMO00001", ids["a_same"]: "A1DEMO00004", ids["a_has"]: "A1DEMO00009",
                    ids["a_ws2"]: "A1DEMO00001"}
        assert {k: v for k, v in got.items() if v is not None} == expected
        for key in ("a_two", "a_revoked", "a_taken", "a_dup1", "a_dup2", "a_arch", "a_out", "a_bad", "a_none"):
            assert got[ids[key]] is None, key
        ev = _adopted(conn)
        assert set(ev) == {ids["a_fill"], ids["a_same"], ids["a_ws2"]}
        e = ev[ids["a_fill"]]
        assert e["workspace_id"] == ids["ws"] and e["entity"] == "account" and e["actor_kind"] == "system"
        assert e["actor_id"] is None
        assert e["detail"] == {"customer_id": "A1DEMO00001", "device_id": ids["d_fill"], "via": "legacy_binding"}
        assert ev[ids["a_same"]]["detail"] == {"customer_id": "A1DEMO00004", "device_id": ids["d_same1"],
                                              "via": "legacy_binding"}
        assert ev[ids["a_ws2"]]["workspace_id"] == ids["ws2"]
    # 再执行一遍：不重复回填、不重复记事件
    run_schema(dsn)
    with db.pg_conn(dsn) as conn:
        assert len(_adopted(conn)) == 3
        assert conn.execute("SELECT count(*) AS n FROM procure.buyer_accounts WHERE amazon_customer_id IS NOT NULL"
                            ).fetchone()["n"] == 4


def test_upgrade_reshapes_devices_and_drops_pairing(old_db):
    dsn, ids = old_db
    run_schema(dsn)
    with db.pg_conn(dsn) as conn:
        cols = {(c[0], c[1]) for c in _columns(conn)}
        assert {("devices", "login_customer_id"), ("devices", "hidden_at"), ("devices", "browser"),
                ("ignored_buyer_ids", "customer_id"), ("execution_runs", "bj_date"),
                ("execution_runs", "scheduled_slot")} <= cols
        for gone in (("devices", "bound_account_id"), ("devices", "identity_status"), ("devices", "revoked_at"),
                     ("buyer_accounts", "default_device_id")):
            assert gone not in cols, gone
        assert not any(t == "device_pairings" for t, _ in cols)
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM procure.devices").fetchall()}
        login = {key: rows[ids[key]]["login_customer_id"] for key in ids if key.startswith("d_")}
        assert login == {"d_fill": "A1DEMO00001", "d_two1": "A1DEMO00002", "d_two2": "A1DEMO00003",
                         "d_same1": "A1DEMO00004", "d_same2": "A1DEMO00004", "d_has": "A1DEMO00008",
                         "d_revoked": "A1DEMO00005", "d_taken": "A1DEMO00009", "d_dup1": "A1DEMO00007",
                         "d_dup2": "A1DEMO00007", "d_arch": "A1DEMO00010", "d_out": None, "d_bad": None,
                         "d_none": None, "d_unbound": "A1DEMO00012", "d_ws2": "A1DEMO00001"}
        revoked = rows[ids["d_revoked"]]
        assert revoked["hidden_at"] == NOW - timedelta(days=1) and revoked["token_hash"] is None
        assert all(r["hidden_at"] is None for i, r in rows.items() if i != ids["d_revoked"])
        assert rows[ids["d_fill"]]["browser"] == "adspower" and rows[ids["d_fill"]]["workspace_id"] == ids["ws"]
        assert conn.execute("SELECT count(*) AS n FROM procure.events WHERE kind = 'device.paired'").fetchone()["n"] == 1
        # 之后 login_customer_id 由服务端维护：重复执行脚本不得把它按旧身份写回去
        conn.execute("UPDATE procure.devices SET login_customer_id = NULL WHERE id = %s", (ids["d_fill"],))
    run_schema(dsn)
    with db.pg_conn(dsn) as conn:
        row = conn.execute("SELECT * FROM procure.devices WHERE id = %s", (ids["d_fill"],)).fetchone()
        assert row["login_customer_id"] is None and row["identity"]["customer_id"] == "A1DEMO00001"


def test_upgrade_backfills_run_dates_and_keeps_one_open_run_per_device(old_db):
    dsn, ids = old_db
    run_schema(dsn)
    run_schema(dsn)
    with db.pg_conn(dsn) as conn:
        runs = {r["id"]: r for r in conn.execute("SELECT * FROM procure.execution_runs").fetchall()}
        assert runs[ids["r_done"]]["bj_date"] == date(2026, 11, 17)                 # 按北京日期，不是 UTC 日期
        assert runs[ids["r_open"]]["bj_date"] == date(2026, 11, 18)
        assert all(r["bj_date"] is not None and r["scheduled_slot"] is None for r in runs.values())
        assert runs[ids["r_open"]]["ended_at"] is None                              # 升级时未结束的批次原样留着
        assert runs[ids["r_revoked"]]["end_reason"] == "device_revoked"             # 历史行不改
        old, new = runs[ids["r_twin_old"]], runs[ids["r_twin_new"]]
        assert old["ended_at"] is not None and old["end_reason"] == "lease_expired" and new["ended_at"] is None
        nullable = conn.execute(
            "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'procure' "
            "AND table_name = 'execution_runs' AND column_name = 'bj_date'").fetchone()["is_nullable"]
        assert nullable == "NO"
        names = {i[1] for i in _indexes(conn)}
        assert {"execution_runs_one_active_per_account", "execution_runs_one_active_per_device",
                "execution_runs_slot_once", "devices_login_customer_idx", "buyer_accounts_customer_id_uq"} <= names


def test_upgraded_database_has_the_same_structure_as_a_fresh_one(old_db):
    dsn, _ = old_db
    run_schema(dsn)
    run_schema(dsn)
    with db.pg_conn(dsn) as conn:
        upgraded = (_columns(conn), _indexes(conn), _constraints(conn))
    with db.pg_conn(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA procure CASCADE")
    run_schema(dsn)
    with db.pg_conn(dsn) as conn:
        fresh = (_columns(conn), _indexes(conn), _constraints(conn))
    for what, a, b in zip(("列", "索引", "约束"), upgraded, fresh):
        assert a == b, f"{what}不一致：只在升级后的库里 {sorted(a - b)}；只在新库里 {sorted(b - a)}"


def test_new_schema_runs_twice_on_a_fresh_database(scratch_db):
    run_schema(scratch_db)
    with db.pg_conn(scratch_db) as conn:
        first = (_columns(conn), _indexes(conn), _constraints(conn))
        assert db_init.schema_problem(conn=conn) is None
    run_schema(scratch_db)
    with db.pg_conn(scratch_db) as conn:
        assert (_columns(conn), _indexes(conn), _constraints(conn)) == first
        assert conn.execute("SELECT count(*) AS n FROM procure.events").fetchone()["n"] == 0


def test_db_init_upgrades_an_old_database_in_place(old_db, capsys):
    dsn, ids = old_db
    assert db_init.schema_problem(dsn) == "数据库结构是旧版：请先运行 python cli.py db_init"
    out = db_init.run(dsn=dsn)
    assert out["ok"] is True and out["database_created"] is False
    assert db_init.run(dsn=dsn)["ok"] is True                                       # 重复执行
    assert db_init.schema_problem(dsn) is None
    capsys.readouterr()


def test_services_work_on_the_upgraded_database(old_db, _clock):
    """升级后：旧令牌仍然有效、原来绑定的账号直接可用（不用所有者再做任何事），没回填上的出现在待登记列表里。"""
    dsn, ids = old_db
    run_schema(dsn)
    with db.pg_conn(dsn) as conn:
        dev = devices.authenticate(conn, OLD_TOKEN)
        assert dev["id"] == ids["d_fill"] and devices.login_state(dev) == "signed_in"
        # 旧插件（0.1.0）心跳 → 看得懂的报错；新插件带着旧令牌登记 → 还是原来那一行
        with pytest.raises(Exception) as e:
            devices.heartbeat(conn, dev, ext_version="0.1.0")
        assert getattr(e.value, "code", None) == "PLUGIN_OUTDATED"
        again = devices.register(conn, OLD_TOKEN, ext_version="0.2.0")
        assert again == {"device_id": ids["d_fill"], "device_name": "Mac-DEMO-1", "workspace_id": ids["ws"],
                         "workspace_name": "DEMO 工作区 A", "created": False}
        hb = devices.heartbeat(conn, dev, ext_version="0.2.0", identity=fx.identity("A1DEMO00001"))
        assert hb["login"]["state"] == "ready" and hb["account"]["id"] == ids["a_fill"]
        out = execution.start_run(conn, dev, customer_id="A1DEMO00001", trigger="manual", simulated=True)
        assert out["account"] == {"id": ids["a_fill"], "name": "环境-DEMO-回填"}
        assert accounts.get(conn, ids["ws"], ids["a_fill"])["browsers"][0]["id"] == ids["d_fill"]
        pending = {i["customer_id"] for i in accounts.discovered(conn, ids["ws"])}
        # 没被未归档账号登记的 ID（已撤销 = 已移除的那台不算）
        assert pending == {"A1DEMO00002", "A1DEMO00003", "A1DEMO00007", "A1DEMO00008", "A1DEMO00010", "A1DEMO00012"}
        listed = {d["id"] for d in devices.list_devices(conn, ids["ws"])}
        assert ids["d_revoked"] not in listed and ids["d_fill"] in listed
        assert ids["d_revoked"] in {d["id"] for d in devices.list_devices(conn, ids["ws"], include_hidden=True)}
        conn.rollback()


# ── serve 启动前的库结构检查 ──────────────────────────────────────────────────


def test_schema_problem_on_empty_old_and_current_databases(scratch_db):
    assert db_init.schema_problem(scratch_db) == "数据库还没有建表：请先运行 python cli.py db_init"
    run_schema(scratch_db, SCHEMA_V1)
    assert db_init.schema_problem(scratch_db) == db_init.SCHEMA_OUTDATED_MESSAGE
    assert db_init.SCHEMA_OUTDATED_MESSAGE == "数据库结构是旧版：请先运行 python cli.py db_init"
    run_schema(scratch_db)
    assert db_init.schema_problem(scratch_db) is None


@pytest.mark.parametrize("ddl", [
    "ALTER TABLE procure.devices DROP COLUMN login_customer_id",
    "ALTER TABLE procure.devices DROP COLUMN hidden_at",
    "ALTER TABLE procure.execution_runs DROP COLUMN scheduled_slot",
    "DROP TABLE procure.ignored_buyer_ids",
    "ALTER TABLE procure.devices ADD COLUMN bound_account_id bigint",
    "ALTER TABLE procure.buyer_accounts ADD COLUMN default_device_id bigint",
    "CREATE TABLE procure.device_pairings (code_hash text PRIMARY KEY)",
])
def test_schema_problem_spots_each_marker_of_an_old_structure(scratch_db, ddl):
    run_schema(scratch_db)
    with db.pg_conn(scratch_db, autocommit=True) as conn:
        conn.execute(ddl)
    assert db_init.schema_problem(scratch_db) == db_init.SCHEMA_OUTDATED_MESSAGE
    run_schema(scratch_db)                                      # db_init 之后恢复成当前结构
    assert db_init.schema_problem(scratch_db) is None


def test_schema_problem_uses_configured_database_by_default(clean_db):
    assert db_init.schema_problem() is None                     # 会话测试库（PC_PG_DSN）就是当前结构
    assert paths.SCHEMA_SQL.is_file() and SCHEMA_V1.is_file()
