"""种子数据：风味卡（全局记忆）与演示客人的历史记忆。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import db
from .embedding import Embedder

DATA_DIR = Path(__file__).resolve().parent.parent / "sample_data"


def _load(name: str) -> list[dict]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def seed_flavors(embedder: Embedder) -> int:
    """写入风味卡库（幂等：按标题去重）。"""
    existing = db.flavor_titles()
    added = 0
    for card in _load("flavor_cards.json"):
        if card["title"] in existing:
            continue
        vector = embedder.embed_one(f"{card['title']}。{card['content']}")
        db.insert_memory(
            db.LIBRARY_USER,
            "flavor",
            card["title"],
            card["content"],
            card.get("mood", ""),
            vector,
        )
        added += 1
    return added


def seed_demo(embedder: Embedder, user_id: str = "demo") -> int:
    """写入演示客人的历史记忆（已有记忆则跳过，避免重复灌入）。"""
    if db.count_memories(user_id, "drop"):
        return 0
    entries = _load("demo_memories.json")
    for entry in entries:
        vector = embedder.embed_one(entry["content"])
        db.insert_memory(
            user_id,
            "drop",
            entry["title"],
            entry["content"],
            entry.get("mood", ""),
            vector,
            datetime.fromisoformat(entry["created_at"]),
        )
    return len(entries)
