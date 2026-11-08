import logging
from typing import Optional
from app.config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
from app.search import hybrid_search

logger = logging.getLogger(__name__)


def answer_question(query: str, top_k: int = 5) -> dict:
    results = hybrid_search(query, top_k=top_k)

    if not results:
        return {
            "answer": "未找到与您的问题相关的文献段落。请尝试导入更多文档或换一种表述。",
            "citations": [],
            "mode": "empty",
        }

    if OPENAI_API_KEY:
        return _llm_answer(query, results)
    else:
        return _excerpt_answer(query, results)


def _excerpt_answer(query: str, results: list[dict]) -> dict:
    lines = [f"基于检索结果，以下是最相关的段落（共 {len(results)} 条）：\n"]
    citations = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] （相关度: {r['score']:.2f}，来源: {r['source']}）")
        lines.append(f"    {r['text'][:300]}{'...' if len(r['text']) > 300 else ''}")
        lines.append("")
        citations.append({
            "index": i,
            "text": r["text"],
            "source": r["source"],
            "score": r["score"],
            "filename": r["filename"],
            "page_number": r["page_number"],
            "paragraph_index": r["paragraph_index"],
        })

    return {
        "answer": "\n".join(lines),
        "citations": citations,
        "mode": "excerpt",
    }


def _llm_answer(query: str, results: list[dict]) -> dict:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)

        context_parts = []
        for i, r in enumerate(results, 1):
            context_parts.append(f"[{i}] 来源: {r['source']}\n内容: {r['text']}")
        context = "\n\n".join(context_parts)

        system_prompt = """你是一个学术文献助手。根据提供的检索结果回答用户问题。
要求：
1. 在回答中使用 [数字] 标记引用来源
2. 如果检索结果不足以回答问题，请说明
3. 保持客观准确，不要编造信息
4. 回答要简洁有条理"""

        user_prompt = f"问题：{query}\n\n检索结果：\n{context}\n\n请根据以上检索结果回答问题。"

        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=1000,
        )

        answer = response.choices[0].message.content

        citations = []
        for i, r in enumerate(results, 1):
            citations.append({
                "index": i,
                "text": r["text"],
                "source": r["source"],
                "score": r["score"],
                "filename": r["filename"],
                "page_number": r["page_number"],
                "paragraph_index": r["paragraph_index"],
            })

        return {
            "answer": answer,
            "citations": citations,
            "mode": "llm",
        }

    except Exception as e:
        logger.error(f"LLM answer generation failed: {e}")
        return _excerpt_answer(query, results)
