import sqlite3
import json
import struct
from pathlib import Path
from typing import Optional
import numpy as np
from app.config import DATABASE_PATH


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DATABASE_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            file_type TEXT NOT NULL,
            content_hash TEXT,
            chunk_count INTEGER DEFAULT 0,
            imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            page_number INTEGER,
            paragraph_index INTEGER,
            text TEXT NOT NULL,
            embedding BLOB,
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_text ON chunks(text);
    """)
    db.commit()
    db.close()


def serialize_embedding(embedding: np.ndarray) -> bytes:
    return embedding.astype(np.float32).tobytes()


def deserialize_embedding(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32)


def insert_document(filename: str, file_type: str, content_hash: str) -> int:
    db = get_db()
    cursor = db.execute(
        "INSERT INTO documents (filename, file_type, content_hash) VALUES (?, ?, ?)",
        (filename, file_type, content_hash),
    )
    doc_id = cursor.lastrowid
    db.commit()
    db.close()
    return doc_id


def update_document_chunk_count(doc_id: int, count: int):
    db = get_db()
    db.execute("UPDATE documents SET chunk_count = ? WHERE id = ?", (count, doc_id))
    db.commit()
    db.close()


def insert_chunk(doc_id: int, chunk_index: int, page_number: Optional[int],
                 paragraph_index: Optional[int], text: str,
                 embedding: Optional[np.ndarray] = None):
    db = get_db()
    emb_blob = serialize_embedding(embedding) if embedding is not None else None
    db.execute(
        """INSERT INTO chunks
           (document_id, chunk_index, page_number, paragraph_index, text, embedding)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (doc_id, chunk_index, page_number, paragraph_index, text, emb_blob),
    )
    db.commit()
    db.close()


def get_all_chunks(with_embeddings: bool = True):
    db = get_db()
    if with_embeddings:
        rows = db.execute(
            """SELECT c.id, c.document_id, c.chunk_index, c.page_number,
                      c.paragraph_index, c.text, c.embedding,
                      d.filename, d.file_type
               FROM chunks c
               JOIN documents d ON c.document_id = d.id
               WHERE c.embedding IS NOT NULL"""
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT c.id, c.document_id, c.chunk_index, c.page_number,
                      c.paragraph_index, c.text, NULL as embedding,
                      d.filename, d.file_type
               FROM chunks c
               JOIN documents d ON c.document_id = d.id"""
        ).fetchall()
    db.close()
    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "document_id": row["document_id"],
            "chunk_index": row["chunk_index"],
            "page_number": row["page_number"],
            "paragraph_index": row["paragraph_index"],
            "text": row["text"],
            "embedding": deserialize_embedding(row["embedding"]) if row["embedding"] else None,
            "filename": row["filename"],
            "file_type": row["file_type"],
        })
    return results


def get_documents():
    db = get_db()
    rows = db.execute(
        "SELECT id, filename, file_type, chunk_count, imported_at FROM documents ORDER BY imported_at DESC"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def document_exists(content_hash: str) -> bool:
    db = get_db()
    row = db.execute("SELECT id FROM documents WHERE content_hash = ?", (content_hash,)).fetchone()
    db.close()
    return row is not None
