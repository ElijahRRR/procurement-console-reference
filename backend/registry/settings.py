"""配置唯一入口：环境变量（可由 backend/.env 提供）→ 类型化的设置值。

其他模块只准 `from registry import settings` 后读 `settings.get()` 的字段，
不得自行 os.environ 读取。.env 只在首次 get() 时加载一次，已存在的环境变量优先。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from registry import paths

_loaded: "Settings | None" = None


@dataclass(frozen=True)
class Settings:
    pg_dsn: str
    host: str
    port: int
    feishu_app_id: str | None
    feishu_app_secret: str | None
    feishu_base_url: str
    session_days: int
    cookie_secure: bool
    lease_ttl_s: int
    identity_fresh_s: int
    claim_busy_retry_s: int
    pairing_ttl_s: int
    sweep_interval_s: int
    writeback_interval_s: int
    intake_check_s: int
    web_dist: Path
    login_fail_limit: int
    pair_fail_limit: int
    rate_window_s: int
    allowed_hosts: tuple[str, ...]

    @property
    def feishu_configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret)


def _load_dotenv(path: Path) -> None:
    """输入：.env 路径 → 输出：无（把未设置的 KEY=VALUE 写进 os.environ）。

    规则：# 开头的行是注释；可写 export KEY=VALUE；值可用成对的单 / 双引号括起（引号里原样保留，收尾引号
    之后可以跟「# 注释」）；不带引号的值里「空白 + #」之后是行尾注释；值为空等于没写（用缺省值）。
    已存在的环境变量优先。
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        end = value.find(value[0], 1) if value[:1] in ("\"", "'") else -1
        rest = value[end + 1:].strip() if end > 0 else None
        if rest is not None and (not rest or rest.startswith("#")):
            value = value[1:end]          # 引号里原样保留；收尾引号之后只允许空白或「# 注释」
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip()
    return raw or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def get() -> Settings:
    """输入：无 → 输出：当前进程的设置（首次调用时加载 .env）。"""
    global _loaded
    if _loaded is None:
        _load_dotenv(paths.BACKEND_DIR / ".env")
        _loaded = Settings(
            pg_dsn=_str("PC_PG_DSN", "dbname=procurement"),
            host=_str("PC_HOST", "127.0.0.1"),
            port=_int("PC_PORT", 8790),
            feishu_app_id=os.environ.get("FEISHU_APP_ID", "").strip() or None,
            feishu_app_secret=os.environ.get("FEISHU_APP_SECRET", "").strip() or None,
            feishu_base_url=_str("FEISHU_BASE_URL", "https://open.feishu.cn").rstrip("/"),
            session_days=_int("PC_SESSION_DAYS", 14),
            cookie_secure=_bool("PC_COOKIE_SECURE", False),
            lease_ttl_s=_int("PC_LEASE_TTL_S", 180),
            identity_fresh_s=_int("PC_IDENTITY_FRESH_S", 600),
            claim_busy_retry_s=_int("PC_CLAIM_BUSY_RETRY_S", 5),
            pairing_ttl_s=_int("PC_PAIRING_TTL_S", 600),
            sweep_interval_s=_int("PC_SWEEP_INTERVAL_S", 30),
            writeback_interval_s=_int("PC_WRITEBACK_INTERVAL_S", 60),
            intake_check_s=_int("PC_INTAKE_CHECK_S", 30),
            web_dist=Path(_str("PC_WEB_DIST", str(paths.REPO_DIR / "web" / "dist"))),
            login_fail_limit=_int("PC_LOGIN_FAIL_LIMIT", 10),
            pair_fail_limit=_int("PC_PAIR_FAIL_LIMIT", 20),
            rate_window_s=_int("PC_RATE_WINDOW_S", 600),
            allowed_hosts=_list("PC_ALLOWED_HOSTS"),
        )
    return _loaded


def reset_for_tests() -> None:
    """输入：无 → 输出：无（清掉缓存，下次 get() 重新读环境变量）。只给测试用。"""
    global _loaded
    _loaded = None
