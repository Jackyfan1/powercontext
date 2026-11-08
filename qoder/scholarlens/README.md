# 研阅 ScholarLens — 文献与资料语义检索助手

> 学生/科研人员的论文、课程笔记散落在多个文件夹，找某个结论的出处只能一篇篇翻？复习时找不到上学期笔记？ScholarLens 通过本地向量语义检索 + PowerContext 记忆增强，让你快速定位文献段落、追溯结论出处。

## 功能特性

### 1. 文献导入
- 支持 **PDF / Markdown / TXT** 格式
- 自动按段落切块，记录出处（文件名 + 页码/段落序号）
- 使用 sentence-transformers 生成语义向量，SQLite 持久化
- 支持批量导入，自动去重（基于内容哈希）

### 2. 语义检索
- 输入自然语言问题，返回相关段落列表
- **混合排序**：向量余弦相似度（70%）+ 关键词打分（30%）
- 每条结果展示出处、相关度分数、向量/关键词分项得分

### 3. 智能问答
- 基于检索段落生成带引用标记 `[1] [2]` 的回答
- 配置 OpenAI API Key 时使用 LLM 生成回答
- **未配置 Key 时自动降级为摘录式回答**（展示最相关段落与出处）

### 4. 研究记忆（PowerContext）
- 本机 PowerContext Server + SQLite 持久化
- 自动保存研究主题、历史检索、已确认结论
- 下次打开自动召回，展示"上次你在查…"
- 历史主题用于扩展检索

## 技术架构

```
┌─────────────────────────────────────────────────┐
│                  Frontend SPA                     │
│            (HTML + CSS + Vanilla JS)              │
├─────────────────────────────────────────────────┤
│                  FastAPI Backend                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ Import   │ │ Search   │ │ Q&A              │ │
│  │ Module   │ │ Engine   │ │ Module           │ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
│  ┌──────────────────────────────────────────────┐│
│  │          PowerContext Client (HTTP)           ││
│  └──────────────────────────────────────────────┘│
├─────────────────────────────────────────────────┤
│  sentence-transformers │ SQLite │ numpy          │
├─────────────────────────────────────────────────┤
│          PowerContext Server (port 8710)          │
│          (真实 PowerContext 1.5.0 + SQLite)       │
└─────────────────────────────────────────────────┘
```

## 环境要求

- **Python**: 3.11+
- **Node.js**: v20+ (仅开发时需要)
- **操作系统**: Windows / macOS / Linux
- **磁盘空间**: ≥ 2 GB（embedding 模型约 500 MB）

## 依赖版本

| 依赖 | 版本 |
|------|------|
| fastapi | 0.115.6 |
| uvicorn | 0.34.0 |
| sentence-transformers | 3.3.1 |
| torch | ≥ 2.1.0 |
| numpy | ≥ 1.24.0 |
| PyPDF2 | 3.0.1 |
| pdfplumber | 0.11.4 |
| httpx | 0.28.1 |
| python-dotenv | 1.0.1 |
| powercontext | ≥ 1.5.0 |
| openai | ≥ 1.0.0 (可选) |

## 安装与启动

### 1. 克隆/进入项目目录

```bash
cd qoder/scholarlens
```

### 2. 创建 Python 虚拟环境

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

> 首次安装 sentence-transformers 会自动下载 PyTorch（约 2 GB），请耐心等待。

### 4. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，按需填入 OpenAI API Key（不填则使用摘录式回答）
```

### 5. 启动 PowerContext 记忆服务

PowerContext 是真实的本机上下文记忆服务（v1.5.0），ScholarLens 通过 HTTP API 与之通信。

```bash
# 启动 PowerContext Server（v1.5.0，HTTP 端口通过环境变量指定为 8710）
# Windows PowerShell:
$env:POWERCONTEXT_HOME="$PWD/.pc-home"
$env:POWERCONTEXT_SERVER_HTTP_PORT="8710"
powercontext server run

# macOS / Linux:
export POWERCONTEXT_HOME="$PWD/.pc-home"
export POWERCONTEXT_SERVER_HTTP_PORT=8710
powercontext server run
```

> 注意：`POWERCONTEXT_HOME` 指向本地数据目录（SQLite 存储），首次启动会自动初始化。

验证服务已启动：
```bash
curl http://127.0.0.1:8710/health/live
# 返回: {"status":"ok"}
```

ScholarLens 使用的 PowerContext 真实 API：
- `GET /health/live` — 存活检查
- `GET /v1/scopes/default` — 获取默认 scope_id
- `POST /v1/memory/remember` — 写入记忆 (body: {scope_id, kind, text})
- `POST /v1/memory/search` — 检索记忆 (body: {scope_id, query, limit})

### 6. 启动 ScholarLens 主服务

```bash
# 在第二个终端启动 ScholarLens（端口 8000）
python -m app.main
```

看到以下输出表示启动成功：
```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 7. 打开浏览器访问

```
http://localhost:8000
```

## 快速验证

### 导入示例文献

```bash
# 使用 curl 导入 sample_data 中的文献
curl -X POST http://localhost:8000/api/import \
  -F "file=@sample_data/transformer_attention.md"

curl -X POST http://localhost:8000/api/import \
  -F "file=@sample_data/bert_paper.txt"

curl -X POST http://localhost:8000/api/import \
  -F "file=@sample_data/cnn_notes.md"
```

### 语义检索

```bash
curl -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "什么是自注意力机制", "top_k": 3}'
```

### 智能问答

```bash
curl -X POST http://localhost:8000/api/qa \
  -H "Content-Type: application/json" \
  -d '{"query": "BERT 和 GPT 有什么区别"}'
```

### 记忆召回

```bash
# 查看研究记忆
curl http://localhost:8000/api/memory/recall

# 查看 PowerContext 状态
curl http://localhost:8000/api/memory/status
```

## 目录结构

```
scholarlens/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 主应用
│   ├── config.py               # 配置管理
│   ├── database.py             # SQLite 数据库操作
│   ├── document_parser.py      # PDF/MD/TXT 解析与切块
│   ├── embeddings.py           # sentence-transformers 向量编码
│   ├── search.py               # 混合检索引擎
│   ├── qa.py                   # 问答模块（LLM/摘录降级）
│   ├── memory.py               # 记忆高层接口
│   ├── powercontext_client.py  # PowerContext HTTP 客户端（真实 API）
│   └── static/
│       ├── index.html          # 前端 SPA
│       ├── style.css
│       └── app.js
├── sample_data/                # 示例文献（4 篇）
├── data/                       # 运行时数据（自动创建）
├── docs/
│   └── dev-log.md              # 开发记录
├── .env.example
├── requirements.txt
└── README.md
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/import` | 上传并导入文献 |
| GET | `/api/documents` | 列出已导入文献 |
| POST | `/api/search` | 语义检索 |
| POST | `/api/qa` | 智能问答 |
| GET | `/api/memory/recall` | 召回研究记忆 |
| POST | `/api/memory/conclusion` | 保存结论 |
| GET | `/api/memory/topics` | 查看研究主题 |
| GET | `/api/memory/status` | PowerContext 状态 |
| GET | `/api/memory/expansion` | 检索扩展词 |

## 常见问题

**Q: 首次运行很慢？**
A: sentence-transformers 首次运行会自动下载 embedding 模型（all-MiniLM-L6-v2，约 80 MB），之后会缓存。国内网络可先设置 `HF_ENDPOINT=https://hf-mirror.com`。

**Q: PowerContext 未连接怎么办？**
A: 确保已在端口 8710 启动 PowerContext Server (`powercontext server run`，并设置 `POWERCONTEXT_SERVER_HTTP_PORT=8710`)。未连接时记忆功能优雅降级（available=false），导入/检索/问答仍可正常工作。

**Q: 8000 端口被占用怎么办？**
A: 改用 `python -m uvicorn app.main:app --port 8010` 启动，并把 `.env` 里的 `SERVER_PORT` 同步为 8010。

**Q: 如何使用 LLM 生成回答？**
A: 在 `.env` 中配置 `OPENAI_API_KEY`，重启服务即可。未配置时自动降级为摘录式回答。

## License

MIT
