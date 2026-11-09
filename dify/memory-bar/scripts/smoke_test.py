"""端到端冒烟测试：验证 seekdb 写入 + 混合检索 + 调酒草稿全链路。

先启动服务（python -m app.main），再运行：
    python scripts/smoke_test.py [--base-url http://127.0.0.1:8787]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: dict | str) -> None:
    RESULTS.append({"case": name, "status": "PASS" if ok else "FAIL", "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"       {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    user = f"smoke_{int(time.time()) % 100000}"

    health = requests.get(f"{base}/v1/health", timeout=10).json()
    index_names = {item["name"] for item in health.get("seekdb", {}).get("indexes", [])}
    check(
        "健康检查 / seekdb 索引",
        health.get("status") == "ok" and {"vidx_memories", "fidx_memories"} <= index_names,
        {
            "status": health.get("status"),
            "seekdb": health.get("seekdb", {}).get("version"),
            "embedding": health.get("embedding"),
            "indexes": sorted(index_names),
        },
    )

    recall = requests.post(
        f"{base}/v1/recall",
        json={"query": "今天加班到很晚，有点想家", "user_id": "demo", "top_k": 4},
        timeout=20,
    ).json()
    top = recall["matches"][0] if recall["matches"] else {}
    check(
        "混合检索 + 调酒草稿",
        "今晚的酒" in recall.get("draft", "")
        and recall.get("intent") == "brew"
        and len(recall.get("matches", [])) > 0
        and top.get("vec_score", 0) > 0,
        {
            "cocktail": recall.get("cocktail", {}).get("name"),
            "top_memory": top.get("content"),
            "score": top.get("score"),
            "vec_score": top.get("vec_score"),
            "text_score": top.get("text_score"),
            "flavors": [item["title"] for item in recall.get("flavors", [])],
        },
    )

    text = "今天第一次自己煮了面，盐放多了，但居然挺好吃。"
    remembered = requests.post(
        f"{base}/v1/remember", json={"user_id": user, "text": text}, timeout=20
    ).json()
    check(
        "写入新记忆 /v1/remember",
        remembered.get("stored") is True and remembered.get("total_memories") == 1,
        remembered,
    )

    again = requests.post(
        f"{base}/v1/recall", json={"query": "自己煮的面是不是盐放多了", "user_id": user}, timeout=20
    ).json()
    first = again["matches"][0] if again["matches"] else {}
    check(
        "语义召回刚写入的记忆",
        first.get("id") == remembered.get("memory_id"),
        {
            "expected_id": remembered.get("memory_id"),
            "top_id": first.get("id"),
            "top_content": first.get("content"),
            "score": first.get("score"),
            "vec_score": first.get("vec_score"),
            "text_score": first.get("text_score"),
        },
    )

    shelf = requests.post(
        f"{base}/v1/recall", json={"query": "看看我的酒柜", "user_id": "demo"}, timeout=20
    ).json()
    check(
        "酒柜意图",
        shelf.get("intent") == "shelf" and "酒柜" in shelf.get("draft", ""),
        {"intent": shelf.get("intent"), "lines": len(shelf.get("matches", []))},
    )

    close = requests.post(
        f"{base}/v1/recall", json={"query": "打烊了", "user_id": "demo"}, timeout=20
    ).json()
    check(
        "打烊意图",
        close.get("intent") == "close" and "打烊" in close.get("draft", ""),
        {"intent": close.get("intent")},
    )

    memories = requests.get(f"{base}/v1/memories", params={"user_id": user}, timeout=10).json()
    orders = requests.get(f"{base}/v1/orders", params={"user_id": "demo"}, timeout=10).json()
    check(
        "酒柜与酒单持久化",
        memories.get("total") == 1 and len(orders.get("orders", [])) > 0,
        {
            "memories": memories.get("total"),
            "orders": len(orders.get("orders", [])),
            "latest_order": orders.get("orders", [{}])[0].get("cocktail_name")
            if orders.get("orders")
            else None,
        },
    )

    out = Path(__file__).resolve().parent.parent / "docs" / "smoke_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"base_url": base, "results": RESULTS}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    failures = [item for item in RESULTS if item["status"] == "FAIL"]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} 通过，结果已写入 {out}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
