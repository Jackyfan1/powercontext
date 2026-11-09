# 开发日志 · 深夜记忆调酒馆

## 1. 选题

赛题要求「用 Dify 做一个好玩的智能应用，并结合 seekdb 或 PowerContext」。
我们希望作品既能体现 Dify 的编排价值，又能让 seekdb 承担不可替代的角色，
而不是把数据库当成一个被动的存储桶。

最终选题：**一家只在深夜营业的调酒馆**。
客人说一句话，调酒师把它调成一杯有名字、有配方、有回甘的酒；
这句话同时被写进 seekdb，下一次再聊时，旧记忆会从杯底浮上来。

「回甘」是作品的核心趣味点：只有真正记住了上一轮，第二杯才会不一样。

## 2. 技术选型

| 选择 | 原因 |
| --- | --- |
| Dify Chatflow（advanced-chat） | 对话型应用，5 个节点即可串起「检索 → 生成 → 回写」；HTTP 节点负责连接本地能力 |
| seekdb | 一张表同时支持 `VECTOR` + `FULLTEXT`（ngram），混合召回可以在一条 SQL 内完成，不必引入外部检索引擎 |
| bge-small-zh-v1.5（ONNX） | 中文语义效果好、24 MB 量化权重、CPU 即可推理，评审无需 GPU |
| FastAPI | 依赖少，接口清晰，便于评审用 curl 直接验证 |

未选 PowerContext：赛题允许二选一。seekdb 的向量索引与全文索引可在同一张表、
同一条 SQL 内协同，更贴合「数据库比赛」的技术展示目标。

## 3. 实现要点

### 3.1 数据模型

- `memories`：客人记忆（`kind=drop`）与风味卡（`kind=flavor`）共用一张表；
  向量列 `VECTOR(512)` + HNSW 向量索引 + `(title, content)` ngram 全文索引；
  以 `user_id + kind + created_at` 建普通索引承接过滤。
- `orders`：每杯酒的酒单（酒名、心情、配方 JSON、原话、时间），支撑「累计几杯」和评审复盘。

### 3.2 检索

```sql
ORDER BY 0.7 * (1 - cosine_distance(embedding, :vec))
       + 0.3 * (MATCH(title, content) AGAINST (:q) / (1 + MATCH(...)))
```

- 两路分数都下推到 seekdb，Python 只接收 Top-K；
- 全文分数做了 `x / (1 + x)` 归一化，避免长文本 BM25 量纲压过余弦相似度；
- 每条检索结果回传 `vec_score`、`text_score`、`score`，评审可直接核对两路都在工作。

### 3.3 调酒引擎

- 意图路由：`brew`（默认）/ `shelf`（酒柜）/ `close`（打烊），用短语匹配，避免误触发；
- 酒名：从「时间意象 + 名词」两个池子里按 `md5(user_id + query + 日期)` 取词，
  同样输入当天稳定、跨天变化，既有个性又可复现；
- 配方：Top-2 风味卡 + 客人原话作为「记忆基底」；
- 回甘：引用检索到的最近一条旧记忆；没有旧记忆时明确说「这是你的第一杯」，不编造；
- 杯型：优先按原话中的情绪关键词（加班/想家/心动/遗憾…）选择，其次看风味卡心情。

### 3.4 Dify 编排

```
开始 → 查阅酒柜（HTTP /v1/recall）→ 封存记忆（HTTP /v1/remember）→ 回复
```

- 检索与写入都通过 HTTP 节点完成，Dify 负责会话、变量传递与展示；
- 调酒文案由后端依据 seekdb 检索结果生成，Dify 侧零模型依赖，导入即可运行；
- 两个 HTTP 节点均为 `error_strategy: default-value`，后端未启动也不阻断对话（回复会提示启动方式）。

### 3.5 稳健性

- ONNX 模型缺失时自动从 Hugging Face 下载；下载失败则退化为哈希向量，服务不中断，
  `/v1/health` 会如实显示 `embedding.mode`；
- `scripts/init_db.py` 幂等建库建表，`scripts/seed.py` 按标题去重，重复执行不会灌重复数据；
- 服务启动时自动补种风味卡，评审只需 `pip install` + 启动两步。

## 4. 验证记录

- 冒烟测试 7/7 通过（详见 `docs/verification.md` 与 `docs/smoke_results.json`）；
- 混合检索得分示例：向量 0.595 / 全文 0.851 / 加权 0.672；
- 写入记忆后再次检索，Top-1 即刚写入记录（向量 0.796 / 全文 0.906）；
- 连续两轮对话中，第二杯的「回甘」正确引用了第一轮原话。

## 5. 遇到的问题

| 问题 | 处理 |
| --- | --- |
| 中文全文检索需要合适的分词 | 使用 `WITH PARSER ngram`，无需额外词典，短语与单字都能命中 |
| 向量维度与索引不一致会建表失败 | 维度统一由 `VECTOR_DIM`（默认 512）驱动，编码器与 DDL 共用同一配置 |
| Dify 容器访问不到宿主机服务 | README 按 Docker / 本机 / Cloud 三种部署给出主机名与内网穿透方案 |
| 评审机器可能无法下载模型 | 提供 `HF_ENDPOINT` 镜像配置，并实现哈希向量降级，保证可运行性 |
| 首版 DSL（0.1.5）被判「阻断主要流程」 | Dify 1.18 的 `body.type=json` 只接受一个条目，旧版 timeout/retry 字段也已过期；改为 0.7.0 结构、单条目 JSON、`timeout.connect/read/write` 与 `error_strategy: default-value` |
| 二次提测仍报同一错误 | 回复节点引用了 HTTP 节点未声明的自定义变量（`recall.reply`），会被判为变量不存在；同时 DSL 不在约定路径。最终改为 `dify/app.dsl.yml`，回复模板只引用标准输出 `{{#recall.body#}}`（配套新增 `/v1/recall_text` 纯文本接口），HTTP 节点 `variables` 保持为空 |

## 6. 后续可做

- 接入 TTS，把「酒语」读出来；
- 增加「同一杯酒」分享卡片；
- 用 Dify 的外部知识库 API 把 seekdb 检索暴露成标准知识库，供其他应用复用；
- 引入 PowerContext 管理跨会话的客人画像，与 seekdb 形成双记忆层。
