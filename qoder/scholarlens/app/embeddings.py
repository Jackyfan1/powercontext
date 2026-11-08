import os
import logging
from functools import lru_cache
from typing import Optional
import numpy as np

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from sentence_transformers import SentenceTransformer
from app.config import EMBEDDING_MODEL

logger = logging.getLogger(__name__)

_model: Optional[SentenceTransformer] = None


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
        _model = SentenceTransformer(EMBEDDING_MODEL)
        logger.info("Embedding model loaded successfully")
    return _model


def encode_texts(texts: list[str]) -> np.ndarray:
    model = get_model()
    embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
    return np.array(embeddings, dtype=np.float32)


def encode_query(query: str) -> np.ndarray:
    model = get_model()
    embedding = model.encode([query], normalize_embeddings=True)
    return np.array(embedding[0], dtype=np.float32)


def cosine_similarity(query_emb: np.ndarray, doc_emb: np.ndarray) -> float:
    return float(np.dot(query_emb, doc_emb))


def batch_cosine_similarity(query_emb: np.ndarray, doc_embs: np.ndarray) -> np.ndarray:
    return np.dot(doc_embs, query_emb)
