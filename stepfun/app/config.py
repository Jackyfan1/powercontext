"""配置：全部通过环境变量覆盖，默认值可直接本机运行。"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent


def _load_dotenv() -> None:
    """极简 .env 读取（避免额外依赖），已存在的环境变量优先。"""
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


class Settings:
    host: str = os.getenv("SEEKDB_HOST", "127.0.0.1")
    port: int = int(os.getenv("SEEKDB_PORT", "2881"))
    user: str = os.getenv("SEEKDB_USER", "root")
    password: str = os.getenv("SEEKDB_PASSWORD", "")
    database: str = os.getenv("SEEKDB_DATABASE", "stepseek")

    server_host: str = os.getenv("SERVER_HOST", "127.0.0.1")
    server_port: int = int(os.getenv("SERVER_PORT", "8011"))

    model_name: str = os.getenv("EMBED_MODEL", "bge-small-zh-v1.5")
    vector_dim: int = int(os.getenv("VECTOR_DIM", "512"))

    static_dir: Path = APP_DIR / "static"
    model_dir: Path = APP_DIR / "static" / "models"
    docs_dir: Path = ROOT_DIR / "docs"


settings = Settings()
