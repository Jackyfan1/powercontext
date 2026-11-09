# StepSeek · 端侧知识库

面向「端侧 AI 推理性能优化与 seekdb 混合检索」赛题的可复现实现：**文档在浏览器本地向量化，原文/向量/元数据落到 seekdb，向量与全文的混合检索在 seekdb 内一条 SQL 完成**。

```
┌────────────────────────── 浏览器（本机） ──────────────────────────┐
│ 语料粘贴/上传 ──▶ 切块 ──▶ transformers.js (ONNX Runtime Web)      │
│                              │ WebGPU 优先，失败自动降级 WASM       │
│                              ▼                                     │
│                         512 维向量（本地计算，无云端推理）           │
│ 查询 ──▶ 本地编码 ──▶ 展示排序结果 / 会话历史（IndexedDB 无关）     │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ HTTP（本机 127.0.0.1）
┌──────────────────────────────▼─────────────────────────────────────┐
│ FastAPI（Python）: 静态前端 + 数据接口                              │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ MySQL 协议（127.0.0.1:2881）
┌──────────────────────────────▼─────────────────────────────────────┐
│ seekdb 1.4                                                         │
│  chunks.embedding VECTOR(512) + HNSW 向量索引（cosine）             │
│  chunks(title, content) FULLTEXT 索引（ngram，中文可用）            │
│  collections / documents / sessions 三张业务表                      │
└────────────────────────────────────────────────────────────────────┘
```

## 赛题检查项对照

| 检查项 | 本实现的落点 |
| --- | --- |
| 完整流程 | 页面「导入语料 → 本地向量化 → 写入 seekdb → 输入查询 → 展示排序结果」全流程可操作 |
| 本地推理 | embedding 在 Web Worker 内用 ONNX Runtime Web 计算，`env.allowRemoteModels = false`，模型文件由本机静态目录提供，无任何云端推理调用 |
| 模型量化 | 同时提供 FP32 / FP16 / INT8(q8) 三份权重；页面「量化对比」实测体积、单条耗时、与 FP32 的平均/最低余弦、Top-1 一致率 |
| 后端与降级 | 启动时检测 `navigator.gpu` 并请求 adapter，可用则 `device=webgpu, dtype=fp16`；不可用或初始化失败自动 `device=wasm, dtype=q8`，顶部芯片与横幅明确提示 |
| seekdb 数据层 | 原文、向量、元数据（标题/分块序号/字符数/模型/精度）全部落 seekdb；混合检索在 `POST /api/search` 的一条 SQL 内完成，不在应用层重排 |
| 状态与刷新 | 多次导入为增量追加，导入后自动用全文检索验证新文档可命中，再 `ANALYZE TABLE` 刷新统计；集合与会话（历史查询）存 seekdb，刷新页面后恢复 |
| 可复现性 | 见下方「快速开始」，全部命令可直接执行 |

## 目录结构

```
stepfun/
├── app/
│   ├── config.py            # 环境变量配置
│   ├── db.py                # seekdb 建库建表、写入、混合检索 SQL、会话
│   ├── main.py              # FastAPI 接口 + 静态站点
│   └── static/
│       ├── index.html       # 三栏界面：集合/导入、检索、状态与量化
│       ├── app.js           # 前端主逻辑（切块、导入、检索、会话、量化实测）
│       ├── embed-worker.js  # Web Worker：transformers.js 本地推理
│       ├── vendor/          # transformers.min.js + ORT wasm（脚本拉取）
│       └── models/          # bge-small-zh-v1.5 三精度 ONNX（脚本拉取）
├── sample_data/             # 4 篇示例语料（seekdb / 端侧推理 / 量化 / RAG）
├── scripts/
│   ├── prepare_assets.py    # 一键下载模型与前端库
│   ├── init_db.py           # 建库建表并打印索引
│   └── smoke_test.py        # 服务端链路冒烟测试
├── docs/
│   ├── dev-log.md           # 开发与排错记录
│   ├── quantization.md      # 量化方案与实测方法
│   └── quantization-results.json  # 页面实测后自动生成
├── requirements.txt
└── .env.example
```

## 快速开始

前置：Windows + WSL2（已安装 `oceanbase-desktop` 发行版并带 `/usr/bin/seekdb`）、Python 3.12+。

### 1. 启动 seekdb

```powershell
# 建议在独立终端前台运行，保持 WSL 会话存活
wsl -d oceanbase-desktop -u root -- /usr/bin/seekdb --base-dir /root/seekdb_stepfun --port 2881 --nodaemon
```

> 没有现成 seekdb 时，可从 OceanBase 官网获取 seekdb 1.4 的单机包，解压后执行 `./seekdb --base-dir <dir> --port 2881 --nodaemon`。

### 2. 安装 Python 依赖

```powershell
cd stepfun
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. 准备前端资产（模型 + 推理库）

```powershell
.\.venv\Scripts\python.exe scripts\prepare_assets.py
```

从 `hf-mirror.com` 下载 `bge-small-zh-v1.5` 的 FP32/FP16/INT8 权重（约 95/47/24 MB），并从 npm registry 解包 `transformers.js` 与 `onnxruntime-web` 的 wasm 产物。已存在的文件会跳过。

### 4. 初始化数据库

```powershell
copy .env.example .env
.\.venv\Scripts\python.exe scripts\init_db.py
```

### 5. 启动应用

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8011
```

浏览器打开 <http://127.0.0.1:8011>：

1. 顶部芯片显示 `seekdb …`、`WebGPU · FP16`（或 `WASM · INT8（已降级）`）、`bge-small-zh-v1.5 · …`；
2. 左栏点「载入示例语料」可一键导入 `sample_data/` 的 4 篇文档（仍走完整的本地向量化链路），也可自己填写标题、粘贴文本导入；
3. 中间输入问题 →「检索」，查看每条结果的 `score / 向量分 / 全文分` 与耗时；
4. 右栏「刷新索引」执行 `ANALYZE TABLE`；「运行实测」生成三精度对比。

### 6. 服务端链路自测

```powershell
.\.venv\Scripts\python.exe scripts\smoke_test.py
```

## 关键实现说明

### 检索 SQL（混合模式）

```sql
SELECT * FROM (
  SELECT c.id, c.doc_id, c.seq, c.title, c.content, c.meta,
         1 - cosine_distance(c.embedding, %s) AS vec_score,
         MATCH(c.title, c.content) AGAINST (%s) AS text_raw,
         MATCH(c.title, c.content) AGAINST (%s) / (1 + MATCH(c.title, c.content) AGAINST (%s)) AS text_score
  FROM chunks c
  WHERE c.collection_id = %s
) t
ORDER BY %s * t.vec_score + %s * t.text_score DESC
LIMIT %s
```

- 向量分：`1 - cosine_distance`，走 `VECTOR INDEX vidx (embedding) WITH (distance=cosine, type=hnsw)`；
- 全文分：`MATCH ... AGAINST` 经 `score/(1+score)` 归一到 0~1，走 `FULLTEXT INDEX fidx (title, content) WITH PARSER ngram`；
- 权重默认 `0.7 / 0.3`，页面滑块可调（仅混合模式）；
- 纯向量模式 `ORDER BY vec_score DESC`，纯全文模式在 `WHERE` 中用 `MATCH` 过滤。

### 增量导入与刷新

每次导入生成新的 `documents` 记录与若干 `chunks`，不触碰已有数据；插入后立即用标题做一次全文检索，确认新文档可命中（导入日志中的「增量验证」），再执行 `ANALYZE TABLE chunks` 刷新优化器统计。HNSW 与全文索引随插入自动维护，不需要重建。

### 会话状态

`POST /api/sessions` 创建会话，`PUT /api/sessions/{id}` 保存历史查询（问题、模式、SQL 耗时、Top3 标题与分数）。前端把 `session_id` 放在 `localStorage`，刷新后从 seekdb 读回并渲染「本次会话」列表，点击可复跑。

### 量化与降级

- 模型：`bge-small-zh-v1.5`（BAAI，12 层 BERT，24M 参数，512 维）；
- 权重来源：`Xenova/bge-small-zh-v1.5`（transformers.js 转换版），三份 ONNX：`model.onnx` / `model_fp16.onnx` / `model_quantized.onnx`；
- `dtype=fp16` 仅用于 WebGPU，`dtype=q8` 用于 WASM（每通道 8bit 动态量化）；
- 判定逻辑在 `app/static/app.js` 的 `detectWebGPU()` 与 `embed-worker.js` 的 `init()`：WebGPU 初始化抛错时自动重建为 WASM/INT8 并回传 `fallback: true`。

实测数据见 [docs/quantization.md](docs/quantization.md)。

## 已知限制

- 表结构按 512 维固定；换用其他维度模型需同步修改 `VECTOR_DIM` 并重建 `chunks` 表。
- 混合检索的加权排序使用派生表，数据量很大时 OceanBase 可能放弃 HNSW 索引而走全表扫描；生产场景应按集合分表或使用 `WHERE` 预过滤 + 向量 `LIMIT` 召回候选后再加权。
- 浏览器需允许 `SharedArrayBuffer`（服务端已下发 COOP/COEP 头）；若被代理改写，WASM 会退化为单线程，功能不受影响。
