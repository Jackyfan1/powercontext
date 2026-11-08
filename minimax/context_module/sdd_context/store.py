"""seekdb 存储层：工程资料（规格 / 代码 / 测试）的导入与检索。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

import pymysql


SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       VARCHAR(191) NOT NULL PRIMARY KEY,
    kind         VARCHAR(16)  NOT NULL,
    path         VARCHAR(255) NOT NULL,
    symbol       VARCHAR(128) NOT NULL DEFAULT '',
    title        VARCHAR(255) NOT NULL DEFAULT '',
    content      MEDIUMTEXT   NOT NULL,
    line_start   INT          NOT NULL DEFAULT 0,
    line_end     INT          NOT NULL DEFAULT 0,
    content_hash CHAR(64)     NOT NULL,
    version      INT          NOT NULL DEFAULT 1,
    updated_at   DATETIME     NOT NULL
)
"""


@dataclass(frozen=True)
class Document:
    doc_id: str
    kind: str
    path: str
    symbol: str
    title: str
    content: str
    line_start: int = 0
    line_end: int = 0

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SearchHit:
    document: Document
    version: int
    score: int


class SeekDBStore:
    """基于 seekdb（MySQL 协议）的文档库。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 2881,
        user: str = "root",
        password: str = "",
        database: str = "sdd_context",
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database

    def _connect(self, with_database: bool = True) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database if with_database else None,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
        )

    def ensure_schema(self) -> None:
        with self._connect(with_database=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{self.database}` "
                    "DEFAULT CHARACTER SET utf8mb4"
                )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_TABLE)
        self.ping()

    def ping(self) -> str:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                row = cur.fetchone()
        return str(row[0]) if row else "unknown"

    def upsert(self, documents: Iterable[Document]) -> tuple[int, int]:
        """写入或更新文档，返回 (新增数, 更新数)。"""
        inserted = updated = unchanged = 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                for doc in documents:
                    cur.execute(
                        "SELECT content_hash, version FROM documents WHERE doc_id = %s",
                        (doc.doc_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        cur.execute(
                            "INSERT INTO documents (doc_id, kind, path, symbol, title, content,"
                            " line_start, line_end, content_hash, version, updated_at)"
                            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,NOW())",
                            (
                                doc.doc_id,
                                doc.kind,
                                doc.path,
                                doc.symbol,
                                doc.title,
                                doc.content,
                                doc.line_start,
                                doc.line_end,
                                doc.content_hash,
                            ),
                        )
                        inserted += 1
                    elif row[0] != doc.content_hash:
                        cur.execute(
                            "UPDATE documents SET kind=%s, path=%s, symbol=%s, title=%s,"
                            " content=%s, line_start=%s, line_end=%s, content_hash=%s,"
                            " version=version+1, updated_at=NOW() WHERE doc_id=%s",
                            (
                                doc.kind,
                                doc.path,
                                doc.symbol,
                                doc.title,
                                doc.content,
                                doc.line_start,
                                doc.line_end,
                                doc.content_hash,
                                doc.doc_id,
                            ),
                        )
                        updated += 1
                    else:
                        unchanged += 1
        return inserted, updated

    def search(
        self,
        keywords: Sequence[str],
        kinds: Sequence[str],
        limit: int = 12,
    ) -> list[SearchHit]:
        """按关键词在标题与正文中检索，SQL 过滤后按命中次数排序。"""
        terms = [term.strip().lower() for term in keywords if term and term.strip()][:8]
        if not terms or not kinds:
            return []

        like_clause = " OR ".join(["LOWER(title) LIKE %s OR LOWER(content) LIKE %s"] * len(terms))
        params: list[object] = []
        for term in terms:
            params.extend([f"%{term}%", f"%{term}%"])

        placeholders = ",".join(["%s"] * len(kinds))
        sql = (
            "SELECT doc_id, kind, path, symbol, title, content, line_start, line_end, version"
            f" FROM documents WHERE kind IN ({placeholders}) AND ({like_clause})"
            " ORDER BY kind, path LIMIT 200"
        )

        hits: list[SearchHit] = []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [*kinds, *params])
                for row in cur.fetchall():
                    doc = Document(
                        doc_id=row[0],
                        kind=row[1],
                        path=row[2],
                        symbol=row[3],
                        title=row[4],
                        content=row[5],
                        line_start=row[6],
                        line_end=row[7],
                    )
                    lowered_title = doc.title.lower()
                    lowered_content = doc.content.lower()
                    score = 0
                    for term in terms:
                        if term in lowered_title:
                            score += 3
                        score += lowered_content.count(term)
                    hits.append(SearchHit(document=doc, version=row[8], score=score))

        hits.sort(key=lambda hit: (-hit.score, hit.document.kind, hit.document.doc_id))
        return hits[:limit]

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT kind, COUNT(*) FROM documents GROUP BY kind")
                rows = cur.fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def describe(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "server_version": self.ping(),
            "documents": self.stats(),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
