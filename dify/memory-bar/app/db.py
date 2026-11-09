"""seekdb 数据层：记忆、风味卡、订单。

两处核心能力都下推到 seekdb：
  - 向量召回：cosine_distance(embedding, query_vec) + HNSW 向量索引
  - 全文召回：MATCH(title, content) AGAINST(query) + ngram 全文索引
混合排序在一条 SQL 内完成（0.7 向量 + 0.3 全文），应用层只接收 Top-K。
"""

from __future__ import annotations

import contextlib
import json
from datetime import date, datetime
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor

from .config import settings

LIBRARY_USER = "__library__"

DDL_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS memories (
      id BIGINT NOT NULL AUTO_INCREMENT,
      user_id VARCHAR(64) NOT NULL,
      kind VARCHAR(16) NOT NULL,
      title VARCHAR(128) NOT NULL DEFAULT '',
      content TEXT NOT NULL,
      mood VARCHAR(32) NOT NULL DEFAULT '',
      created_at DATETIME NOT NULL,
      embedding VECTOR({settings.vector_dim}),
      PRIMARY KEY (id),
      VECTOR INDEX vidx_memories (embedding)
        WITH (distance=cosine, type=hnsw, m=16, ef_construction=200),
      FULLTEXT INDEX fidx_memories (title, content) WITH PARSER ngram,
      KEY idx_user_kind (user_id, kind, created_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
      id BIGINT NOT NULL AUTO_INCREMENT,
      user_id VARCHAR(64) NOT NULL,
      cocktail_name VARCHAR(128) NOT NULL,
      mood VARCHAR(32) NOT NULL DEFAULT '',
      recipe JSON,
      query_text TEXT NOT NULL,
      created_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      KEY idx_orders_user (user_id, created_at)
    )
    """,
]


def connect(skip_database: bool = False) -> pymysql.connections.Connection:
    kwargs: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "user": settings.user,
        "password": settings.password,
        "charset": "utf8mb4",
        "autocommit": True,
        "cursorclass": DictCursor,
        "connect_timeout": 5,
        "read_timeout": 60,
        "write_timeout": 60,
    }
    if not skip_database:
        kwargs["database"] = settings.database
    return pymysql.connect(**kwargs)


@contextlib.contextmanager
def cursor() -> Iterator[DictCursor]:
    """数据库未创建时（1049）自动退回实例级连接。"""
    try:
        conn = connect()
    except pymysql.err.OperationalError as exc:
        if exc.args and exc.args[0] == 1049:
            conn = connect(skip_database=True)
        else:
            raise
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def query_all(sql: str, args: Any = None) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def query_one(sql: str, args: Any = None) -> dict | None:
    rows = query_all(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: Any = None) -> int:
    with cursor() as cur:
        return cur.execute(sql, args)


def init_database() -> None:
    conn = connect(skip_database=True)
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
    rows = query_all("SHOW INDEX FROM memories")
    merged: dict[str, dict] = {}
    for row in rows:
        item = merged.setdefault(
            row["Key_name"],
            {"name": row["Key_name"], "type": row["Index_type"], "columns": []},
        )
        item["columns"].append(row["Column_name"])
    return list(merged.values())


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in vec) + "]"


# --------------------------------------------------------------------- memory
def insert_memory(
    user_id: str,
    kind: str,
    title: str,
    content: str,
    mood: str,
    embedding: list[float],
    created_at: datetime | None = None,
) -> int:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO memories (user_id, kind, title, content, mood, created_at, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                user_id,
                kind,
                title,
                content,
                mood,
                created_at or datetime.now(),
                vector_literal(embedding),
            ),
        )
        return int(cur.lastrowid)


def count_memories(user_id: str | None = None, kind: str | None = None) -> int:
    clauses, args = [], []
    if user_id is not None:
        clauses.append("user_id = %s")
        args.append(user_id)
    if kind is not None:
        clauses.append("kind = %s")
        args.append(kind)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    row = query_one(f"SELECT COUNT(*) AS c FROM memories{where}", tuple(args))
    return int(row["c"]) if row else 0


def flavor_titles() -> set[str]:
    rows = query_all("SELECT title FROM memories WHERE kind = 'flavor'")
    return {row["title"] for row in rows}


def list_memories(user_id: str, limit: int = 8) -> list[dict]:
    rows = query_all(
        "SELECT id, title, content, mood, created_at FROM memories "
        "WHERE user_id = %s AND kind = 'drop' ORDER BY created_at DESC LIMIT %s",
        (user_id, limit),
    )
    for row in rows:
        row["created_at"] = row["created_at"].isoformat(sep=" ") if row["created_at"] else None
    return rows


# ---------------------------------------------------------------------- order
def insert_order(
    user_id: str,
    cocktail_name: str,
    mood: str,
    recipe: list[dict],
    query_text: str,
) -> int:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO orders (user_id, cocktail_name, mood, recipe, query_text, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                user_id,
                cocktail_name,
                mood,
                json.dumps(recipe, ensure_ascii=False),
                query_text,
                datetime.now(),
            ),
        )
        return int(cur.lastrowid)


def list_orders(user_id: str, limit: int = 8) -> list[dict]:
    rows = query_all(
        "SELECT id, cocktail_name, mood, recipe, query_text, created_at FROM orders "
        "WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
        (user_id, limit),
    )
    for row in rows:
        if isinstance(row["recipe"], str):
            try:
                row["recipe"] = json.loads(row["recipe"])
            except json.JSONDecodeError:
                row["recipe"] = []
        row["created_at"] = row["created_at"].isoformat(sep=" ") if row["created_at"] else None
    return rows


def orders_today(user_id: str) -> int:
    row = query_one(
        "SELECT COUNT(*) AS c FROM orders WHERE user_id = %s AND DATE(created_at) = %s",
        (user_id, date.today()),
    )
    return int(row["c"]) if row else 0


def orders_total(user_id: str) -> int:
    row = query_one("SELECT COUNT(*) AS c FROM orders WHERE user_id = %s", (user_id,))
    return int(row["c"]) if row else 0


# --------------------------------------------------------------------- search
def search(
    user_id: str,
    kind: str,
    query_text: str,
    query_vector: list[float],
    top_k: int = 4,
    weights: tuple[float, float] = (0.7, 0.3),
) -> list[dict]:
    """一条 SQL 完成「向量 + 全文」混合召回与排序。"""
    w_vec, w_text = weights
    vec = vector_literal(query_vector)
    sql = """
        SELECT * FROM (
          SELECT m.id, m.title, m.content, m.mood, m.created_at,
                 1 - cosine_distance(m.embedding, %s) AS vec_score,
                 MATCH(m.title, m.content) AGAINST (%s) AS text_raw,
                 MATCH(m.title, m.content) AGAINST (%s)
                   / (1 + MATCH(m.title, m.content) AGAINST (%s)) AS text_score
          FROM memories m
          WHERE m.user_id = %s AND m.kind = %s
        ) t
        ORDER BY %s * t.vec_score + %s * t.text_score DESC
        LIMIT %s
    """
    rows = query_all(
        sql,
        (
            vec,
            query_text,
            query_text,
            query_text,
            user_id,
            kind,
            w_vec,
            w_text,
            top_k,
        ),
    )
    results = []
    for row in rows:
        vec_score = float(row["vec_score"] or 0)
        text_score = float(row["text_score"] or 0)
        results.append(
            {
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "mood": row["mood"],
                "created_at": row["created_at"].isoformat(sep=" ") if row["created_at"] else None,
                "score": round(w_vec * vec_score + w_text * text_score, 6),
                "vec_score": round(vec_score, 6),
                "text_score": round(text_score, 6),
            }
        )
    return results


def reset_user(user_id: str) -> dict:
    """演示用：清空某个用户的数据（不动风味卡库）。"""
    if user_id == LIBRARY_USER:
        raise ValueError("不能清空风味卡库")
    memories = execute("DELETE FROM memories WHERE user_id = %s", (user_id,))
    orders = execute("DELETE FROM orders WHERE user_id = %s", (user_id,))
    return {"memories_deleted": memories, "orders_deleted": orders}
