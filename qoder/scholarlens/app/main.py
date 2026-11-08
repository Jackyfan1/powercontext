import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

from app.config import SERVER_PORT
from app.database import init_db, insert_document, insert_chunk, update_document_chunk_count, get_documents, document_exists
from app.document_parser import parse_file_from_bytes
from app.embeddings import encode_texts
from app.search import hybrid_search
from app.qa import answer_question
from app.memory import on_search, on_conclusion_confirmed, get_recall_summary, get_search_expansion, check_powercontext_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("ScholarLens database initialized")
    yield
    logger.info("ScholarLens shutting down")


app = FastAPI(title="ScholarLens — 研阅文献语义检索助手", version="1.0.0", lifespan=lifespan)


class SearchRequest(BaseModel):
    query: str
    top_k: int = 10


class QARequest(BaseModel):
    query: str
    top_k: int = 5


class ConclusionRequest(BaseModel):
    topic: str
    conclusion: str
    source_text: str = ""


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/import")
async def import_document(file: UploadFile = File(...)):
    filename = file.filename or "unknown"
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".md", ".txt", ".markdown"):
        raise HTTPException(400, f"不支持的文件类型: {suffix}，仅支持 PDF/Markdown/TXT")

    content = await file.read()
    if not content:
        raise HTTPException(400, "文件内容为空")

    try:
        chunks, content_hash = parse_file_from_bytes(filename, content)
    except Exception as e:
        raise HTTPException(500, f"文件解析失败: {str(e)}")

    if not chunks:
        raise HTTPException(400, "未能从文件中提取到有效段落")

    if document_exists(content_hash):
        return {"status": "duplicate", "message": "该文件已导入过", "filename": filename}

    doc_id = insert_document(filename, suffix.lstrip("."), content_hash)

    texts = [c["text"] for c in chunks]
    logger.info(f"Generating embeddings for {len(texts)} chunks from {filename}")
    embeddings = encode_texts(texts)

    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        insert_chunk(
            doc_id=doc_id,
            chunk_index=i,
            page_number=chunk.get("page_number"),
            paragraph_index=chunk.get("paragraph_index"),
            text=chunk["text"],
            embedding=emb,
        )

    update_document_chunk_count(doc_id, len(chunks))

    return {
        "status": "success",
        "document_id": doc_id,
        "filename": filename,
        "chunk_count": len(chunks),
    }


@app.post("/api/import_batch")
async def import_batch(files: list[UploadFile] = File(...)):
    results = []
    for file in files:
        try:
            content = await file.read()
            filename = file.filename or "unknown"
            chunks, content_hash = parse_file_from_bytes(filename, content)
            if document_exists(content_hash):
                results.append({"filename": filename, "status": "duplicate"})
                continue
            suffix = Path(filename).suffix.lower()
            doc_id = insert_document(filename, suffix.lstrip("."), content_hash)
            texts = [c["text"] for c in chunks]
            embeddings = encode_texts(texts)
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                insert_chunk(doc_id, i, chunk.get("page_number"), chunk.get("paragraph_index"), chunk["text"], emb)
            update_document_chunk_count(doc_id, len(chunks))
            results.append({"filename": filename, "status": "success", "chunks": len(chunks)})
        except Exception as e:
            results.append({"filename": file.filename, "status": "error", "error": str(e)})
    return {"results": results}


@app.get("/api/documents")
async def list_documents():
    return {"documents": get_documents()}


@app.post("/api/search")
async def search(req: SearchRequest):
    if not req.query.strip():
        raise HTTPException(400, "查询不能为空")
    expansion = get_search_expansion()
    expanded_query = req.query
    if expansion:
        expanded_query = req.query + " " + " ".join(expansion[:3])
    results = hybrid_search(expanded_query, top_k=req.top_k)
    on_search(req.query, results)
    return {"query": req.query, "expanded_query": expanded_query if expansion else None, "count": len(results), "results": results}


@app.post("/api/qa")
async def qa(req: QARequest):
    if not req.query.strip():
        raise HTTPException(400, "问题不能为空")
    result = answer_question(req.query, top_k=req.top_k)
    on_search(req.query, result.get("citations", []))
    return result


@app.get("/api/memory/recall")
async def memory_recall():
    return get_recall_summary()


@app.post("/api/memory/conclusion")
async def save_conclusion(req: ConclusionRequest):
    on_conclusion_confirmed(req.topic, req.conclusion, req.source_text)
    return {"status": "saved"}


@app.get("/api/memory/topics")
async def memory_topics():
    from app.powercontext_client import search_memory
    memories = search_memory("research_topic", limit=10)
    topics = [{"topic": m["text"]} for m in memories if m.get("kind") == "research_topic"]
    return {"topics": topics}


@app.get("/api/memory/status")
async def memory_status():
    return check_powercontext_status()


@app.get("/api/memory/expansion")
async def memory_expansion():
    return {"terms": get_search_expansion()}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=SERVER_PORT, reload=True)
