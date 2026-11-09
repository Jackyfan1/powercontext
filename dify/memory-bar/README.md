# 深夜记忆调酒馆 · Midnight Memory Bar

> **Just for Fun：把今天调成一杯酒，把这句话存进 seekdb。**

一个用 **Dify Chatflow** 编排、以 **OceanBase seekdb** 作为长期记忆库的小应用：
客人说一句话，调酒师阿浮从 seekdb 里检索「客人过去的记忆」与「风味卡」，
调出一杯有名字、有配方、有回甘的酒；同时把这句话封存回 seekdb，下次还能被想起来。

同一句话，第二次来会有不同的回甘——因为酒柜里真的多了东西。

---

## 1. 怎么玩

进入 Dify 应用后直接聊天，共三种意图：

| 意图 | 触发示例 | 行为 |
| --- | --- | --- |
| 调酒（默认） | 「今天加班到很晚，有点想家」 | 混合检索旧记忆 + 风味卡，生成酒名/杯型/配方/酒语/回甘，并写入订单 |
| 逛酒柜 | 「看看我的酒柜」「我上次说了什么」 | 列出 seekdb 里最近的记忆 |
| 打烊 | 「打烊吧，晚安」 | 统计今晚存下的记忆与喝过的酒，道别 |

酒名由「用户 + 原话 + 日期」播种生成，同样的输入当天稳定、换一天会变；
回甘优先引用检索到的真实旧记忆，**不会编造**（回复文案全部由后端依据检索结果生成，无需在 Dify 里配置任何大模型）。

---

## 2. 与赛题要求的对应

| 要求 | 本作品如何满足 |
| --- | --- |
| 必须使用 Dify | `dsl/midnight_memory_bar.yml` 是可导入的 Dify Chatflow（DSL 0.7.0，4 个节点：开始 → 查阅酒柜 → 封存记忆 → 回复） |
| 实际使用 seekdb（或 PowerContext） | 记忆与风味卡全部存于 seekdb `memory_bar` 库；向量召回走 HNSW 向量索引，全文召回走 ngram 全文索引，混合排序在一条 SQL 内完成 |
| 应用能运行、可按样例体验 | 后端零框架魔法（FastAPI），`scripts/init_db.py` + `scripts/seed.py` 一键初始化，`scripts/smoke_test.py` 7 项全通过 |
| 提交 Dify DSL / 运行说明 / 验证样例 | DSL 见 `dsl/`；运行说明即本文；验证见第 6 节与 `docs/verification.md`、`docs/smoke_results.json` |
| 创意、趣味、体验 | 每杯酒的命名、配方、酒语、回甘都不同；长期使用会长出个人「酒柜」，越用越有味道 |

---

## 3. 架构

```
┌──────────────────────────── Dify Chatflow ────────────────────────────┐
│  开始 ──► 查阅酒柜 (HTTP /v1/recall) ──► 封存记忆 (HTTP /v1/remember) ──► 回复 │
└───────────────┬───────────────────────────────────────────┬───────────┘
                │ POST /v1/recall                            │ POST /v1/remember
                ▼                                            ▼
┌────────────────────────── 深夜记忆调酒馆 API (8787) ──────────────────────────┐
│  FastAPI · bge-small-zh-v1.5 (ONNX, 512 维) · 调酒/酒柜/打烊三种意图          │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                ▼
┌───────────────────────────── OceanBase seekdb ──────────────────────────────┐
│ memories：VECTOR(512) + HNSW 向量索引 + ngram 全文索引（混合检索一条 SQL）    │
│ orders  ：每杯酒的酒单（酒名 / 心情 / 配方 / 原话 / 时间）                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. 快速开始

### 4.1 前置条件

- Python 3.10+
- OceanBase **seekdb 1.4+**（默认监听 `127.0.0.1:2881`，MySQL 协议）
- Dify 1.x（Cloud 或自托管均可）

seekdb 启动示例（二选一）：

```bash
# Linux / WSL
./seekdb --base-dir ./seekdb_data --port 2881 --nodaemon

# Docker
docker run -d --name seekdb -p 2881:2881 oceanbase/seekdb:latest
```

### 4.2 启动记忆服务

```bash
cd dify/memory-bar

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # 按需修改 seekdb 地址 / 端口

python scripts/init_db.py   # 建库、建表、建向量与全文索引
python scripts/seed.py      # 写入 20 张风味卡 + demo 的 6 段演示记忆
python -m app.main          # 启动 HTTP 服务，监听 8787
```

首次启动会下载 `bge-small-zh-v1.5` 的 ONNX 权重（约 24 MB）。
国内网络可在 `.env` 中打开 `HF_ENDPOINT=https://hf-mirror.com`。
若下载失败，服务会退化为哈希向量并继续运行，`/v1/health` 会显示 `"mode": "hash"`。

健康检查：

```bash
curl http://127.0.0.1:8787/v1/health
```

返回中可看到 seekdb 版本、`vidx_memories`（VECTOR）与 `fidx_memories`（FULLTEXT）索引。

### 4.3 导入 Dify 应用

1. Dify →「工作室」→「创建应用」→「导入 DSL 文件」，选择 `dsl/midnight_memory_bar.yml`。
2. 只改应用环境变量 `BAR_BASE_URL`（节点里引用 `{{#env.BAR_BASE_URL#}}`，不用动节点）：

   | Dify 部署方式 | `BAR_BASE_URL` |
   | --- | --- |
   | Docker 自托管（默认写法） | `http://host.docker.internal:8787` |
   | 本机源码 / 同机进程 | `http://127.0.0.1:8787` |
   | Dify Cloud | 用 cloudflared / ngrok 把 8787 暴露成公网地址后替换 |

3. 发布应用，用下面的问题试玩：

> 本 DSL 为 Dify 1.18 的 0.7.0 结构：HTTP 节点的 JSON body 严格保持「单条目整段 JSON」
> （多个 key/value 会被 Dify 判为 `json body type should have exactly one item`）；
> 两个 HTTP 节点都设置了 `error_strategy: default-value`，后端未启动时主流程仍然走完并给出提示，不会中断对话。
> 应用不依赖任何大模型 API Key，导入即可体验。

```
今天加班到很晚，有点想家
楼下的猫今天肯让我摸头了
看看我的酒柜
打烊吧，晚安
```

> 想先看效果、不装 Dify 也可以直接调后端：
> `curl -X POST http://127.0.0.1:8787/v1/recall -H "Content-Type: application/json" -d '{"query":"今天加班到很晚，有点想家","user_id":"demo"}'`

---

## 5. seekdb 里的关键实现

### 5.1 表结构：一张表同时承载向量与全文索引

```sql
CREATE TABLE memories (
  id BIGINT NOT NULL AUTO_INCREMENT,
  user_id VARCHAR(64) NOT NULL,
  kind VARCHAR(16) NOT NULL,              -- drop=客人记忆 / flavor=风味卡
  title VARCHAR(128) NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  mood VARCHAR(32) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL,
  embedding VECTOR(512),
  PRIMARY KEY (id),
  VECTOR INDEX vidx_memories (embedding)
    WITH (distance=cosine, type=hnsw, m=16, ef_construction=200),
  FULLTEXT INDEX fidx_memories (title, content) WITH PARSER ngram,
  KEY idx_user_kind (user_id, kind, created_at)
);
```

### 5.2 混合检索：一条 SQL 内完成加权排序

```sql
SELECT * FROM (
  SELECT m.id, m.title, m.content, m.mood, m.created_at,
         1 - cosine_distance(m.embedding, %s) AS vec_score,
         MATCH(m.title, m.content) AGAINST (%s) AS text_raw,
         MATCH(m.title, m.content) AGAINST (%s)
           / (1 + MATCH(m.title, m.content) AGAINST (%s)) AS text_score
  FROM memories m
  WHERE m.user_id = %s AND m.kind = %s
) t
ORDER BY 0.7 * t.vec_score + 0.3 * t.text_score DESC
LIMIT %s
```

- 向量分负责「意思相近」，全文分负责「关键词命中」（中文用 ngram 分词）；
- 两路召回与加权排序都下推到 seekdb，应用层只拿 Top-K，不在 Python 里重复算；
- `/v1/recall` 的返回会带上每条记忆的 `vec_score` / `text_score` / `score`，方便评审核对。

### 5.3 写入路径

`POST /v1/remember` 把客人的原话编码成 512 维向量后写入 `memories`，
下次同一用户的检索即可召回——第 6 节的「第二轮回甘引用了第一轮原话」就是这条链路。

---

## 6. 验证样例（本机实测）

环境：Windows + WSL seekdb 1.4.0.0（2881）+ Python 3.14 + `bge-small-zh-v1.5` ONNX。

`python scripts/smoke_test.py`：**7/7 通过**（完整结果见 `docs/smoke_results.json`）。

| 用例 | 结果 |
| --- | --- |
| 健康检查 / seekdb 索引 | PASS（`vidx_memories` VECTOR、`fidx_memories` FULLTEXT 均在） |
| 混合检索 + 调酒草稿 | PASS（命中「加班到十一点」，向量 0.595 / 全文 0.851 / 加权 0.672） |
| 写入新记忆 | PASS |
| 语义召回刚写入的记忆 | PASS（Top-1 即刚写入的记录，向量 0.796 / 全文 0.906） |
| 酒柜意图 | PASS |
| 打烊意图 | PASS |
| 酒柜与酒单持久化 | PASS |

同一用户连续对话（真实输出）：

```text
Q: 今天加班到很晚，有点想家
🍸 **今晚的酒：「晚风归途」**
*厚底威士忌杯 · 无酒精 · 今夜特调*

**配方**
- **苦橙皮** —— 把一天的疲惫削成一条卷，苦味先到，甜味后放。适合加班后的深夜，慢慢喝完，别急着回家。
- **气泡水** —— 把好消息打进气里，一口下去全是细小的烟花。今天值得多加两片柠檬。
- **你今晚的原话** —— “今天加班到很晚，有点想家”

**酒语**：把一天的疲惫削成一条卷，苦味先到，甜味后放。
**回甘**：这是你的第一杯。今晚这句话，会替你记住。
`今晚第 1 杯 · 酒柜里 0 段记忆 · seekdb 混合检索命中 4 条`

Q: 楼下的猫今天肯让我摸头了
🍸 **今晚的酒：「第七码头备忘录」**
...
**回甘**：11月9日你说过「今天加班到很晚，有点想家」。今晚它沉在杯底，慢慢地散开。
`今晚第 2 杯 · 酒柜里 1 段记忆 · seekdb 混合检索命中 5 条`

Q: 看看我的酒柜
📜 **你的酒柜**（2 段记忆）
- `11月9日` 楼下的猫今天肯让我摸头了
- `11月9日` 今天加班到很晚，有点想家
```

更完整的对话与复现步骤见 [`docs/verification.md`](docs/verification.md)。

---

## 7. API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/health` | seekdb 版本、索引、编码模式、数据量 |
| POST | `/v1/recall` | 读取记忆 + 生成调酒/酒柜/打烊草稿（`query`、`user_id`、`top_k`） |
| POST | `/v1/remember` | 封存客人原话（`user_id`、`text`） |
| GET | `/v1/memories?user_id=` | 查看某位客人的记忆 |
| GET | `/v1/orders?user_id=` | 查看酒单 |
| POST | `/v1/reset` | 清空某位客人的演示数据（不动风味卡库） |

---

## 8. 目录结构

```
dify/memory-bar/
├── app/
│   ├── config.py        # 环境变量与 .env
│   ├── db.py            # seekdb 连接、DDL、混合检索 SQL
│   ├── embedding.py     # bge-small-zh-v1.5 ONNX 编码（含离线降级）
│   ├── mixology.py      # 调酒/酒柜/打烊三种意图的文案与配方
│   ├── seed.py          # 风味卡与演示记忆灌入
│   └── main.py          # FastAPI 服务
├── dsl/
│   └── midnight_memory_bar.yml   # Dify Chatflow DSL
├── sample_data/
│   ├── flavor_cards.json         # 20 张风味卡
│   └── demo_memories.json        # demo 的 6 段历史记忆
├── scripts/
│   ├── init_db.py
│   ├── seed.py
│   └── smoke_test.py
├── docs/
│   ├── dev-log.md
│   ├── verification.md
│   └── smoke_results.json
├── requirements.txt
└── .env.example
```

---

## 9. 常见问题

**Q: Dify 里调用 HTTP 节点失败（Connection refused）？**
A: Dify 在容器里时 `127.0.0.1` 指向容器自身。Docker 自托管用 `host.docker.internal`；
Linux 可在 compose 中加 `extra_hosts: ["host.docker.internal:host-gateway"]`；
Dify Cloud 需要把 8787 用内网穿透映射到公网。

**Q: 没有 API Key，能体验吗？**
A: 能。后端三种意图与文案全部是本地逻辑 + seekdb 检索结果，DSL 中没有任何 LLM 节点，
导入后不需要配置任何模型即可对话。

**Q: 向量模型下载失败？**
A: 在 `.env` 设置 `HF_ENDPOINT=https://hf-mirror.com` 后重跑。
服务在完全离线时会自动降级为哈希向量（`/v1/health` 的 `embedding.mode` 为 `hash`），功能不受影响，只是语义精度下降。

**Q: seekdb 版本要求？**
A: 需要支持 `VECTOR` 类型与 `WITH PARSER ngram` 全文索引的版本（本作品实测 seekdb 1.4.0.0）。

**Q: 想重新演示？**
A: `curl -X POST http://127.0.0.1:8787/v1/reset -H "Content-Type: application/json" -d '{"user_id":"demo"}'`，
然后重新说一句话即可。

---

## 10. License

MIT
