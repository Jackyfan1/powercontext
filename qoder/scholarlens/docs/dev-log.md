# ScholarLens 开发记录

## 项目概述

研阅 ScholarLens 是一个文献与资料语义检索助手，旨在解决学生/科研人员论文笔记分散、难以快速定位出处的痛点。

## 开发日志

### 2026-11-08 项目初始化

#### 完成内容

1. **项目结构搭建** — `qoder/scholarlens/` 目录，requirements.txt、.env.example、config.py
2. **文档解析** (`app/document_parser.py`) — PDF(pdfplumber)/Markdown/TXT 解析，段落切块(800字/100重叠)，内容哈希去重
3. **向量引擎** (`app/embeddings.py`) — sentence-transformers all-MiniLM-L6-v2，批量编码+归一化，HF_HUB_OFFLINE=1 使用本地缓存
4. **混合检索** (`app/search.py`) — 向量余弦(0.7) + 关键词(0.3) 融合排序，出处格式化
5. **问答模块** (`app/qa.py`) — LLM 模式(OpenAI) / 摘录模式(无Key降级)
6. **PowerContext 真实集成** (`app/powercontext_client.py`) — 连接本机 PowerContext Server v1.5.0 (port 8710)，使用真实 API
7. **FastAPI 后端** (`app/main.py`) — 全部 API 端点，静态文件托管，查询扩展
8. **前端 SPA** — 原生 HTML/CSS/JS，四标签页(导入/检索/问答/记忆)，拖拽上传，记忆召回横幅
9. **示例数据** — 4 篇文献 (Transformer/BERT/CNN/强化学习)

### 2026-11-08 修正：PowerContext 真实接入

- 删除自造的 `app/powercontext_server.py`（假服务）
- 改用真实 PowerContext Server (port 8710) 的 API：
  - `GET /health/live` — 存活检查
  - `GET /v1/scopes/default` — 获取 scope_id
  - `POST /v1/memory/remember` — 写入记忆 {scope_id, kind, text}
  - `POST /v1/memory/search` — 检索记忆 {scope_id, query, limit}
- 每次检索后 remember "检索主题+结果数+最佳片段"
- 用户确认结论时写入 kind=conclusion
- 首页通过 search 拉取"上次你在查…"
- 历史主题拼入检索做查询扩展（真实改变检索结果）
- httpx 统一 UTF-8、超时 5s、trust_env=False（避免代理干扰）
- 服务不可用时优雅降级 available=false

---

### 冒烟测试记录（真实执行）

#### 测试环境
- Python: 3.14.5 (`C:\Users\LENOVO\AppData\Local\Programs\Python\Python314\python.exe`)
- OS: Windows 10 (10.0.26300)
- ScholarLens: http://127.0.0.1:8010 (8000 被占用)
- PowerContext: http://127.0.0.1:8710 (v1.5.0, SQLite, 基础记忆模式)
- Embedding: all-MiniLM-L6-v2 (本地缓存, HF_HUB_OFFLINE=1)
- 测试脚本: `smoke_test.py` (httpx, trust_env=False)

#### 测试 1: 文献导入

请求: `POST /api/import` (multipart file upload)

响应:
```json
{"status": "success", "document_id": 2, "filename": "transformer_attention.md", "chunk_count": 15}
{"status": "success", "document_id": 3, "filename": "bert_paper.txt", "chunk_count": 8}
{"status": "success", "document_id": 4, "filename": "cnn_notes.md", "chunk_count": 15}
```

重复导入检测:
```json
{"status": "duplicate", "message": "该文件已导入过", "filename": "transformer_attention.md"}
```

**结果**: ✅ 通过 — 3 篇文献共 38 个段落成功导入并生成向量

#### 测试 2: 语义检索

请求: `POST /api/search` body: `{"query": "什么是自注意力机制", "top_k": 3}`

响应:
```json
{
  "query": "什么是自注意力机制",
  "expanded_query": "什么是自注意力机制 卷积神经网络 什么是自注意力机制",
  "count": 3,
  "results": [
    {"id": 7, "text": "3. 多头注意力", "score": 0.7, "vector_score": 0.663, "source": "transformer_attention.md | para.6"},
    {"id": 4, "text": "2. 自注意力机制", "score": 0.698, "vector_score": 0.6612, "source": "transformer_attention.md | para.3"},
    {"id": 25, "text": "什么是卷积神经网络", "score": 0.6535, "vector_score": 0.6039, "source": "cnn_notes.md | para.1"}
  ]
}
```

**结果**: ✅ 通过 — 正确返回 Transformer 相关段落；查询扩展真实生效（expanded_query 包含历史主题）

#### 测试 3: 智能问答（摘录模式）

请求: `POST /api/qa` body: `{"query": "BERT和GPT有什么区别"}`

响应:
```json
{"mode": "excerpt", "citations": [5条], "answer": "基于检索结果，以下是最相关的段落（共 5 条）：\n[1] (相关度: 0.70，来源: ...)"}
```

**结果**: ✅ 通过 — 未配置 LLM Key 时正确降级为摘录模式，返回 5 条引用

#### 测试 4: 记忆状态

请求: `GET /api/memory/status`

响应:
```json
{"available": true, "message": "PowerContext 已连接"}
```

**结果**: ✅ 通过

#### 测试 5: 记忆召回

请求: `GET /api/memory/recall`

响应:
```json
{
  "available": true,
  "topics": [{"topic": "什么是自注意力机制"}, {"topic": "BERT和GPT有什么区别"}, ...],
  "recent_searches": [{"query": "检索主题: 什么是自注意力机制 | 结果数: 3 | 最佳片段: ...", "kind": "search_history"}],
  "conclusions": [{"conclusion": "[Transformer架构] 自注意力机制是Transformer的核心，通过Q/K/V向量计算注意力权重"}],
  "summary": "上次你在查「...」；最近搜索过「...」；已确认 2 条结论"
}
```

**结果**: ✅ 通过 — 自动记录检索历史、主题、结论，生成召回摘要

#### 测试 6: 保存结论

请求: `POST /api/memory/conclusion` body: `{"topic": "Transformer架构", "conclusion": "自注意力机制是Transformer的核心，通过Q/K/V向量计算注意力权重"}`

响应: `{"status": "saved"}`

**结果**: ✅ 通过

#### 测试 7: 查询扩展验证

请求: `POST /api/search` body: `{"query": "卷积神经网络", "top_k": 3}`

响应:
```json
{
  "query": "卷积神经网络",
  "expanded_query": "卷积神经网络 什么是自注意力机制 BERT和GPT有什么区别",
  "count": 3,
  "results": [
    {"id": 25, "text": "什么是卷积神经网络", "score": 0.715, "source": "cnn_notes.md | para.1"},
    {"id": 29, "score": 0.6786, "source": "cnn_notes.md | para.5"},
    {"id": 26, "score": 0.6063, "source": "cnn_notes.md | para.2"}
  ]
}
```

**结果**: ✅ 通过 — 历史主题真实拼入查询，expanded_query 可见扩展词

---

### 真实 PowerContext API 请求与响应

#### POST /v1/memory/remember

请求:
```json
{
  "scope_id": "scp_6daw1m899x4hwzd9xyckx75dbr",
  "kind": "dev_log_test",
  "text": "ScholarLens 冒烟测试记录：四接口全部通过"
}
```

响应:
```json
{
  "changed": true,
  "records": [
    {
      "artifact": {
        "family": "atomic-memory",
        "artifact_id": "art_d38e34748e354a87afb562206edebe32",
        "revision": 1
      },
      "kind": "dev_log_test",
      "text": "ScholarLens 冒烟测试记录：四接口全部通过",
      "state": "active",
      "state_version": 0,
      "merged_into_id": null
    }
  ]
}
```

#### POST /v1/memory/search

请求:
```json
{
  "scope_id": "scp_6daw1m899x4hwzd9xyckx75dbr",
  "query": "ScholarLens 冒烟测试",
  "limit": 3
}
```

响应:
```json
{
  "mode": "text",
  "hits": [
    {
      "memory": {
        "artifact": {
          "family": "atomic-memory",
          "artifact_id": "art_d38e34748e354a87afb562206edebe32",
          "revision": 1
        },
        "kind": "dev_log_test",
        "text": "ScholarLens 冒烟测试记录：四接口全部通过",
        "state": "active",
        "state_version": 0,
        "merged_into_id": null
      },
      "score": 0.01639344262295082,
      "matched_by": ["text"]
    }
  ]
}
```

---

### 冒烟测试总结

| # | 测试项 | 结果 |
|---|--------|------|
| 1 | 文献导入（3 篇，38 chunks） | ✅ 通过 |
| 2 | 重复导入检测 | ✅ 通过 |
| 3 | 语义检索 + 查询扩展 | ✅ 通过 |
| 4 | 智能问答（摘录模式） | ✅ 通过 |
| 5 | 记忆状态 (available=true) | ✅ 通过 |
| 6 | 记忆召回（主题/历史/结论） | ✅ 通过 |
| 7 | 保存结论 | ✅ 通过 |
| 8 | PowerContext /v1/memory/remember | ✅ 通过 |
| 9 | PowerContext /v1/memory/search | ✅ 通过 |
| 10 | 查询扩展真实改变结果 | ✅ 通过 |

**全部 10 项测试通过**，四个核心接口（导入/检索/问答/记忆）均工作正常。PowerContext 使用真实 Server (port 8710) 的 `/v1/memory/remember` 和 `/v1/memory/search` API，非自造服务。
