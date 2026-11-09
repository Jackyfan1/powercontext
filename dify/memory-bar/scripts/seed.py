"""写入风味卡库与演示客人「demo」的历史记忆。

    python scripts/seed.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.embedding import Embedder  # noqa: E402
from app.seed import seed_demo, seed_flavors  # noqa: E402


def main() -> int:
    db.init_database()
    embedder = Embedder(settings.model_dir, dim=settings.vector_dim, repo=settings.model_repo)
    print(f"编码模式：{embedder.mode}（{embedder.model_name} / {settings.vector_dim} 维）")

    flavors = seed_flavors(embedder)
    demo = seed_demo(embedder, "demo")
    print(f"新增风味卡：{flavors} 张（库内共 {db.count_memories(db.LIBRARY_USER, 'flavor')} 张）")
    print(f"新增演示记忆：{demo} 条（demo 共 {db.count_memories('demo', 'drop')} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
