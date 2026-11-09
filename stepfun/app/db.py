"""seekdb 数据层：建库建表、集合/文档/分块/会话的读写与混合检索。

所有检索（向量、全文、混合）都在 seekdb 内通过一条 SQL 完成：
  - 向量相似度：cosine_distance(embedding, query_vec)，配合 HNSW 向量索引
  - 全文相关度：MATCH(title, content) AGAINST (query)，ngram 分词的全文索引
混合模式在 SQL 内对两路分数加权求和后排序，避免把候选集拉回应用层再算。
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from datetime import datetime
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from .config import settings

_POOL: queue.Queue[pymysql.connections.Connection] = queue.Queue(maxsize=8)
_POOL_LOCK = threading.Lock()


def _new_connection(database: str | None = None) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=5,
        read_timeout=60,
        write_timeout=60,
    )


def get_conn() -> pymysql.connections.Connection:
    try:
        conn = _POOL.get_nowait()
        conn.ping(reconnect=True)
        return conn
    except queue.Empty:
        pass
    try:
        return _new_connection(settings.database)
    except pymysql.err.OperationalError as exc:
        # 数据库尚未创建（首次 init）时先连到实例级连接
        if exc.args and exc.args[0] == 1049:
            return _new_connection(None)
        raise


def release_conn(conn: pymysql.connections.Connection) -> None:
    try:
        _POOL.put_nowait(conn)
    except queue.Full:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


class _Cursor:
    """with 语法糖：自动归还连接。"""

    def __enter__(self):
        self.conn = get_conn()
        self.cur = self.conn.cursor()
        return self.cur

    def __exit__(self, exc_type, exc, tb):
        try:
            self.cur.close()
        finally:
            release_conn(self.conn)


def execute(sql: str, args: Any = None) -> int:
    with _Cursor() as cur:
        return cur.execute(sql, args)


def query_all(sql: str, args: Any = None) -> list[dict]:
    with _Cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def query_one(sql: str, args: Any = None) -> dict | None:
    rows = query_all(sql, args)
    return rows[0] if rows else None


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS collections (
      id VARCHAR(64) NOT NULL,
      name VARCHAR(128) NOT NULL,
      description VARCHAR(500) NOT NULL DEFAULT '',
      model VARCHAR(128) NOT NULL,
      dim INT NOT NULL,
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
      id VARCHAR(64) NOT NULL,
      collection_id VARCHAR(64) NOT NULL,
      title VARCHAR(255) NOT NULL,
      source VARCHAR(255) NOT NULL DEFAULT '',
      content_hash CHAR(64) NOT NULL DEFAULT '',
      char_count INT NOT NULL DEFAULT 0,
      chunk_count INT NOT NULL DEFAULT 0,
      created_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      KEY idx_doc_collection (collection_id, created_at)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS chunks (
      id BIGINT NOT NULL AUTO_INCREMENT,
      collection_id VARCHAR(64) NOT NULL,
      doc_id VARCHAR(64) NOT NULL,
      seq INT NOT NULL,
      title VARCHAR(255) NOT NULL DEFAULT '',
      content TEXT NOT NULL,
      meta JSON,
      embedding VECTOR({settings.vector_dim}),
      PRIMARY KEY (id),
      VECTOR INDEX vidx (embedding) WITH (distance=cosine, type=hnsw, m=16, ef_construction=200),
      FULLTEXT INDEX fidx (title, content) WITH PARSER ngram,
      KEY idx_chunk_collection (collection_id, doc_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
      id VARCHAR(64) NOT NULL,
      collection_id VARCHAR(64) NOT NULL DEFAULT '',
      title VARCHAR(255) NOT NULL DEFAULT '',
      state JSON,
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      KEY idx_session_collection (collection_id)
    )
    """,
]


def init_database() -> None:
    """建库 + 建表，可重复执行。"""
    with _POOL_LOCK:
        conn = _new_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{settings.database}` "
                    "DEFAULT CHARACTER SET utf8mb4"
                )
                cur.execute(f"USE `{settings.database}`")
                for ddl in DDL_STATEMENTS:
                    cur.execute(ddl)
        finally:
            conn.close()


def seekdb_version() -> str:
    row = query_one("SELECT VERSION() AS v")
    return row["v"] if row else "unknown"


def index_info() -> list[dict]:
    rows = query_all("SHOW INDEX FROM chunks")
    result = {}
    for row in rows:
        key = row["Key_name"]
        item = result.setdefault(
            key,
            {"name": key, "type": row["Index_type"], "columns": [], "visible": row["Visible"]},
        )
        item["columns"].append(row["Column_name"])
    return list(result.values())


def analyze() -> None:
    execute("ANALYZE TABLE chunks")


# --------------------------------------------------------------------------- #
# collections
# --------------------------------------------------------------------------- #

def list_collections() -> list[dict]:
    rows = query_all(
        """
        SELECT c.id, c.name, c.description, c.model, c.dim, c.created_at, c.updated_at,
               (SELECT COUNT(*) FROM documents d WHERE d.collection_id = c.id) AS doc_count,
               (SELECT COUNT(*) FROM chunks k WHERE k.collection_id = c.id) AS chunk_count
        FROM collections c
        ORDER BY c.created_at ASC
        """
    )
    for r in rows:
        r["created_at"] = r["created_at"].isoformat(sep=" ") if r["created_at"] else None
        r["updated_at"] = r["updated_at"].isoformat(sep=" ") if r["updated_at"] else None
    return rows


def create_collection(name: str, description: str = "", model: str | None = None) -> dict:
    collection_id = "col_" + uuid.uuid4().hex[:12]
    now = datetime.now()
    execute(
        "INSERT INTO collections (id, name, description, model, dim, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (collection_id, name, description, model or settings.model_name, settings.vector_dim, now, now),
    )
    return get_collection(collection_id)  # type: ignore[return-value]


def get_collection(collection_id: str) -> dict | None:
    for item in list_collections():
        if item["id"] == collection_id:
            return item
    return None


def delete_collection(collection_id: str) -> None:
    execute("DELETE FROM chunks WHERE collection_id = %s", (collection_id,))
    execute("DELETE FROM documents WHERE collection_id = %s", (collection_id,))
    execute("DELETE FROM sessions WHERE collection_id = %s", (collection_id,))
    execute("DELETE FROM collections WHERE id = %s", (collection_id,))


# --------------------------------------------------------------------------- #
# documents & chunks
# --------------------------------------------------------------------------- #

def find_document_by_hash(collection_id: str, content_hash: str) -> dict | None:
    return query_one(
        "SELECT id, title FROM documents WHERE collection_id = %s AND content_hash = %s LIMIT 1",
        (collection_id, content_hash),
    )


def insert_document(
    collection_id: str,
    title: str,
    source: str,
    content_hash: str,
    char_count: int,
    chunks: list[dict],
) -> dict:
    """写入一个文档及其全部分块（同一事务），返回落库结果。"""
    doc_id = "doc_" + uuid.uuid4().hex[:12]
    now = datetime.now()
    conn = get_conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (id, collection_id, title, source, content_hash, "
                "char_count, chunk_count, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (doc_id, collection_id, title, source, content_hash, char_count, len(chunks), now),
            )
            rows = [
                (
                    collection_id,
                    doc_id,
                    int(c.get("seq", i)),
                    title,
                    c["text"],
                    json.dumps(c.get("meta") or {}, ensure_ascii=False),
                    vector_literal(c["embedding"]),
                )
                for i, c in enumerate(chunks)
            ]
            cur.executemany(
                "INSERT INTO chunks (collection_id, doc_id, seq, title, content, meta, embedding) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)

    execute("UPDATE collections SET updated_at = %s WHERE id = %s", (now, collection_id))
    touch_collection(collection_id)
    return {
        "id": doc_id,
        "title": title,
        "chunk_count": len(chunks),
        "char_count": char_count,
        "created_at": now.isoformat(sep=" "),
    }


def touch_collection(collection_id: str) -> None:
    execute("UPDATE collections SET updated_at = %s WHERE id = %s", (datetime.now(), collection_id))


def list_documents(collection_id: str) -> list[dict]:
    rows = query_all(
        "SELECT id, title, source, char_count, chunk_count, created_at FROM documents "
        "WHERE collection_id = %s ORDER BY created_at ASC",
        (collection_id,),
    )
    for r in rows:
        r["created_at"] = r["created_at"].isoformat(sep=" ") if r["created_at"] else None
    return rows


def delete_document(doc_id: str) -> str | None:
    doc = query_one("SELECT collection_id FROM documents WHERE id = %s", (doc_id,))
    if not doc:
        return None
    execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
    execute("DELETE FROM documents WHERE id = %s", (doc_id,))
    touch_collection(doc["collection_id"])
    return doc["collection_id"]


# --------------------------------------------------------------------------- #
# hybrid search
# --------------------------------------------------------------------------- #

SEARCH_MODES = {
    # 权重：向量 / 全文
    "hybrid": (0.7, 0.3),
    "vector": (1.0, 0.0),
    "text": (0.0, 1.0),
}


def search(
    collection_id: str,
    query_text: str,
    query_vector: list[float],
    top_k: int = 5,
    mode: str = "hybrid",
    weights: tuple[float, float] | None = None,
) -> list[dict]:
    """一条 SQL 在 seekdb 内完成召回与排序。"""
    if mode not in SEARCH_MODES:
        raise ValueError(f"unknown mode: {mode}")
    w_vec, w_text = weights or SEARCH_MODES[mode]
    vec = vector_literal(query_vector)

    if mode == "vector":
        sql = f"""
            SELECT c.id, c.doc_id, c.seq, c.title, c.content, c.meta,
                   1 - cosine_distance(c.embedding, %s) AS vec_score,
                   MATCH(c.title, c.content) AGAINST (%s) AS text_raw,
                   MATCH(c.title, c.content) AGAINST (%s) / (1 + MATCH(c.title, c.content) AGAINST (%s)) AS text_score
            FROM chunks c
            WHERE c.collection_id = %s
            ORDER BY vec_score DESC
            LIMIT %s
        """
        args = (vec, query_text, query_text, query_text, collection_id, top_k)
    elif mode == "text":
        sql = f"""
            SELECT c.id, c.doc_id, c.seq, c.title, c.content, c.meta,
                   1 - cosine_distance(c.embedding, %s) AS vec_score,
                   MATCH(c.title, c.content) AGAINST (%s) AS text_raw,
                   MATCH(c.title, c.content) AGAINST (%s) / (1 + MATCH(c.title, c.content) AGAINST (%s)) AS text_score
            FROM chunks c
            WHERE c.collection_id = %s AND MATCH(c.title, c.content) AGAINST (%s)
            ORDER BY text_score DESC
            LIMIT %s
        """
        args = (vec, query_text, query_text, query_text, collection_id, query_text, top_k)
    else:
        sql = f"""
            SELECT * FROM (
              SELECT c.id, c.doc_id, c.seq, c.title, c.content, c.meta,
                     1 - cosine_distance(c.embedding, %s) AS vec_score,
                     MATCH(c.title, c.content) AGAINST (%s) AS text_raw,
                     MATCH(c.title, c.content) AGAINST (%s) / (1 + MATCH(c.title, c.content) AGAINST (%s)) AS text_score
              FROM chunks c
              WHERE c.collection_id = %s
            ) t
            ORDER BY %s * t.vec_score + %s * t.text_score DESC
            LIMIT %s
        """
        args = (vec, query_text, query_text, query_text, collection_id, w_vec, w_text, top_k)

    rows = query_all(sql, args)
    results = []
    for r in rows:
        vec_score = float(r["vec_score"] or 0)
        text_score = float(r["text_score"] or 0)
        score = w_vec * vec_score + w_text * text_score
        meta = r["meta"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        results.append(
            {
                "chunk_id": r["id"],
                "doc_id": r["doc_id"],
                "seq": r["seq"],
                "title": r["title"],
                "content": r["content"],
                "meta": meta or {},
                "score": round(score, 6),
                "vec_score": round(vec_score, 6),
                "text_score": round(text_score, 6),
                "text_raw": round(float(r["text_raw"] or 0), 6),
            }
        )
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# --------------------------------------------------------------------------- #
# sessions（会话状态持久化在 seekdb）
# --------------------------------------------------------------------------- #

def create_session(collection_id: str, title: str = "") -> dict:
    session_id = "ses_" + uuid.uuid4().hex[:12]
    now = datetime.now()
    execute(
        "INSERT INTO sessions (id, collection_id, title, state, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (session_id, collection_id, title, json.dumps({"history": []}, ensure_ascii=False), now, now),
    )
    return get_session(session_id)  # type: ignore[return-value]


def get_session(session_id: str) -> dict | None:
    row = query_one(
        "SELECT id, collection_id, title, state, created_at, updated_at FROM sessions WHERE id = %s",
        (session_id,),
    )
    if not row:
        return None
    row["state"] = json.loads(row["state"]) if isinstance(row["state"], str) else row["state"]
    row["created_at"] = row["created_at"].isoformat(sep=" ") if row["created_at"] else None
    row["updated_at"] = row["updated_at"].isoformat(sep=" ") if row["updated_at"] else None
    return row


def update_session(session_id: str, state: dict, title: str | None = None) -> dict | None:
    if title is not None:
        execute(
            "UPDATE sessions SET state = %s, title = %s, updated_at = %s WHERE id = %s",
            (json.dumps(state, ensure_ascii=False), title, datetime.now(), session_id),
        )
    else:
        execute(
            "UPDATE sessions SET state = %s, updated_at = %s WHERE id = %s",
            (json.dumps(state, ensure_ascii=False), datetime.now(), session_id),
        )
    return get_session(session_id)
