"""
记忆模块 — 通过真实 PowerContext Server 持久化研究记忆。
"""
import logging
from app.powercontext_client import (
    remember, search_memory, recall_context, get_expansion_terms, is_available,
)

logger = logging.getLogger(__name__)


def on_search(query: str, results: list[dict]):
    top_text = results[0]["text"][:200] if results else "无结果"
    remember("search_history", f"检索主题: {query} | 结果数: {len(results)} | 最佳片段: {top_text}")
    if len(query.strip()) > 3:
        remember("research_topic", query.strip())


def on_conclusion_confirmed(topic: str, conclusion: str, source_text: str = ""):
    remember("conclusion", f"[{topic}] {conclusion}" + (f" (来源: {source_text[:200]})" if source_text else ""))


def get_recall_summary() -> dict:
    return recall_context()


def get_search_expansion() -> list[str]:
    return get_expansion_terms()


def check_powercontext_status() -> dict:
    available = is_available()
    return {
        "available": available,
        "message": "PowerContext 已连接" if available else "PowerContext 未启动，记忆功能不可用",
    }
