"""飞书表格授权：python cli.py feishu_grant -p workspace_id=2 -p app_token=bascnXXXX [-p transfer=1] | -p revoke=1 -p app_token=…

v1 §6.1 第 3 步：链接只用来定位表格，不等于授权。部署所有者的工作区（第一个工作区）绑定尚未授权的表格时自动登记；
其他工作区要用某个多维表格，由部署所有者在服务器上用本命令授权（一个表格同一时刻只归一个工作区）。
也可以给 link（多维表格链接，取其中的 app_token；知识库链接请先在控制台解析出 app_token）。不带参数时列出全部授权。
"""

from __future__ import annotations

from typing import Any

from registry import db
from services import feishu_connections, feishu_fields
from services.errors import validation


def _flag(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def run(workspace_id: int | str | None = None, app_token: str | None = None, link: str | None = None,
        transfer: Any = "0", revoke: Any = "0", *, conn=None) -> dict[str, Any]:
    """输入：工作区 id、app_token 或多维表格链接、是否从别的工作区转授、是否撤销（可选外部连接）→
    输出：授权结果 {app_token, workspace_id, granted_by, previous_workspace_id}、撤销结果，或 {grants: [...]}。

    自带事务；传入 conn 时不提交。
    """
    token = app_token
    if not token and link:
        parsed = feishu_fields.parse_link(link)
        if parsed["kind"] != "base":
            raise validation("知识库链接请先在控制台解析出 app_token，再用 -p app_token=… 授权")
        token = parsed["token"]

    def go(c) -> dict[str, Any]:
        if not token:
            rows = c.execute("SELECT app_token, workspace_id, granted_by, granted_at FROM procure.feishu_base_grants "
                             "ORDER BY workspace_id, app_token").fetchall()
            return {"grants": [{**r, "granted_at": r["granted_at"].isoformat()} for r in rows]}
        if _flag(revoke):
            return feishu_connections.revoke_base(c, token)
        if workspace_id in (None, ""):
            raise validation("请用 -p workspace_id=… 指定要授权的工作区")
        return feishu_connections.grant_base(c, int(workspace_id), token, transfer=_flag(transfer))

    if conn is not None:
        return go(conn)
    with db.pg_conn() as c:
        return go(c)
