"""后端冒烟测试：验证 seekdb 存取、混合检索、增量导入、索引刷新与会话状态。

浏览器端 embedding 由真实模型完成；本脚本用确定性的哈希向量替代，
只验证「写入 → 检索 → 刷新 → 会话」这条服务端链路。

    python scripts/smoke_test.py [--base http://127.0.0.1:8011]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIM = 512


def call(base: str, path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> {exc.code}: {exc.read().decode()}") from exc


def fake_embedding(text: str) -> list[float]:
    """字符二元组哈希到固定维度，语义相近的文本会共享更多维度。"""
    vec = [0.0] * DIM
    for size in (1, 2):
        for i in range(len(text) - size + 1):
            gram = text[i : i + size]
            digest = hashlib.md5(gram.encode()).digest()
            for offset in range(0, 8, 2):
                bucket = int.from_bytes(digest[offset : offset + 2], "big") % DIM
                vec[bucket] += 1.0 if offset % 4 == 0 else -1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [round(v / norm, 6) for v in vec]


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    return condition


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8011")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    ok = True

    print("[1] 健康检查")
    health = call(base, "/api/health")
    ok &= check("seekdb 可用", health.get("ok") is True, health.get("seekdb", {}).get("version", ""))
    index_types = {idx["type"] for idx in health.get("index", [])}
    ok &= check("存在 VECTOR 与 FULLTEXT 索引", {"VECTOR", "FULLTEXT"} <= index_types, str(sorted(index_types)))

    print("[2] 创建测试集合")
    collection = call(base, "/api/collections", "POST", {"name": f"smoke-{hashlib.md5(str(Path.cwd()).encode()).hexdigest()[:6]}"})
    collection_id = collection["id"]
    check("集合创建成功", bool(collection_id), collection_id)

    docs = [
        ("seekdb 检索", "seekdb 支持向量检索与全文检索，可以在一条 SQL 中完成混合排序。"),
        ("端侧推理", "浏览器通过 WebGPU 或 WASM 执行 ONNX 模型，实现本地向量化。"),
        ("量化方案", "INT8 量化把模型体积压缩到 FP32 的四分之一，质量基本不变。"),
    ]
    print("[3] 增量写入文档与向量")
    doc_ids = []
    for title, content in docs:
        result = call(
            base,
            "/api/documents",
            "POST",
            {
                "collection_id": collection_id,
                "title": title,
                "source": "smoke",
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "char_count": len(content),
                "chunks": [{"seq": 0, "text": content, "embedding": fake_embedding(content), "meta": {"from": "smoke"}}],
            },
        )
        doc_ids.append(result["id"])
        check(f"写入「{title}」", result["chunk_count"] == 1)

    print("[4] 重复导入应被识别")
    title, content = docs[0]
    dup = call(
        base,
        "/api/documents",
        "POST",
        {
            "collection_id": collection_id,
            "title": title,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "chunks": [{"seq": 0, "text": content, "embedding": fake_embedding(content)}],
        },
    )
    ok &= check("重复内容被跳过", dup.get("duplicate") is True)

    print("[5] 三种检索模式")
    query = "seekdb 的向量检索怎么用"
    for mode in ("hybrid", "vector", "text"):
        res = call(
            base,
            "/api/search",
            "POST",
            {
                "collection_id": collection_id,
                "query": query,
                "embedding": fake_embedding(query),
                "top_k": 3,
                "mode": mode,
            },
        )
        scores = [item["score"] for item in res["results"]]
        ok &= check(
            f"{mode} 检索返回结果且按分数降序",
            bool(scores) and scores == sorted(scores, reverse=True),
            f"{res['count']} 条 / SQL {res['sql_ms']} ms",
        )
        if mode in ("vector", "hybrid"):
            ok &= check(f"{mode} 首位命中「{docs[0][0]}」", res["results"][0]["title"] == docs[0][0], res["results"][0]["title"])

    print("[6] 刷新索引后新内容仍可检索")
    new_doc = call(
        base,
        "/api/documents",
        "POST",
        {
            "collection_id": collection_id,
            "title": "HNSW 索引",
            "content_hash": hashlib.sha256(b"hnsw").hexdigest(),
            "chunks": [{"seq": 0, "text": "HNSW 是图结构的近似最近邻索引。", "embedding": fake_embedding("HNSW 是图结构的近似最近邻索引。")}],
        },
    )
    refresh = call(base, f"/api/collections/{collection_id}/refresh", "POST")
    ok &= check("刷新索引成功", refresh.get("ok") is True, f"{refresh.get('took_ms')} ms")
    found = call(
        base,
        "/api/search",
        "POST",
        {"collection_id": collection_id, "query": "HNSW", "embedding": fake_embedding("HNSW"), "top_k": 3, "mode": "text"},
    )
    ok &= check("新增文档可被检索", any(r["doc_id"] == new_doc["id"] for r in found["results"]))

    print("[7] 会话状态持久化")
    session = call(base, "/api/sessions", "POST", {"collection_id": collection_id, "title": "smoke"})
    payload = {"history": [{"query": query, "mode": "hybrid", "sql_ms": 1.2}]}
    call(base, f"/api/sessions/{session['id']}", "PUT", {"state": payload})
    restored = call(base, f"/api/sessions/{session['id']}")
    ok &= check("会话状态可读回", restored["state"]["history"][0]["query"] == query)

    print("[8] 清理")
    call(base, f"/api/collections/{collection_id}", "DELETE")
    remaining = [c for c in call(base, "/api/collections") if c["id"] == collection_id]
    ok &= check("测试集合已删除", not remaining)

    print("\n结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
