"""测试与本机配置隔离（NEW-DOC-5）与过期的接口说明（NEW-DOC-11）。全部虚构值。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from registry import paths, settings
from tests import conftest

BACKEND = Path(__file__).resolve().parent.parent


def test_local_dotenv_is_not_loaded_during_tests(monkeypatch, tmp_path):
    """本机 backend/.env 里调过的参数（如 PC_LEASE_TTL_S）不进测试：测试只认缺省设置。"""
    env = tmp_path / ".env"
    env.write_text("PC_LEASE_TTL_S=240\nPC_PORT=8793\n", encoding="utf-8")
    monkeypatch.setattr(paths, "BACKEND_DIR", tmp_path)             # settings.get() 读 BACKEND_DIR/.env
    monkeypatch.setattr(conftest, "BLOCKED_DOTENV", env.resolve(), raising=False)
    saved = dict(os.environ)
    for k in ("PC_LEASE_TTL_S", "PC_PORT"):
        os.environ.pop(k, None)
    settings.reset_for_tests()
    try:
        s = settings.get()
        assert s.lease_ttl_s == 180 and s.port == 8790
    finally:
        os.environ.clear()
        os.environ.update(saved)
        settings.reset_for_tests()


def test_other_dotenv_files_still_parse(tmp_path):
    """屏蔽只针对本机 backend/.env：settings 的 .env 解析规则照常可测。"""
    env = tmp_path / ".env"
    env.write_text("PC_DEMO_ONLY_FOR_TEST=1\n", encoding="utf-8")
    try:
        settings._load_dotenv(env)
        assert os.environ.get("PC_DEMO_ONLY_FOR_TEST") == "1"
    finally:
        os.environ.pop("PC_DEMO_ONLY_FOR_TEST", None)


def test_shell_tunables_do_not_reach_the_test_session():
    """shell 里的 PC_* / FEISHU_* 调参在加载测试基座时就被清掉（PC_TEST_ADMIN_DSN 保留）。"""
    env = {**os.environ, "PC_LEASE_TTL_S": "240", "FEISHU_APP_ID": "cli_demo_not_real",
           "PC_TEST_ADMIN_DSN": os.environ.get("PC_TEST_ADMIN_DSN", "dbname=postgres")}
    code = ("import os, tests.conftest; from registry import settings; s = settings.get(); "
            "print(s.lease_ttl_s, s.feishu_app_id, 'PC_TEST_ADMIN_DSN' in os.environ)")
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True,
                         timeout=60, check=True).stdout.split()
    assert out == ["180", "None", "True"]


def test_route_and_service_docstrings_are_not_stale():
    """契约 §8 已列出的接口不再写成「契约之外的补充接口」；导入模板是工作区共用的，不叫「个人模板」。"""
    stale = re.compile("之外的补充接口|个人模板")
    hits = [str(p.relative_to(BACKEND)) for d in ("server", "services") for p in (BACKEND / d).rglob("*.py")
            if stale.search(p.read_text(encoding="utf-8"))]
    assert hits == []
