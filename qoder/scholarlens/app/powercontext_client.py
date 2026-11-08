"""
PowerContext 客户端 — 连接真实 PowerContext Server (port 8710)。
使用真实 API: /health/live, /v1/scopes, /v1/memory/remember, /v1/memory/search
"""
import json
import logging
from pathlib import Path
from typing import Optional
import httpx
from app.config import POWERCONTEXT_URL, DATA_DIR

logger = logging.getLogger(__name__)

TIMEOUT = 5.0
SCOPE_FILE = Path(DATA_DIR) / "powercontext_scope.json"
_scope_id: Optional[str] = None


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, trust_env=False)


def is_available() -> bool:
    try:
        with _client() as c:
            r = c.get(f"{POWERCONTEXT_URL}/health/live")
            return r.status_code == 200
    except Exception as e:
        logger.warning(f"PowerContext health check failed: {e!r}")
        return False


def get_scope_id() -> Optional[str]:
    """获取 ScholarLens 专属 scope（首次调用时通过 POST /v1/scopes 创建并持久化）。"""
    global _scope_id
    if _scope_id:
        return _scope_id

    if SCOPE_FILE.exists():
        try:
            _scope_id = json.loads(SCOPE_FILE.read_text(encoding="utf-8"))["scope_id"]
            return _scope_id
        except Exception:
            pass

    try:
        with _client() as c:
            r = c.post(
                f"{POWERCONTEXT_URL}/v1/scopes",
                json={
                    "title": "ScholarLens 研究记忆",
                    "summary": "ScholarLens 文献语义检索助手的研究记忆：检索历史、研究主题与已确认结论",
                    "idempotency_key": "scholarlens-qoder-scope-v1",
                },
            )
            if r.status_code in (200, 201):
                _scope_id = r.json()["scope_id"]
            else:
                r2 = c.get(f"{POWERCONTEXT_URL}/v1/scopes/default")
                r2.raise_for_status()
                _scope_id = r2.json()["scope_id"]
            SCOPE_FILE.write_text(json.dumps({"scope_id": _scope_id}), encoding="utf-8")
            return _scope_id
    except Exception as e:
        logger.warning(f"PowerContext get_scope_id failed: {e}")
        return None


def remember(kind: str, text: str) -> dict:
    scope_id = get_scope_id()
    if not scope_id:
        return {"error": "no_scope"}
    try:
        with _client() as c:
            r = c.post(
                f"{POWERCONTEXT_URL}/v1/memory/remember",
                json={"scope_id": scope_id, "kind": kind, "text": text},
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning(f"PowerContext remember failed: {e}")
        return {"error": str(e)}


def search_memory(query: str, limit: int = 10) -> list[dict]:
    scope_id = get_scope_id()
    if not scope_id:
        return []
    try:
        with _client() as c:
            r = c.post(
                f"{POWERCONTEXT_URL}/v1/memory/search",
                json={"scope_id": scope_id, "query": query, "limit": limit},
            )
            r.raise_for_status()
            data = r.json()
            return [hit["memory"] for hit in data.get("hits", [])]
    except Exception as e:
        logger.warning(f"PowerContext search_memory failed: {e}")
        return []


def recall_context() -> dict:
    available = is_available()
    if not available:
        return {
            "available": False,
            "topics": [],
            "recent_searches": [],
            "conclusions": [],
            "last_topic": None,
            "summary": "PowerContext 未启动，记忆功能不可用",
        }

    searches = search_memory("检索主题", limit=10)
    conclusions = search_memory("conclusion", limit=10)
    topics = search_memory("research_topic", limit=10)

    recent_searches = []
    for mem in searches:
        if mem.get("kind") == "search_history":
            recent_searches.append({"query": mem["text"], "kind": mem["kind"]})

    topic_list = []
    for mem in topics:
        if mem.get("kind") == "research_topic":
            topic_list.append({"topic": mem["text"]})

    conclusion_list = []
    for mem in conclusions:
        if mem.get("kind") == "conclusion":
            conclusion_list.append({"conclusion": mem["text"]})

    last_topic = topic_list[-1]["topic"] if topic_list else None
    summary = _build_summary(topic_list, recent_searches, conclusion_list)

    return {
        "available": True,
        "topics": topic_list[-5:],
        "recent_searches": recent_searches[-5:],
        "conclusions": conclusion_list[-5:],
        "last_topic": last_topic,
        "summary": summary,
    }


def _build_summary(topics: list, searches: list, conclusions: list) -> str:
    parts = []
    if topics:
        parts.append(f"上次你在查「{topics[-1].get('topic', '')}」")
    if searches:
        parts.append(f"最近搜索过「{searches[-1].get('query', '')[:60]}」")
    if conclusions:
        parts.append(f"已确认 {len(conclusions)} 条结论")
    return "；".join(parts) if parts else "暂无研究记忆"


def get_expansion_terms() -> list[str]:
    terms = []
    memories = search_memory("research_topic", limit=5)
    for mem in memories:
        if mem.get("kind") == "research_topic" and mem.get("text"):
            terms.append(mem["text"])
    return terms
