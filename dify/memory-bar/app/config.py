"""配置：环境变量 + 项目内 .env（不引入额外依赖）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("SEEKDB_HOST", "127.0.0.1")
    port: int = int(os.getenv("SEEKDB_PORT", "2881"))
    user: str = os.getenv("SEEKDB_USER", "root")
    password: str = os.getenv("SEEKDB_PASSWORD", "")
    database: str = os.getenv("SEEKDB_DATABASE", "memory_bar")

    server_port: int = int(os.getenv("SERVER_PORT", "8787"))
    vector_dim: int = int(os.getenv("VECTOR_DIM", "512"))

    model_repo: str = os.getenv("EMBEDDING_MODEL_REPO", "Xenova/bge-small-zh-v1.5")
    model_dir: str = os.getenv(
        "EMBEDDING_MODEL_DIR", str(PROJECT_ROOT / "models" / "bge-small-zh-v1.5")
    )


settings = Settings()
