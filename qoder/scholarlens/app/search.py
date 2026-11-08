import re
import math
from typing import Optional
import numpy as np
from app.embeddings import encode_query, batch_cosine_similarity
from app.database import get_all_chunks
from app.config import SEARCH_TOP_K, VECTOR_WEIGHT, KEYWORD_WEIGHT


def keyword_score(query: str, text: str) -> float:
    query_terms = set(re.findall(r'\w+', query.lower()))
    text_lower = text.lower()
    if not query_terms:
        return 0.0
    hits = 0
    for term in query_terms:
        count = text_lower.count(term)
        if count > 0:
            hits += min(count, 5)
    max_possible = len(query_terms) * 5
    return hits / max_possible if max_possible > 0 else 0.0


def hybrid_search(query: str, top_k: int = SEARCH_TOP_K,
                  vector_weight: float = VECTOR_WEIGHT,
                  keyword_weight: float = KEYWORD_WEIGHT) -> list[dict]:
    chunks = get_all_chunks(with_embeddings=True)
    if not chunks:
        return []

    query_emb = encode_query(query)

    embeddings = np.array([c["embedding"] for c in chunks], dtype=np.float32)
    vector_scores = batch_cosine_similarity(query_emb, embeddings)

    v_min, v_max = vector_scores.min(), vector_scores.max()
    if v_max > v_min:
        vector_scores_norm = (vector_scores - v_min) / (v_max - v_min)
    else:
        vector_scores_norm = np.zeros_like(vector_scores)

    keyword_scores = np.array([keyword_score(query, c["text"]) for c in chunks])

    final_scores = vector_weight * vector_scores_norm + keyword_weight * keyword_scores

    top_indices = np.argsort(final_scores)[::-1][:top_k]

    results = []
    for idx in top_indices:
        chunk = chunks[idx]
        score = float(final_scores[idx])
        source = format_source(chunk)
        results.append({
            "id": chunk["id"],
            "text": chunk["text"],
            "score": round(score, 4),
            "vector_score": round(float(vector_scores[idx]), 4),
            "keyword_score": round(float(keyword_scores[idx]), 4),
            "source": source,
            "filename": chunk["filename"],
            "page_number": chunk["page_number"],
            "paragraph_index": chunk["paragraph_index"],
            "chunk_index": chunk["chunk_index"],
        })

    return results


def format_source(chunk: dict) -> str:
    parts = [chunk["filename"]]
    if chunk["page_number"] is not None:
        parts.append(f"p.{chunk['page_number']}")
    if chunk["paragraph_index"] is not None:
        parts.append(f"para.{chunk['paragraph_index']}")
    return " | ".join(parts)
