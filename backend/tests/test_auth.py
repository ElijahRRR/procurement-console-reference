"""控制台用户与会话：初始化、登录、会话过期、退出、建用户命令。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from registry import settings
from services import auth, clock
from services.errors import DomainError
from workflows import user_create

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _frozen(clean_db):
    clock.freeze(NOW)
    yield
    clock.unfreeze()


def test_hash_and_verify_password():
    h = auth.hash_password("demo-pass-123")
    assert h.startswith("scrypt$16384$8$1$") and len(h.split("$")) == 6
    assert auth.verify_password("demo-pass-123", h)
    assert not auth.verify_password("demo-pass-124", h)
    assert auth.hash_password("demo-pass-123") != h            # 每次加盐不同
    for bad in ("x", "", "scrypt$1$2", "bcrypt$16384$8$1$AAAA$BBBB", "scrypt$abc$8$1$AAAA$BBBB"):
        assert not auth.verify_password("demo-pass-123", bad)


def test_setup_owner_only_when_no_users(conn):
    assert auth.needs_setup(conn) is True
    me = auth.setup_owner(conn, "demo_owner", "demo-pass-123", "DEMO 工作区")
    assert me["user"]["username"] == "demo_owner" and me["workspace"]["name"] == "DEMO 工作区"
    assert me["role"] == "owner"
    ws = me["workspace"]["id"]
    assert conn.execute("SELECT 1 FROM procure.workspace_settings WHERE workspace_id = %s", (ws,)).fetchone()
    assert conn.execute("SELECT role FROM procure.workspace_members WHERE workspace_id = %s AND user_id = %s",
                        (ws, me["user"]["id"])).fetchone()["role"] == "owner"
    assert auth.needs_setup(conn) is False
    with pytest.raises(DomainError) as e:
        auth.setup_owner(conn, "demo_other", "demo-pass-123", "另一个")
    assert e.value.code == "FORBIDDEN"


def test_login_success_and_generic_failure(conn):
    auth.setup_owner(conn, "demo_owner", "demo-pass-123", "DEMO 工作区")
    token, me = auth.login(conn, " demo_owner ", "demo-pass-123")
    assert isinstance(token, str) and len(token) >= 32 and me["user"]["username"] == "demo_owner"
    stored = conn.execute("SELECT token_hash, expires_at FROM procure.sessions").fetchone()
    assert stored["token_hash"] != token and len(stored["token_hash"]) == 64
    assert stored["expires_at"] == NOW + timedelta(days=settings.get().session_days)
    messages = set()
    for user, pw in (("demo_owner", "wrong-pass-1"), ("nobody", "demo-pass-123"), ("", "")):
        with pytest.raises(DomainError) as e:
            auth.login(conn, user, pw)
        assert e.value.code == "UNAUTHORIZED" and e.value.http_status == 401
        messages.add(e.value.message)
    assert messages == {"用户名或密码不正确"}


def test_session_me_touch_expiry_and_logout(conn):
    auth.setup_owner(conn, "demo_owner", "demo-pass-123", "DEMO 工作区")
    token, _ = auth.login(conn, "demo_owner", "demo-pass-123")
    clock.freeze(NOW + timedelta(hours=1))
    me = auth.session_me(conn, token)
    assert me == {"user": {"id": me["user"]["id"], "username": "demo_owner", "display_name": "demo_owner"},
                  "workspace": {"id": me["workspace"]["id"], "name": "DEMO 工作区"}, "role": "owner"}
    seen = conn.execute("SELECT last_seen_at FROM procure.sessions").fetchone()["last_seen_at"]
    assert seen == NOW + timedelta(hours=1)
    assert auth.session_me(conn, "not-a-token") is None and auth.session_me(conn, None) is None
    clock.freeze(NOW + timedelta(days=settings.get().session_days, seconds=1))
    assert auth.session_me(conn, token) is None
    clock.freeze(NOW)
    token2, _ = auth.login(conn, "demo_owner", "demo-pass-123")
    assert auth.session_me(conn, token2) is not None
    auth.logout(conn, token2)
    assert auth.session_me(conn, token2) is None
    auth.logout(conn, "unknown-token")


def test_disabled_user_cannot_login_and_session_dies(conn):
    me = auth.setup_owner(conn, "demo_owner", "demo-pass-123", "DEMO 工作区")
    token, _ = auth.login(conn, "demo_owner", "demo-pass-123")
    conn.execute("UPDATE procure.users SET disabled_at = %s WHERE id = %s", (NOW, me["user"]["id"]))
    assert auth.session_me(conn, token) is None
    with pytest.raises(DomainError):
        auth.login(conn, "demo_owner", "demo-pass-123")


def test_create_user_validation_and_join_workspace(conn):
    owner = auth.create_user(conn, "demo_a", "demo-pass-123", workspace_name="DEMO A")
    with pytest.raises(DomainError) as e:
        auth.create_user(conn, "demo_a", "demo-pass-123")
    assert e.value.code == "CONFLICT"
    with pytest.raises(DomainError) as e:
        auth.create_user(conn, "demo_b", "short")
    assert e.value.code == "VALIDATION"
    with pytest.raises(DomainError):
        auth.create_user(conn, "has space", "demo-pass-123")
    with pytest.raises(DomainError) as e:
        auth.create_user(conn, "demo_c", "demo-pass-123", workspace_id=999999)
    assert e.value.code == "NOT_FOUND"
    member = auth.create_user(conn, "demo_m", "demo-pass-123", workspace_id=owner["workspace"]["id"], role="member")
    assert member["workspace"] == owner["workspace"] and member["role"] == "member"
    _, me = auth.login(conn, "demo_m", "demo-pass-123")
    assert me["workspace"]["id"] == owner["workspace"]["id"]
    default = auth.create_user(conn, "demo_d", "demo-pass-123")
    assert default["workspace"]["name"] == "demo_d 的工作区"


def test_user_create_workflow_commits_in_own_transaction(conn):
    conn.commit()
    out = user_create.run("demo_cli", "demo-pass-123", workspace="DEMO CLI 工作区")
    assert out["user"]["username"] == "demo_cli" and "password" not in str(out)
    row = conn.execute("SELECT username FROM procure.users WHERE username = 'demo_cli'").fetchone()
    assert row is not None
    joined = user_create.run("demo_cli2", "demo-pass-123", workspace_id=str(out["workspace"]["id"]))
    assert joined["workspace"]["id"] == out["workspace"]["id"]
