"""路径唯一入口。"""

from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
SCHEMA_SQL = BACKEND_DIR / "refdata" / "schema.sql"
