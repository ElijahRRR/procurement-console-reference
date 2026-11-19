"""建表脚本的升级路径（契约 §1）：在合并前的旧结构（tests/data/schema_v1.sql）上执行当前的 refdata/schema.sql，
旧绑定回填成账号的买家号 ID、浏览器行换成新形状、配对码表删除、批次补上北京日期；脚本在旧库、新库上都能重复执行。
来源指定买家号：任务多两列、assignment_kind 多一个取值（按确切名字换 CHECK），db_init 在加列的那一次回填旧任务的指定。
另测 serve 启动前的库结构检查（workflows/db_init.schema_problem）。

每个用例用自己的临时库（conftest.temp_database），不碰会话共用的测试库。全部数据虚构。"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from registry import db, paths
from services import accounts, allocation, clock, devices, execution, orders, simulation
from tests import factories as f
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
    err = capsys.readouterr().err
    assert "已按 schema.sql 建表或升级到当前结构" in err and "已有的表不受影响" not in err   # 升级会改表、删表
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
    "ALTER TABLE procure.purchase_orders DROP COLUMN designated_customer_id",
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


# ── 来源指定买家号：两列、CHECK 换成三个取值、旧任务回填 ──────────────────────────────

KIND_CHECK = "purchase_orders_assignment_kind_check"
PAIR_CHECK = "purchase_orders_check"          # (assigned_account_id IS NULL) = (assignment_kind IS NULL)


def _order_checks(conn) -> dict:
    rows = conn.execute(
        "SELECT c.conname, c.oid::bigint AS oid, pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
        "WHERE c.conrelid = 'procure.purchase_orders'::regclass AND c.contype = 'c'").fetchall()
    return {r["conname"]: r for r in rows}


def _insert_order(conn, ws, no, **cols):
    cols = {"workspace_id": ws, "upstream_order_no": no, "status": "queued", "primary_source_kind": "feishu", **cols}
    names = list(cols)
    return conn.execute(
        f"INSERT INTO procure.purchase_orders ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) "
        "RETURNING id", list(cols.values())).fetchone()["id"]


def test_upgrade_widens_the_assignment_kind_check_by_its_exact_name(old_db):
    """陷阱：表上有两条 CHECK 都提到 assignment_kind。升级只换列级那一条（同名重建），「有账号必有分配方式」的表级
    约束还在；重复执行不重建约束。"""
    dsn, ids = old_db
    with db.pg_conn(dsn) as conn:
        before = _order_checks(conn)
        assert "source" not in before[KIND_CHECK]["def"] and PAIR_CHECK in before
        with pytest.raises(Exception):                                # 旧库写不进 source
            with conn.transaction():
                _insert_order(conn, ids["ws"], "PO-DEMO-OLD", status="assigned", assigned_account_id=ids["a_has"],
                              assignment_kind="source")
    run_schema(dsn)
    with db.pg_conn(dsn) as conn:
        checks = _order_checks(conn)
        assert "'source'" in checks[KIND_CHECK]["def"] and "'auto'" in checks[KIND_CHECK]["def"]
        assert checks[PAIR_CHECK]["def"] == before[PAIR_CHECK]["def"]
        assert checks[PAIR_CHECK]["oid"] == before[PAIR_CHECK]["oid"]              # 表级约束没被动过
        cols = {(c[0], c[1]) for c in _columns(conn)}
        assert {("purchase_orders", "designated_customer_id"), ("purchase_orders", "designated_buyer_name")} <= cols
        oid = _insert_order(conn, ids["ws"], "PO-DEMO-SRC", status="assigned", assigned_account_id=ids["a_has"],
                            assignment_kind="source", designated_customer_id="not-checked-here")   # 两列不加形状约束
        assert oid
        for bad in (dict(assigned_account_id=ids["a_has"], assignment_kind="bogus"),
                    dict(assigned_account_id=ids["a_has"], assignment_kind=None),       # 有账号无分配方式
                    dict(assigned_account_id=None, assignment_kind="source")):          # 有分配方式无账号
            with pytest.raises(Exception):
                with conn.transaction():
                    _insert_order(conn, ids["ws"], "PO-DEMO-BAD", status="assigned", **bad)
    run_schema(dsn)
    with db.pg_conn(dsn) as conn:
        again = _order_checks(conn)
        assert again[KIND_CHECK]["oid"] == checks[KIND_CHECK]["oid"]               # 已含 source：不重建、不重新校验
        assert len(again) == len(checks)                                           # 没有多出 _check1 之类的约束


def test_current_shape_database_with_the_old_two_value_check_is_upgraded_too(scratch_db):
    """列已是新形状、只有 CHECK 还是旧的两个取值（只执行了一半的升级）→ 同样换成三个取值，结构与新库一致。"""
    run_schema(scratch_db)
    with db.pg_conn(scratch_db) as conn:
        fresh = (_columns(conn), _indexes(conn), _constraints(conn))
    with db.pg_conn(scratch_db, autocommit=True) as conn:
        conn.execute(f"ALTER TABLE procure.purchase_orders DROP CONSTRAINT {KIND_CHECK}")
        conn.execute(f"ALTER TABLE procure.purchase_orders ADD CONSTRAINT {KIND_CHECK} "
                     "CHECK (assignment_kind IN ('auto', 'manual'))")
    run_schema(scratch_db)
    with db.pg_conn(scratch_db) as conn:
        assert (_columns(conn), _indexes(conn), _constraints(conn)) == fresh
    with db.pg_conn(scratch_db, autocommit=True) as conn:                          # 约束整条丢了也补得回来
        conn.execute(f"ALTER TABLE procure.purchase_orders DROP CONSTRAINT {KIND_CHECK}")
    run_schema(scratch_db)
    with db.pg_conn(scratch_db) as conn:
        assert _constraints(conn) == fresh[2]


def _seed_v1_orders(conn, ids) -> dict:
    """往旧结构里加带来源关联的任务（旧版已经把买家号两列存进了 order_sources.normalized）→ {场景: 任务 id}。"""
    ws, ws2 = ids["ws"], ids["ws2"]
    out: dict = {}

    def one(sql, params=()):
        return conn.execute(sql, params).fetchone()["id"]

    def connection(workspace, n):
        return one("INSERT INTO procure.feishu_connections (workspace_id, name, link, app_token, source_table_id, "
                   "result_table_id) VALUES (%s, %s, %s, %s, 'tblDEMO0000001', 'tblDEMO0000001') RETURNING id",
                   (workspace, f"DEMO 订单中心 {n}", "https://demo.example.invalid/base/bascnDEMO", f"bascnDEMO{n:06d}"))

    def batch(workspace, n):
        return one("INSERT INTO procure.import_batches (workspace_id, filename, file_sha256, file_bytes, status) "
                   "VALUES (%s, %s, %s, ''::bytea, 'committed') RETURNING id",
                   (workspace, f"demo-{n}.xlsx", f"demo-sha-{n}"))

    conn_a, conn_b = connection(ws, 1), connection(ws2, 2)
    batches = [batch(ws, n) for n in (1, 2, 3)]
    seq = iter(range(1, 1000))

    def link(order_id, kind, *, buyer=None, cid=None, amz=None, seen_min=0, workspace=ws, feishu_conn=conn_a,
             batch_id=None, issues=()):
        n = next(seq)
        normalized = {"upstream_order_no": f"PO-DEMO-{order_id}", "external_buyer_account": buyer,
                      "external_buyer_customer_id": cid, "external_amz_order_no": amz, "review": "approved",
                      "upstream_cancelled": False, "issues": list(issues)}
        seen = NOW - timedelta(minutes=seen_min)
        conn.execute(
            "INSERT INTO procure.order_sources (workspace_id, purchase_order_id, kind, feishu_connection_id, "
            "feishu_record_id, import_batch_id, import_row_no, snapshot, normalized, fingerprint, first_seen_at, "
            "last_seen_at) VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, 'demo-fp', %s, %s)",
            (workspace, order_id, kind, feishu_conn if kind == "feishu" else None,
             f"recDEMO{n:04d}" if kind == "feishu" else None, batch_id if kind == "file" else None,
             n if kind == "file" else None, Jsonb(normalized), seen, seen))

    def order(key, kind="feishu", workspace=ws, **cols):
        out[key] = _insert_order(conn, workspace, f"PO-DEMO-M-{key}", primary_source_kind=kind, **cols)
        return out[key]

    def attempt(order_id, *, status="succeeded", simulated=True, kind="auto", no=None):
        n = conn.execute("SELECT count(*) AS n FROM procure.purchase_attempts WHERE purchase_order_id = %s",
                         (order_id,)).fetchone()["n"]
        conn.execute(
            "INSERT INTO procure.purchase_attempts (workspace_id, purchase_order_id, attempt_no, kind, account_id, "
            "simulated, status, bj_date, finished_at, amazon_order_no) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (ws, order_id, n + 1, kind, ids["a_has"], simulated, status, NOW.date(), NOW, no))

    # ① 飞书为主：ID（小写、带空白）+ 名称 → 清洗后回填
    link(order("feishu_both"), "feishu", buyer=" 环境-DEMO-甲 ", cid=" a1demo00021 ")
    # ② 文件为主：旧批次写了买家号，新批次没写 → 取「表态且非空」里最新的那条（旧批次）
    o = order("file_blank_newer", kind="file")
    link(o, "file", buyer="环境-DEMO-乙", seen_min=60, batch_id=batches[0])
    link(o, "file", seen_min=5, batch_id=batches[1])
    # ③ 文件为主：两个批次都写了 → 最新的；同一时刻取 id 大的
    o = order("file_latest", kind="file")
    link(o, "file", buyer="环境-DEMO-旧写法", seen_min=60, batch_id=batches[0])
    link(o, "file", buyer="环境-DEMO-新写法", seen_min=5, batch_id=batches[1])
    o = order("file_same_time", kind="file")
    link(o, "file", buyer="环境-DEMO-先", seen_min=5, batch_id=batches[0])
    link(o, "file", buyer="环境-DEMO-后", seen_min=5, batch_id=batches[1])
    # ④ 带可信 AMZ 订单号的行不表态；格式不对的单号不算可信
    link(order("credible_no"), "feishu", buyer="环境-DEMO-丙", cid="A1DEMO00023", amz="111-0000000-0000023")
    link(order("malformed_no", status="conflict"), "feishu", buyer="环境-DEMO-丁", amz="重拍")
    # ⑤ 占位写法按没写；两列都空
    link(order("placeholder"), "feishu", buyer="—", cid="无")
    link(order("blank"), "feishu")
    # ⑥ 只有另一种来源写了买家号（飞书为主、文件写了）→ 不回填
    o = order("other_kind_only")
    link(o, "feishu")
    link(o, "file", buyer="环境-DEMO-戊", batch_id=batches[2])
    # ⑦ 与实时接收同一条规则：只跳过有真实采购成功的任务（和已关闭的）。已采购（没有真实成功的尝试）、已取消的
    #    照样回填——清除模拟结果、撤销取消后它们会回到分配；采购中、异常、待确认的也回填
    for st in ("purchased", "closed", "cancelled"):
        link(order(f"done_{st}", status=st), "feishu", cid="A1DEMO00024")
    for st in ("executing", "exception", "pending_review", "repurchase_pending"):
        link(order(f"open_{st}", status=st), "feishu", cid="A1DEMO00025")
    o = order("sim_purchased", status="purchased")                     # 模拟采购成功：回填
    attempt(o, no="SIM-20261118-1")
    link(o, "feishu", buyer="环境-DEMO-乙")
    o = order("real_purchased", status="purchased")                    # 真实采购成功：不回填（买家号可能是写回写的）
    attempt(o, simulated=False, no="111-0000000-0000031")
    link(o, "feishu", buyer="环境-DEMO-乙")
    o = order("external_purchased", status="purchased")                # 来源带来的外部单号不算本系统的成功：回填
    attempt(o, simulated=False, kind="external", no="111-0000000-0000032")
    link(o, "feishu", buyer="环境-DEMO-乙")
    # ⑧ 行上的 AMZ 订单号就是这笔任务登记过的单号（导出的采购结果再上传，含模拟单号）：那条关联不算表态
    o = order("export_roundtrip", kind="file", status="purchased")
    attempt(o, no="SIM-20261118-2")
    link(o, "file", buyer="环境-DEMO-回填", amz="SIM-20261118-2", batch_id=batches[2])
    o = order("export_after_source", kind="file")                       # 更早一条写了买家号 → 取它
    attempt(o, status="aborted", no="SIM-20261118-3")                   # 模拟结果已清除，单号留在尝试上
    link(o, "file", buyer="环境-DEMO-乙", seen_min=60, batch_id=batches[0])
    link(o, "file", buyer="环境-DEMO-回填", amz=" SIM-20261118-3 ", seen_min=5, batch_id=batches[1])
    # ⑨ 旧版把表格错误值存成 null + 一句问题：还原成读不出的指定；带可信单号的行照样不表态
    link(order("legacy_error_name", kind="file"), "file", batch_id=batches[2],
         issues=["买家号是表格错误值 #REF!，按没读到处理"])
    link(order("legacy_error_id"), "feishu", buyer="环境-DEMO-甲",
         issues=["买家号ID是表格错误值 #VALUE!，按没读到处理"])
    link(order("legacy_error_credible"), "feishu", amz="111-0000000-0000033",
         issues=["买家号是表格错误值 #N/A，按没读到处理"])
    link(order("legacy_other_issue"), "feishu", issues=["收货人电话是表格错误值 #N/A，按没读到处理"])
    # ⑩ 已经自动分给别的账号：回填指定，但不动分配（回填不触发分配）
    link(order("assigned_elsewhere", status="assigned", assigned_account_id=ids["a_has"], assignment_kind="auto"),
         "feishu", buyer="环境-DEMO-回填")
    # ⑪ 另一个工作区
    link(order("other_ws", workspace=ws2), "feishu", cid="A1DEMO00026", workspace=ws2, feishu_conn=conn_b)
    return out


def _designations(conn) -> dict:
    return {r["id"]: (r["designated_customer_id"], r["designated_buyer_name"]) for r in conn.execute(
        "SELECT id, designated_customer_id, designated_buyer_name FROM procure.purchase_orders").fetchall()}


BACKFILLED = {
    "feishu_both": ("A1DEMO00021", "环境-DEMO-甲"),
    "file_blank_newer": (None, "环境-DEMO-乙"),
    "file_latest": (None, "环境-DEMO-新写法"),
    "file_same_time": (None, "环境-DEMO-后"),
    "malformed_no": (None, "环境-DEMO-丁"),
    "done_purchased": ("A1DEMO00024", None), "done_cancelled": ("A1DEMO00024", None),
    "open_executing": ("A1DEMO00025", None), "open_exception": ("A1DEMO00025", None),
    "open_pending_review": ("A1DEMO00025", None), "open_repurchase_pending": ("A1DEMO00025", None),
    "sim_purchased": (None, "环境-DEMO-乙"), "external_purchased": (None, "环境-DEMO-乙"),
    "export_after_source": (None, "环境-DEMO-乙"),
    "legacy_error_name": (None, "#REF!"), "legacy_error_id": ("#VALUE!", "环境-DEMO-甲"),
    "assigned_elsewhere": (None, "环境-DEMO-回填"),
    "other_ws": ("A1DEMO00026", None),
}


def test_db_init_backfills_designations_once(old_db, capsys):
    """验收 修订二 第 7 条：旧库升级后，来源里早已填好买家号、没有真实采购成功的任务带上指定（规则与实时接收一致）；
    重复升级不改数据。"""
    dsn, ids = old_db
    with db.pg_conn(dsn) as conn:
        o = _seed_v1_orders(conn, ids)
    out = db_init.run(dsn=dsn)
    expected = BACKFILLED
    assert out["ok"] is True and out["designations_backfilled"] == len(expected)
    err = capsys.readouterr().err
    assert f"已为 {len(expected)} 笔没有真实采购成功的任务补上来源里早已填好的买家号（来源指定）；" \
           "启动服务后请点一次「重新分配」" in err
    with db.pg_conn(dsn) as conn:
        got = _designations(conn)
        assert {k: got[o[k]] for k in o} == {k: expected.get(k, (None, None)) for k in o}
        ev = {r["order_id"]: r for r in conn.execute(
            "SELECT * FROM procure.events WHERE kind = 'order.designation_changed' ORDER BY id").fetchall()}
        assert set(ev) == {o[k] for k in expected}
        e = ev[o["feishu_both"]]
        assert (e["entity"], e["entity_id"]) == ("order", o["feishu_both"])
        assert (e["actor_kind"], e["actor_id"]) == ("system", None)
        assert e["workspace_id"] == ids["ws"]
        assert e["detail"] == {"source": "upgrade", "changes": {"designated_customer_id": [None, "A1DEMO00021"],
                                                               "designated_buyer_name": [None, "环境-DEMO-甲"]}}
        assert ev[o["file_latest"]]["detail"]["changes"] == {"designated_buyer_name": [None, "环境-DEMO-新写法"]}
        assert ev[o["other_ws"]]["workspace_id"] == ids["ws2"]
        # 回填不触发分配，也不动已有的分配与更新时间
        row = conn.execute("SELECT * FROM procure.purchase_orders WHERE id = %s", (o["assigned_elsewhere"],)).fetchone()
        assert (row["status"], row["assigned_account_id"], row["assignment_kind"]) == ("assigned", ids["a_has"], "auto")
        assert conn.execute("SELECT count(*) AS n FROM procure.allocation_runs").fetchone()["n"] == 0
        # 升级之后来源又清空了指定（或所有者处理过）：重复 db_init 不得把它填回去
        conn.execute("UPDATE procure.purchase_orders SET designated_customer_id = NULL, designated_buyer_name = NULL "
                     "WHERE id = %s", (o["feishu_both"],))
        events = conn.execute("SELECT count(*) AS n FROM procure.events").fetchone()["n"]
    again = db_init.run(dsn=dsn)
    assert again["designations_backfilled"] == 0
    with db.pg_conn(dsn) as conn:
        after = _designations(conn)
        assert after[o["feishu_both"]] == (None, None)
        assert {k: after[o[k]] for k in o if k != "feishu_both"} == {
            k: expected.get(k, (None, None)) for k in o if k != "feishu_both"}
        assert conn.execute("SELECT count(*) AS n FROM procure.events").fetchone()["n"] == events
    capsys.readouterr()


def test_db_init_on_a_fresh_or_current_database_backfills_nothing(scratch_db, capsys):
    """新库（列是建表时就有的）与已是当前结构的库：db_init 不回填、不记事件。"""
    first = db_init.run(dsn=scratch_db)
    assert first["designations_backfilled"] == 0
    with db.pg_conn(scratch_db) as conn:
        ws = conn.execute("INSERT INTO procure.workspaces (name) VALUES ('DEMO 工作区') RETURNING id").fetchone()["id"]
        cid = conn.execute(
            "INSERT INTO procure.feishu_connections (workspace_id, name, link, app_token, source_table_id, "
            "result_table_id) VALUES (%s, 'DEMO', 'https://demo.example.invalid/base/x', 'bascnDEMO000009', "
            "'tblDEMO0000001', 'tblDEMO0000001') RETURNING id", (ws,)).fetchone()["id"]
        oid = _insert_order(conn, ws, "PO-DEMO-FRESH")
        conn.execute(
            "INSERT INTO procure.order_sources (workspace_id, purchase_order_id, kind, feishu_connection_id, "
            "feishu_record_id, snapshot, normalized, fingerprint) VALUES (%s, %s, 'feishu', %s, 'recDEMO9001', "
            "'{}'::jsonb, %s, 'demo-fp')",
            (ws, oid, cid, Jsonb({"upstream_order_no": "PO-DEMO-FRESH", "external_buyer_customer_id": "A1DEMO00030"})))
    assert db_init.run(dsn=scratch_db)["designations_backfilled"] == 0
    with db.pg_conn(scratch_db) as conn:
        assert _designations(conn)[oid] == (None, None)                     # 当前结构的库里，指定只由接收写入
        assert conn.execute("SELECT count(*) AS n FROM procure.events").fetchone()["n"] == 0
        assert db_init.backfill_designations(conn) == 1                     # 函数本身可单独调用（幂等：只填空的）
        assert db_init.backfill_designations(conn) == 0
        assert _designations(conn)[oid] == ("A1DEMO00030", None)
    capsys.readouterr()


def test_db_init_rolls_back_script_and_backfill_together(old_db, monkeypatch, capsys):
    """回填只在加列的那一次做，所以建表脚本与回填必须同一个事务：回填出错 → 结构仍是旧版、什么都没写；
    下次 db_init 从头再来，照样回填。"""
    dsn, ids = old_db
    with db.pg_conn(dsn) as conn:
        _seed_v1_orders(conn, ids)
    real = db_init.backfill_designations

    def broken(conn):
        real(conn)                                       # 回填已经写了行，然后出错
        raise RuntimeError("DEMO 回填中途出错")

    monkeypatch.setattr(db_init, "backfill_designations", broken)
    with pytest.raises(RuntimeError):
        db_init.run(dsn=dsn)
    assert db_init.schema_problem(dsn) == db_init.SCHEMA_OUTDATED_MESSAGE
    with db.pg_conn(dsn) as conn:
        assert conn.execute("SELECT to_regclass('procure.device_pairings') AS t").fetchone()["t"] is not None
        cols = {(c[0], c[1]) for c in _columns(conn)}
        assert ("purchase_orders", "designated_customer_id") not in cols
        assert conn.execute("SELECT count(*) AS n FROM procure.events WHERE kind = 'order.designation_changed'"
                            ).fetchone()["n"] == 0
    monkeypatch.undo()
    assert db_init.run(dsn=dsn)["designations_backfilled"] == len(BACKFILLED)
    capsys.readouterr()


# ── 升级之后回到分配的任务（清除模拟结果、撤销取消）：带着来源指定，不落到别的账号 ──────────────────────

LEGACY_ORDER_COLS = ("asin", "quantity", "unit_price", "price_cap", "order_time", "ship_days",
                     "suggested_purchase_date", "recipient_name", "address1", "city", "state", "postal_code", "phone")


def _legacy_world(conn) -> dict:
    """旧结构里：账号 甲 / 乙（都登记了买家号 ID），一台绑定 甲 的设备，一个飞书连接，一个已提交的文件批次。"""
    def one(sql, params=()):
        return conn.execute(sql, params).fetchone()["id"]

    ws = one("INSERT INTO procure.workspaces (name) VALUES ('DEMO 工作区') RETURNING id")
    conn.execute("INSERT INTO procure.workspace_settings (workspace_id) VALUES (%s)", (ws,))
    acc = {key: one("INSERT INTO procure.buyer_accounts (workspace_id, name, amazon_customer_id) "
                    "VALUES (%s, %s, %s) RETURNING id", (ws, name, cid))
           for key, name, cid in (("jia", "环境-DEMO-甲", "A1DEMO00001"), ("yi", "环境-DEMO-乙", "A1DEMO00002"))}
    dev = one("INSERT INTO procure.devices (workspace_id, name, token_hash, bound_account_id) "
              "VALUES (%s, 'Mac-DEMO-甲', 'demo-hash-legacy', %s) RETURNING id", (ws, acc["jia"]))
    con = one("INSERT INTO procure.feishu_connections (workspace_id, name, link, app_token, source_table_id, "
              "result_table_id) VALUES (%s, 'DEMO', 'https://demo.example.invalid/base/x', 'bascnDEMO000077', "
              "'tblDEMO0000001', 'tblDEMO0000001') RETURNING id", (ws,))
    batch = one("INSERT INTO procure.import_batches (workspace_id, filename, file_sha256, file_bytes, status) "
                "VALUES (%s, 'demo-legacy.xlsx', 'demo-sha-legacy', ''::bytea, 'committed') RETURNING id", (ws,))
    return {"ws": ws, "dev": dev, "con": con, "batch": batch, **acc}


def _legacy_order(conn, w: dict, key: str, *, status: str, kind: str = "feishu", account: int | None = None,
                  normalized_over: dict | None = None, **rec_kw) -> int:
    """旧版接收留下的任务 + 来源关联（normalized 即规范化记录的 JSON 形状）。"""
    rec = f.make_record(upstream_order_no=f"PO-DEMO-UP-{key}", suggested_purchase_date=clock.bj_today(), **rec_kw)
    cols = ["workspace_id", "upstream_order_no", *LEGACY_ORDER_COLS, "suggested_date_basis", "country",
            "review_status", "review_source", "review_value", "status", "primary_source_kind", "assigned_account_id",
            "assignment_kind", "assigned_at"]
    vals = [w["ws"], rec.upstream_order_no, *[getattr(rec, c) for c in LEGACY_ORDER_COLS], "source", "US",
            "approved", kind, "通过", status, kind, account, "auto" if account else None, NOW if account else None]
    oid = conn.execute(f"INSERT INTO procure.purchase_orders ({', '.join(cols)}) "
                       f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id", vals).fetchone()["id"]
    conn.execute(
        "INSERT INTO procure.order_sources (workspace_id, purchase_order_id, kind, feishu_connection_id, "
        "feishu_record_id, import_batch_id, import_row_no, snapshot, normalized, fingerprint) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, 'demo-fp')",
        (w["ws"], oid, kind, w["con"] if kind == "feishu" else None, f"recDEMOUP-{key}" if kind == "feishu" else None,
         w["batch"] if kind == "file" else None, oid + 1 if kind == "file" else None,
         Jsonb({**rec.to_json(), **(normalized_over or {})})))
    return oid


def _legacy_simulated_purchase(conn, w: dict, order_id: int) -> None:
    """旧版 甲 的设备把这笔任务模拟采购成功（批次已结束）。"""
    run = conn.execute(
        "INSERT INTO procure.execution_runs (workspace_id, account_id, device_id, trigger, simulated, started_at, "
        "heartbeat_at, lease_expires_at, ended_at, end_reason) VALUES (%s, %s, %s, 'manual', true, %s, %s, %s, %s, "
        "'finished') RETURNING id",
        (w["ws"], w["jia"], w["dev"], NOW, NOW, NOW + timedelta(seconds=180), NOW)).fetchone()["id"]
    att = conn.execute(
        "INSERT INTO procure.purchase_attempts (workspace_id, purchase_order_id, attempt_no, kind, account_id, "
        "device_id, run_id, simulated, status, bj_date, claimed_at, submitted_at, finished_at, amazon_order_no) "
        "VALUES (%s, %s, 1, 'auto', %s, %s, %s, true, 'succeeded', %s, %s, %s, %s, 'SIM-20261118-1') RETURNING id",
        (w["ws"], order_id, w["jia"], w["dev"], run, clock.bj_today(), NOW, NOW, NOW)).fetchone()["id"]
    conn.execute("UPDATE procure.purchase_orders SET purchased_attempt_id = %s, current_attempt_id = %s WHERE id = %s",
                 (att, att, order_id))


def _state(conn, order_id: int) -> dict:
    return dict(conn.execute(
        "SELECT status, assigned_account_id, assignment_kind, designated_customer_id, designated_buyer_name, "
        "status_reason FROM procure.purchase_orders WHERE id = %s", (order_id,)).fetchone())


def test_upgrade_keeps_the_designation_of_tasks_that_come_back(scratch_db, _clock, capsys):
    """旧库：来源写了买家号 乙，旧版却自动分给 甲 并模拟采购成功；另一笔来源写了 乙 的买家号 ID、在旧控制台取消了。
    升级 → 清除模拟结果 / 撤销取消 → 重新分配：两笔都带着指定分给 乙（kind source），绝不以自动分配落在 甲。"""
    run_schema(scratch_db, SCHEMA_V1)
    with db.pg_conn(scratch_db) as conn:
        w = _legacy_world(conn)
        sim = _legacy_order(conn, w, "SIM", status="purchased", account=w["jia"], external_buyer_account="环境-DEMO-乙")
        _legacy_simulated_purchase(conn, w, sim)
        can = _legacy_order(conn, w, "CAN", status="cancelled", external_buyer_customer_id="A1DEMO00002")
    assert db_init.run(dsn=scratch_db)["designations_backfilled"] == 2
    capsys.readouterr()
    with db.pg_conn(scratch_db) as conn:
        assert simulation.reset(conn, w["ws"], None)["orders_restored"] == [sim]
        orders.uncancel(conn, w["ws"], can, "DEMO 撤销取消", actor_user_id=None)
        allocation.allocate(conn, w["ws"], trigger="manual")
        a, b = _state(conn, sim), _state(conn, can)
    assert (a["designated_customer_id"], a["designated_buyer_name"]) == (None, "环境-DEMO-乙")
    assert (b["designated_customer_id"], b["designated_buyer_name"]) == ("A1DEMO00002", None)
    for got in (a, b):
        assert (got["status"], got["assigned_account_id"], got["assignment_kind"]) == ("assigned", w["yi"], "source")


def test_upgrade_turns_legacy_error_values_into_unreadable_designations(scratch_db, _clock, capsys):
    """旧版把买家号两列的表格错误值存成 null + 一句问题。升级后与当前版本一样：一个读不出的指定，任务留在待分配并
    写明原因，不会被当成「没写」而自动分给 甲（文件来源不会再读一遍，升级时不补就永远补不上）。"""
    run_schema(scratch_db, SCHEMA_V1)
    with db.pg_conn(scratch_db) as conn:
        w = _legacy_world(conn)
        ferr = _legacy_order(conn, w, "FERR", status="assigned", account=w["jia"], kind="file",
                             normalized_over={"external_buyer_account": None,
                                              "issues": ["买家号是表格错误值 #REF!，按没读到处理"]})
        ierr = _legacy_order(conn, w, "IERR", status="queued",
                             normalized_over={"external_buyer_customer_id": None,
                                              "issues": ["买家号ID是表格错误值 #VALUE!，按没读到处理"]})
    assert db_init.run(dsn=scratch_db)["designations_backfilled"] == 2
    capsys.readouterr()
    with db.pg_conn(scratch_db) as conn:
        allocation.allocate(conn, w["ws"], trigger="manual")
        a, b = _state(conn, ferr), _state(conn, ierr)
    assert (a["designated_customer_id"], a["designated_buyer_name"]) == (None, "#REF!")
    assert (b["designated_customer_id"], b["designated_buyer_name"]) == ("#VALUE!", None)
    assert (a["status"], a["assigned_account_id"]) == ("queued", None)
    assert (b["status"], b["assigned_account_id"]) == ("queued", None)
    assert a["status_reason"] == "来源的买家号是表格错误值 #REF!，读不出指定的买家号；在来源里改正后会重新分配"
    assert b["status_reason"] == "来源的买家号 ID 是表格错误值 #VALUE!，读不出指定的买家号；在来源里改正后会重新分配"


def test_schema_problem_uses_configured_database_by_default(clean_db):
    assert db_init.schema_problem() is None                     # 会话测试库（PC_PG_DSN）就是当前结构
    assert paths.SCHEMA_SQL.is_file() and SCHEMA_V1.is_file()
