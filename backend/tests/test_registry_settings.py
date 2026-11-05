"""registry.settings：.env 解析规则（行尾注释、引号、export、空值用缺省）。全部虚构值。"""

from __future__ import annotations

import os

import pytest

from registry import settings

KEYS = ("PC_PORT", "PC_HOST", "PC_PG_DSN", "PC_WEB_DIST", "FEISHU_BASE_URL", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
        "PC_LEASE_TTL_S")


@pytest.fixture()
def clean_env(monkeypatch):
    saved = dict(os.environ)            # _load_dotenv 直接写 os.environ：测试后整体还原，免得污染别的测试
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    settings.reset_for_tests()
    try:
        yield monkeypatch
    finally:
        os.environ.clear()
        os.environ.update(saved)
        settings.reset_for_tests()


def test_dotenv_inline_comments_quotes_export_and_empty_values(clean_env, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# 注释行\n"
        "PC_PORT=8791   # 行尾注释\n"
        "export PC_LEASE_TTL_S=240\n"
        "PC_HOST=\n"                                   # 空值 = 没写，用缺省
        "PC_PG_DSN=\"dbname=demo_db # 引号里原样保留\"\n"
        "FEISHU_APP_ID='cli_demo_not_real'\n"
        "FEISHU_APP_SECRET=\n",
        encoding="utf-8",
    )
    settings._load_dotenv(env)
    s = settings.get()
    assert s.port == 8791 and s.lease_ttl_s == 240
    assert s.host == "127.0.0.1"
    assert s.pg_dsn == "dbname=demo_db # 引号里原样保留"
    assert s.feishu_app_id == "cli_demo_not_real" and s.feishu_app_secret is None
    assert s.feishu_configured is False


def test_process_env_wins_and_blank_text_settings_fall_back_to_defaults(clean_env, tmp_path):
    clean_env.setenv("PC_PORT", "8792")
    clean_env.setenv("PC_PG_DSN", "   ")
    clean_env.setenv("FEISHU_BASE_URL", "")
    env = tmp_path / ".env"
    env.write_text("PC_PORT=9999\n", encoding="utf-8")
    settings._load_dotenv(env)
    s = settings.get()
    assert s.port == 8792
    assert s.pg_dsn == "dbname=procurement"
    assert s.feishu_base_url == "https://open.feishu.cn"
