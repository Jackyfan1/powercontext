"""开发上下文选择：从 seekdb 检索规格、代码与测试，叠加 PowerContext 任务状态。"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .store import SeekDBStore

ENGLISH_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")


def extract_keywords(task: dict[str, Any]) -> list[str]:
    keywords: list[str] = [str(k) for k in task.get("keywords", [])]
    text = f"{task.get('title', '')} {task.get('description', '')}"
    keywords.extend(ENGLISH_TOKEN.findall(text))
    for run in CHINESE_RUN.findall(text):
        keywords.append(run)
        keywords.extend(run[index : index + 2] for index in range(len(run) - 1))

    seen: set[str] = set()
    ordered: list[str] = []
    for keyword in keywords:
        normalized = keyword.strip().lower()
        if len(normalized) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered[:16]


def _document_payload(hit) -> dict[str, Any]:
    doc = hit.document
    return {
        "doc_id": doc.doc_id,
        "kind": doc.kind,
        "path": doc.path,
        "symbol": doc.symbol,
        "title": doc.title,
        "lines": f"{doc.line_start}-{doc.line_end}",
        "version": hit.version,
        "score": hit.score,
        "content": doc.content,
    }


def build_context(
    store: SeekDBStore,
    task: dict[str, Any],
    state: dict[str, Any] | None,
    keywords: Iterable[str] | None = None,
) -> dict[str, Any]:
    terms = list(keywords) if keywords else extract_keywords(task)
    spec_hits = store.search(terms, ["spec"], limit=6)
    code_hits = store.search(terms, ["code"], limit=6)
    test_hits = store.search(terms, ["test"], limit=8)

    state_payload = state or {}
    sources = [
        {
            "doc_id": hit.document.doc_id,
            "path": hit.document.path,
            "lines": f"{hit.document.line_start}-{hit.document.line_end}",
        }
        for hit in [*spec_hits, *code_hits, *test_hits]
    ]

    return {
        "task": task,
        "keywords": terms,
        "spec_items": [_document_payload(hit) for hit in spec_hits],
        "code_targets": [_document_payload(hit) for hit in code_hits],
        "tests": [_document_payload(hit) for hit in test_hits],
        "task_state": {
            "task_id": state_payload.get("task_id", task.get("task_id")),
            "done": state_payload.get("done", []),
            "todo": state_payload.get("todo", []),
            "verification": state_payload.get("verification", []),
        },
        "sources": sources,
        "storage": store.describe(),
    }


def render_markdown(context: dict[str, Any]) -> str:
    task = context["task"]
    lines: list[str] = [
        f"# 开发上下文 · {task.get('task_id')} {task.get('title')}",
        "",
        f"- 任务描述：{task.get('description', '')}",
        f"- 检索关键词：{', '.join(context['keywords'][:12])}",
        "",
        "## 适用的规格条目",
    ]
    for item in context["spec_items"]:
        lines.append(
            f"- `{item['doc_id']}`（v{item['version']}，{item['path']}#L{item['lines']}）：{item['title']}"
        )
    lines += ["", "## 需要修改的代码"]
    for item in context["code_targets"]:
        lines.append(
            f"- `{item['path']}#L{item['lines']}` `{item['symbol']}`（v{item['version']}，score={item['score']}）"
        )
    lines += ["", "## 用于验证的测试"]
    for item in context["tests"]:
        lines.append(
            f"- `{item['path']}#L{item['lines']}` `{item['symbol']}`（v{item['version']}）：{item['title']}"
        )
    state = context["task_state"]
    lines += ["", "## 任务状态（PowerContext）"]
    lines.append(f"- 已完成：{state['done'] or '无'}")
    lines.append(f"- 待办：{state['todo'] or '无'}")
    lines.append(f"- 已有验证结果：{state['verification'] or '无'}")
    return "\n".join(lines)
