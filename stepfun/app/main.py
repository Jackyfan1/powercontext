"""StepSeek 后端：静态前端 + seekdb 数据接口。

接口一览：
  GET  /api/health                      seekdb 连通性、版本、索引状态
  GET  /api/model/info                  本地模型文件体积（FP32/FP16/INT8）
  GET  /api/collections                 集合列表
  POST /api/collections                 新建集合
  DEL  /api/collections/{id}            删除集合
  POST /api/collections/{id}/refresh    刷新索引（ANALYZE + 统计）
  GET  /api/documents?collection_id=    文档列表
  POST /api/documents                   写入文档与向量（浏览器本地推理生成）
  DEL  /api/documents/{id}              删除文档
  POST /api/search                      混合检索（seekdb 内一条 SQL 完成）
  POST /api/sessions                    新建会话
  GET  /api/sessions/{id}               读取会话状态
  PUT  /api/sessions/{id}               保存会话状态
  POST /api/benchmark/report            落盘量化对比实测结果
  GET  /api/benchmark/report            读取量化对比实测结果
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .config import settings

app = FastAPI(title="StepSeek · 端侧知识库", version="1.0.0")

QUANT_REPORT = settings.docs_dir / "quantization-results.json"
MODEL_FILES = {
    "fp32": "onnx/model.onnx",
    "fp16": "onnx/model_fp16.onnx",
    "int8": "onnx/model_quantized.onnx",
}


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #

@app.on_event("startup")
def _startup() -> None:
    db.init_database()


@app.middleware("http")
async def _isolation_headers(request, call_next):
    """开启跨源隔离，让 WASM 后端可以使用多线程（SharedArrayBuffer）。"""
    response = await call_next(request)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@app.get("/api/health")
def health() -> dict:
    try:
        version = db.seekdb_version()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": f"seekdb 连接失败: {exc}"},
        )
    return {
        "ok": True,
        "seekdb": {"host": settings.host, "port": settings.port, "database": settings.database, "version": version},
        "index": db.index_info(),
        "server_time": datetime.now().isoformat(sep=" "),
    }


@app.get("/api/model/info")
def model_info() -> dict:
    model_root = settings.model_dir / settings.model_name
    files = {}
    for dtype, rel in MODEL_FILES.items():
        path = model_root / rel
        files[dtype] = {
            "path": rel,
            "bytes": path.stat().st_size if path.exists() else 0,
            "exists": path.exists(),
        }
    return {
        "model": settings.model_name,
        "dim": settings.vector_dim,
        "local_path": f"/models/{settings.model_name}/",
        "files": files,
    }


# --------------------------------------------------------------------------- #
# collections
# --------------------------------------------------------------------------- #

class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""


@app.get("/api/collections")
def api_collections() -> list[dict]:
    return db.list_collections()


@app.post("/api/collections")
def api_create_collection(body: CollectionIn) -> dict:
    return db.create_collection(body.name, body.description)


@app.delete("/api/collections/{collection_id}")
def api_delete_collection(collection_id: str) -> dict:
    if not db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="集合不存在")
    db.delete_collection(collection_id)
    return {"ok": True}


@app.post("/api/collections/{collection_id}/refresh")
def api_refresh_collection(collection_id: str) -> dict:
    if not db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="集合不存在")
    started = time.perf_counter()
    db.analyze()
    elapsed = (time.perf_counter() - started) * 1000
    collection = db.get_collection(collection_id)
    return {
        "ok": True,
        "took_ms": round(elapsed, 2),
        "collection": collection,
        "index": db.index_info(),
    }


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #

class ChunkIn(BaseModel):
    seq: int
    text: str
    embedding: list[float]
    meta: dict | None = None


class DocumentIn(BaseModel):
    collection_id: str
    title: str = Field(min_length=1, max_length=255)
    source: str = ""
    content_hash: str = ""
    char_count: int = 0
    chunks: list[ChunkIn]


@app.get("/api/documents")
def api_documents(collection_id: str) -> list[dict]:
    return db.list_documents(collection_id)


@app.post("/api/documents")
def api_create_document(body: DocumentIn) -> dict:
    if not db.get_collection(body.collection_id):
        raise HTTPException(status_code=404, detail="集合不存在")
    if not body.chunks:
        raise HTTPException(status_code=400, detail="分块为空")
    for chunk in body.chunks:
        if len(chunk.embedding) != settings.vector_dim:
            raise HTTPException(
                status_code=400,
                detail=f"向量维度应为 {settings.vector_dim}，实际 {len(chunk.embedding)}",
            )
    duplicate = db.find_document_by_hash(body.collection_id, body.content_hash) if body.content_hash else None
    if duplicate:
        return {"duplicate": True, "document": duplicate, "message": "内容已存在，跳过重复导入"}

    started = time.perf_counter()
    document = db.insert_document(
        collection_id=body.collection_id,
        title=body.title,
        source=body.source,
        content_hash=body.content_hash,
        char_count=body.char_count,
        chunks=[c.model_dump() for c in body.chunks],
    )
    document["write_ms"] = round((time.perf_counter() - started) * 1000, 2)
    document["duplicate"] = False
    document["collection"] = db.get_collection(body.collection_id)
    return document


@app.delete("/api/documents/{doc_id}")
def api_delete_document(doc_id: str) -> dict:
    collection_id = db.delete_document(doc_id)
    if not collection_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"ok": True, "collection": db.get_collection(collection_id)}


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

class SearchIn(BaseModel):
    collection_id: str
    query: str
    embedding: list[float]
    top_k: int = 5
    mode: str = "hybrid"
    weights: list[float] | None = None


@app.post("/api/search")
def api_search(body: SearchIn) -> dict:
    if not db.get_collection(body.collection_id):
        raise HTTPException(status_code=404, detail="集合不存在")
    if len(body.embedding) != settings.vector_dim:
        raise HTTPException(
            status_code=400,
            detail=f"向量维度应为 {settings.vector_dim}，实际 {len(body.embedding)}",
        )
    weights = tuple(body.weights) if body.weights and len(body.weights) == 2 else None
    started = time.perf_counter()
    results = db.search(
        collection_id=body.collection_id,
        query_text=body.query,
        query_vector=body.embedding,
        top_k=body.top_k,
        mode=body.mode,
        weights=weights,  # type: ignore[arg-type]
    )
    elapsed = (time.perf_counter() - started) * 1000
    return {
        "query": body.query,
        "mode": body.mode,
        "weights": weights or db.SEARCH_MODES.get(body.mode),
        "sql_ms": round(elapsed, 2),
        "count": len(results),
        "results": results,
    }


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #

class SessionIn(BaseModel):
    collection_id: str = ""
    title: str = ""


class SessionStateIn(BaseModel):
    state: dict
    title: str | None = None


@app.post("/api/sessions")
def api_create_session(body: SessionIn) -> dict:
    return db.create_session(body.collection_id, body.title)


@app.get("/api/sessions/{session_id}")
def api_get_session(session_id: str) -> dict:
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@app.put("/api/sessions/{session_id}")
def api_update_session(session_id: str, body: SessionStateIn) -> dict:
    session = db.update_session(session_id, body.state, body.title)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


# --------------------------------------------------------------------------- #
# quantization benchmark report
# --------------------------------------------------------------------------- #

class BenchmarkIn(BaseModel):
    results: list[dict]
    meta: dict = {}


@app.post("/api/benchmark/report")
def api_save_benchmark(body: BenchmarkIn) -> dict:
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now().isoformat(sep=" "),
        "meta": body.meta,
        "results": body.results,
    }
    QUANT_REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(QUANT_REPORT)}


@app.get("/api/benchmark/report")
def api_get_benchmark() -> dict:
    if not QUANT_REPORT.exists():
        raise HTTPException(status_code=404, detail="尚未运行量化对比")
    return json.loads(QUANT_REPORT.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 示例语料
# --------------------------------------------------------------------------- #

@app.get("/api/samples")
def api_samples() -> list[dict]:
    samples = []
    sample_dir = settings.static_dir.parent.parent / "sample_data"
    for path in sorted(sample_dir.glob("*.md")):
        first_line = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                first_line = line[2:].strip()
                break
        samples.append({"file": path.name, "title": first_line or path.stem})
    return samples


# --------------------------------------------------------------------------- #
# static
# --------------------------------------------------------------------------- #

@app.get("/")
def index() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html")


app.mount(
    "/samples",
    StaticFiles(directory=Path(settings.static_dir.parent.parent / "sample_data")),
    name="samples",
)
app.mount("/", StaticFiles(directory=Path(settings.static_dir), html=True), name="static")
