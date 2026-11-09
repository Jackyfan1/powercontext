"""深夜记忆调酒馆 — HTTP API。

供 Dify Chatflow 的三个节点调用：
  POST /v1/recall    读取记忆 + 生成调酒草稿
  POST /v1/remember  把客人的话封存进 seekdb
  GET  /v1/health    健康检查（含 seekdb 版本与索引信息）
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import db, mixology
from .config import settings
from .embedding import Embedder
from .seed import seed_flavors

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("memory_bar")

embedder = Embedder(settings.model_dir, dim=settings.vector_dim, repo=settings.model_repo)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_database()
    added = seed_flavors(embedder)
    if added:
        logger.info("已写入 %s 张风味卡", added)
    logger.info(
        "调酒馆开张：seekdb %s:%s/%s，编码模式 %s",
        settings.host,
        settings.port,
        settings.database,
        embedder.mode,
    )
    yield


app = FastAPI(
    title="深夜记忆调酒馆",
    description="用 Dify 编排、seekdb 存储记忆的调酒工作流后端",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RecallRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    user_id: str = Field(default="guest", max_length=64)
    top_k: int = Field(default=4, ge=1, le=8)


class RememberRequest(BaseModel):
    user_id: str = Field(default="guest", max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    answer: str | None = None
    mood: str | None = Field(default=None, max_length=32)


class ResetRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)


def _stats(user_id: str) -> dict:
    return {
        "memories": db.count_memories(user_id, "drop"),
        "glasses_today": db.orders_today(user_id),
        "glasses_total": db.orders_total(user_id),
    }


@app.get("/")
def root() -> dict:
    return {
        "name": "深夜记忆调酒馆",
        "endpoints": ["/v1/health", "/v1/recall", "/v1/remember", "/v1/memories", "/v1/orders"],
    }


@app.get("/v1/health")
def health() -> dict:
    try:
        counts = {
            "flavors": db.count_memories(db.LIBRARY_USER, "flavor"),
            "memories": db.count_memories(None, "drop"),
            "orders": db.query_one("SELECT COUNT(*) AS c FROM orders")["c"],
        }
        return {
            "status": "ok",
            "seekdb": {
                "version": db.seekdb_version(),
                "address": f"{settings.host}:{settings.port}",
                "database": settings.database,
                "indexes": db.index_info(),
            },
            "embedding": embedder.info(),
            "counts": counts,
        }
    except Exception as exc:  # noqa: BLE001 - 健康检查要能报告降级原因
        return {"status": "degraded", "error": str(exc), "embedding": embedder.info()}


@app.post("/v1/recall")
def recall(req: RecallRequest) -> dict:
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")

    intent = mixology.detect_intent(query)
    stats = _stats(req.user_id)

    if intent == "shelf":
        recent = db.list_memories(req.user_id, 8)
        result = mixology.render_shelf(recent, stats)
        return {
            "user_id": req.user_id,
            "intent": result["intent"],
            "draft": result["draft"],
            "cocktail": None,
            "matches": recent,
            "flavors": [],
            "stats": stats,
        }

    vector = embedder.embed_one(query)
    memories = db.search(req.user_id, "drop", query, vector, req.top_k)

    if intent == "close":
        result = mixology.render_close(memories, stats)
        return {
            "user_id": req.user_id,
            "intent": result["intent"],
            "draft": result["draft"],
            "cocktail": None,
            "matches": memories,
            "flavors": [],
            "stats": stats,
        }

    flavors = db.search(db.LIBRARY_USER, "flavor", query, vector, req.top_k)
    result = mixology.render_cocktail(req.user_id, query, memories, flavors, stats)
    order_id = db.insert_order(
        req.user_id,
        result["cocktail"]["name"],
        result["cocktail"]["mood"],
        result["cocktail"]["recipe"],
        query,
    )
    return {
        "user_id": req.user_id,
        "intent": result["intent"],
        "draft": result["draft"],
        "cocktail": result["cocktail"],
        "matches": memories,
        "flavors": flavors,
        "stats": {**stats, "glasses_today": stats["glasses_today"] + 1},
        "order_id": order_id,
    }


@app.post("/v1/remember")
def remember(req: RememberRequest) -> dict:
    intent = mixology.detect_intent(req.text)
    if intent != "brew":
        return {
            "stored": False,
            "reason": f"intent={intent}",
            "total_memories": db.count_memories(req.user_id, "drop"),
        }
    text = req.text.strip()
    mood = req.mood or mixology.detect_mood(text)
    vector = embedder.embed_one(text)
    memory_id = db.insert_memory(
        req.user_id,
        "drop",
        mixology.shorten(text, 24),
        text,
        mood,
        vector,
    )
    return {
        "stored": True,
        "memory_id": memory_id,
        "total_memories": db.count_memories(req.user_id, "drop"),
    }


@app.get("/v1/memories")
def memories(user_id: str = "guest", limit: int = 8) -> dict:
    return {
        "user_id": user_id,
        "memories": db.list_memories(user_id, max(1, min(limit, 50))),
        "total": db.count_memories(user_id, "drop"),
    }


@app.get("/v1/orders")
def orders(user_id: str = "guest", limit: int = 8) -> dict:
    return {
        "user_id": user_id,
        "orders": db.list_orders(user_id, max(1, min(limit, 50))),
    }


@app.post("/v1/reset")
def reset(req: ResetRequest) -> dict:
    try:
        return {"user_id": req.user_id, **db.reset_user(req.user_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.server_port)
