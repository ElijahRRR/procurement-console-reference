"""唯一命令入口。

用法（在 backend/ 目录内）：
    python cli.py db_init                     # 库不存在时先建库，再建表（幂等）
    python cli.py user_create -p username=david -p workspace="我的工作区"     # 密码交互输入（不进命令历史）
    python cli.py feishu_grant -p workspace_id=2 -p app_token=bascn…        # 把飞书多维表格授权给工作区
    python cli.py serve                       # HTTP 服务 + 后台调度
    python cli.py allocate [-p workspace_id=1]
    python cli.py sweep
    python cli.py feishu_intake [-p connection_id=1]
    python cli.py feishu_writeback

每个命令对应 workflows/<名字>.py 的 run(**params)；serve 例外（见 scheduler.py）。
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys

COMMANDS = ("db_init", "user_create", "serve", "allocate", "sweep", "feishu_intake", "feishu_writeback",
            "feishu_grant")
#: 这些第三方库的 INFO 日志会打出带多维表格 token 的请求地址（v1 §6.3：表格标识不进日志）
QUIET_LOGGERS = ("httpx", "httpcore")


def _parse_params(items: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"参数格式应为 key=value：{item}")
        key, _, value = item.partition("=")
        params[key.strip()] = value
    return params


def _ask_password() -> str:
    """输入：无 → 输出：交互输入并确认的密码（不回显）；标准输入不是终端时读第一行（脚本用：echo … | python cli.py …）。"""
    import getpass

    if not sys.stdin.isatty():
        line = sys.stdin.readline().rstrip("\r\n")
        if not line:
            raise SystemExit("没有从标准输入读到密码")
        return line
    first = getpass.getpass("新用户密码：")
    second = getpass.getpass("再输入一次：")
    if first != second:
        raise SystemExit("两次输入的密码不一致")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="采购控制台命令入口")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("-p", "--param", action="append", default=[], help="key=value，可重复")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    params = _parse_params(args.param)
    if args.command == "user_create" and "password" in params:
        print("提示：命令行里的密码会留在 shell 历史和进程列表里；以后请省略 -p password，按提示输入。", file=sys.stderr)
    if args.command == "user_create" and "password" not in params:
        params["password"] = _ask_password()

    if args.command == "serve":
        import scheduler

        scheduler.serve(**params)
        return 0

    module = importlib.import_module(f"workflows.{args.command}")
    summary = module.run(**params)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
