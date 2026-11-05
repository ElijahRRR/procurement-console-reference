"""唯一命令入口。

用法（在 backend/ 目录内）：
    python cli.py db_init
    python cli.py user_create -p username=david -p password=*** -p workspace="我的工作区"
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

COMMANDS = ("db_init", "user_create", "serve", "allocate", "sweep", "feishu_intake", "feishu_writeback")


def _parse_params(items: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"参数格式应为 key=value：{item}")
        key, _, value = item.partition("=")
        params[key.strip()] = value
    return params


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="采购控制台命令入口")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("-p", "--param", action="append", default=[], help="key=value，可重复")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    params = _parse_params(args.param)

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
