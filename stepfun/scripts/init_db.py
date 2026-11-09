"""初始化 seekdb 数据库与表结构。

    python scripts/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> int:
    print(f"连接 seekdb {settings.host}:{settings.port} …")
    version = db.seekdb_version()
    print(f"版本：{version}")

    db.init_database()
    print(f"数据库 `{settings.database}` 与表结构就绪")

    for row in db.query_all("SHOW TABLES"):
        name = list(row.values())[0]
        count = db.query_one(f"SELECT COUNT(*) AS c FROM `{name}`")["c"]
        print(f"  {name:<12} {count} 行")

    print("索引：")
    for idx in db.index_info():
        print(f"  {idx['name']:<10} {idx['type']:<10} {', '.join(idx['columns'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
