"""ScholarLens 冒烟测试脚本"""
import sys
import json
import httpx

BASE = "http://127.0.0.1:8010"
PC_BASE = "http://127.0.0.1:8710"

client = httpx.Client(timeout=60, trust_env=False)
pc_client = httpx.Client(timeout=10, trust_env=False)


def test(name, func):
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    try:
        result = func()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except Exception as e:
        print(f"FAILED: {e}")
        return {"error": str(e)}


def t1_import():
    import pathlib
    results = []
    for fname in ["transformer_attention.md", "bert_paper.txt", "cnn_notes.md"]:
        fpath = pathlib.Path("sample_data") / fname
        with open(fpath, "rb") as f:
            r = client.post(f"{BASE}/api/import", files={"file": (fname, f)})
        data = r.json()
        results.append(data)
        print(f"  {fname}: {data.get('status')} ({data.get('chunk_count', 0)} chunks)")
    return results


def t2_search():
    r = client.post(f"{BASE}/api/search", json={"query": "什么是自注意力机制", "top_k": 3})
    return r.json()


def t3_qa():
    r = client.post(f"{BASE}/api/qa", json={"query": "BERT和GPT有什么区别"})
    return r.json()


def t4_memory_status():
    r = client.get(f"{BASE}/api/memory/status")
    return r.json()


def t5_memory_recall():
    r = client.get(f"{BASE}/api/memory/recall")
    return r.json()


def t6_memory_conclusion():
    r = client.post(f"{BASE}/api/memory/conclusion", json={
        "topic": "Transformer架构",
        "conclusion": "自注意力机制是Transformer的核心，通过Q/K/V向量计算注意力权重",
        "source_text": "transformer_attention.md para.1"
    })
    return r.json()


def t7_powercontext_remember():
    scope_r = pc_client.get(f"{PC_BASE}/v1/scopes/default")
    scope_id = scope_r.json()["scope_id"]
    r = pc_client.post(f"{PC_BASE}/v1/memory/remember", json={
        "scope_id": scope_id,
        "kind": "smoke_test",
        "text": "ScholarLens smoke test: 导入/检索/问答/记忆四接口验证"
    })
    return {"request": {"scope_id": scope_id, "kind": "smoke_test", "text": "ScholarLens smoke test: 导入/检索/问答/记忆四接口验证"}, "response": r.json()}


def t8_powercontext_search():
    scope_r = pc_client.get(f"{PC_BASE}/v1/scopes/default")
    scope_id = scope_r.json()["scope_id"]
    r = pc_client.post(f"{PC_BASE}/v1/memory/search", json={
        "scope_id": scope_id,
        "query": "ScholarLens",
        "limit": 5
    })
    return {"request": {"scope_id": scope_id, "query": "ScholarLens", "limit": 5}, "response": r.json()}


def t9_search_expansion():
    r = client.post(f"{BASE}/api/search", json={"query": "卷积神经网络", "top_k": 3})
    return r.json()


def t10_memory_recall_after():
    r = client.get(f"{BASE}/api/memory/recall")
    return r.json()


if __name__ == "__main__":
    print("ScholarLens Smoke Tests")
    print(f"Server: {BASE}")
    print(f"PowerContext: {PC_BASE}")

    test("1. Import documents", t1_import)
    test("2. Semantic search", t2_search)
    test("3. Q&A (excerpt mode)", t3_qa)
    test("4. Memory status", t4_memory_status)
    test("5. Memory recall", t5_memory_recall)
    test("6. Save conclusion", t6_memory_conclusion)
    test("7. PowerContext /v1/memory/remember (real API)", t7_powercontext_remember)
    test("8. PowerContext /v1/memory/search (real API)", t8_powercontext_search)
    test("9. Search with expansion", t9_search_expansion)
    test("10. Memory recall after searches", t10_memory_recall_after)

    print("\n\nALL TESTS COMPLETED")
