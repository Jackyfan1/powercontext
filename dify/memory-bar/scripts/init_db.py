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
        table = list(row.values())[0]
        count = db.query_one(f"SELECT COUNT(*) AS c FROM `{table}`")["c"]
        print(f"  {table:<10} {count} 行")

    print("memories 索引：")
    for index in db.index_info():
        print(f"  {index['name']:<16} {index['type']:<10} {', '.join(index['columns'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
