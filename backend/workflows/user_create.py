"""建控制台用户：python cli.py user_create -p username=demo [-p workspace="工作区名" | -p workspace_id=1]。

密码不要写在命令行里（会留在 shell 历史与进程列表）：cli.py 在没给 password 时交互输入两次（标准输入不是终端时读第一行）。新建的工作区要用飞书表格时，由部署所有者用 python cli.py feishu_grant 授权。
"""

from __future__ import annotations

from typing import Any

from registry import db
from services import auth


def run(username: str, password: str, workspace: str | None = None, workspace_id: int | str | None = None,
        role: str = "owner", *, conn=None) -> dict[str, Any]:
    """输入：用户名、密码、新工作区名或已有工作区 id、角色（缺省 owner；可选外部连接）→ 输出：{user, workspace, role}。

    给 workspace_id 时加入该工作区，否则新建工作区（含默认设置）。自带事务；传入 conn 时不提交。输出不含密码。
    """
    ws_id = int(workspace_id) if workspace_id not in (None, "") else None
    if conn is not None:
        return auth.create_user(conn, username, password, workspace_id=ws_id, workspace_name=workspace, role=role)
    with db.pg_conn() as c:
        return auth.create_user(c, username, password, workspace_id=ws_id, workspace_name=workspace, role=role)
