"""命令入口：user_create 不在命令行里收密码（交互 / 标准输入），feishu_grant 由部署所有者给工作区授权飞书表格。"""

from __future__ import annotations

import io

import pytest

import cli
from registry import db
from services import auth
from services.errors import DomainError
from tests import factories as f
from workflows import feishu_grant


def test_user_create_reads_password_from_stdin_not_argv(clean_db, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("demo-pass-0009\n"))
    assert cli.main(["user_create", "-p", "username=demo_cli", "-p", "workspace=DEMO CLI 工作区"]) == 0
    out = capsys.readouterr()
    assert "demo-pass-0009" not in out.out and "demo_cli" in out.out
    with db.pg_conn() as conn:
        token, me = auth.login(conn, "demo_cli", "demo-pass-0009")
        assert token and me["user"]["username"] == "demo_cli"


def test_user_create_with_password_param_warns(clean_db, monkeypatch, capsys):
    cli.main(["user_create", "-p", "username=demo_cli2", "-p", "password=demo-pass-0010", "-p", "workspace=DEMO"])
    assert "shell 历史" in capsys.readouterr().err


def test_ask_password_refuses_empty_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit):
        cli._ask_password()


def test_feishu_grant_grant_list_transfer_and_revoke(conn, workspace):
    ws_a = workspace["id"]
    ws_b = f.make_workspace(conn)["id"]
    out = feishu_grant.run(workspace_id=ws_b, link="https://example-demo.feishu.cn/base/bascnDEMOGRANT01?table=tblDEMO1",
                           conn=conn)
    assert out["app_token"] == "bascnDEMOGRANT01" and out["workspace_id"] == ws_b and out["granted_by"] == "cli"
    assert feishu_grant.run(conn=conn)["grants"][0]["workspace_id"] == ws_b
    with pytest.raises(DomainError):
        feishu_grant.run(workspace_id=ws_a, app_token="bascnDEMOGRANT01", conn=conn)
    moved = feishu_grant.run(workspace_id=ws_a, app_token="bascnDEMOGRANT01", transfer="1", conn=conn)
    assert moved["previous_workspace_id"] == ws_b and moved["workspace_id"] == ws_a
    assert feishu_grant.run(app_token="bascnDEMOGRANT01", revoke="1", conn=conn)["workspace_id"] == ws_a
    assert feishu_grant.run(conn=conn)["grants"] == []
    with pytest.raises(DomainError):
        feishu_grant.run(workspace_id=999999, app_token="bascnDEMOGRANT01", conn=conn)
    with pytest.raises(DomainError):
        feishu_grant.run(app_token="bascnDEMOGRANT01", conn=conn)            # 缺工作区
    with pytest.raises(DomainError):
        feishu_grant.run(workspace_id=ws_a, link="https://example-demo.feishu.cn/wiki/wikcnDEMO0001", conn=conn)
