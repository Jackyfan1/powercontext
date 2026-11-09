# 验证样例与复现步骤

本文件记录「深夜记忆调酒馆」在本机的实测结果，所有输出均为真实运行结果。

## 1. 验证环境

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows 11（服务）+ WSL seekdb |
| Python | 3.14.5 |
| seekdb | `5.7.25-OceanBase seekdb-v1.4.0.0`，监听 `127.0.0.1:2881` |
| 向量模型 | `bge-small-zh-v1.5`，ONNX Runtime CPU，512 维，CLS pooling + L2 归一化 |
| 服务框架 | FastAPI + uvicorn，监听 `127.0.0.1:8787` |
| Dify | DSL 版本 0.1.5，`advanced-chat` 模式（评审在 Dify 中导入后使用） |

## 2. 复现步骤

```bash
# 1) 确认 seekdb 正在运行（示例）
./seekdb --base-dir ./seekdb_data --port 2881 --nodaemon

# 2) 安装依赖
cd dify/memory-bar
pip install -r requirements.txt

# 3) 建库建表 + 灌入种子数据
python scripts/init_db.py
python scripts/seed.py

# 4) 启动服务
python -m app.main

# 5) 冒烟测试（新终端）
python scripts/smoke_test.py
```

## 3. 数据库与索引检查

`GET /v1/health` 返回（节选）：

```json
{
  "status": "ok",
  "seekdb": {
    "version": "5.7.25-OceanBase seekdb-v1.4.0.0",
    "address": "127.0.0.1:2881",
    "database": "memory_bar",
    "indexes": [
      {"name": "PRIMARY", "type": "BTREE", "columns": ["id"]},
      {"name": "vidx_memories", "type": "VECTOR", "columns": ["embedding"]},
      {"name": "fidx_memories", "type": "FULLTEXT", "columns": ["title", "content"]},
      {"name": "idx_user_kind", "type": "BTREE", "columns": ["user_id", "kind", "created_at"]}
    ]
  },
  "embedding": {"model": "bge-small-zh-v1.5", "dim": 512, "mode": "onnx"},
  "counts": {"flavors": 20, "memories": 6, "orders": 0}
}
```

## 4. 冒烟测试：7/7 通过

`python scripts/smoke_test.py`（原始结果见 `docs/smoke_results.json`）：

| # | 用例 | 结果 | 关键数据 |
| --- | --- | --- | --- |
| 1 | 健康检查 / seekdb 索引 | PASS | VECTOR + FULLTEXT 索引均存在，编码模式 `onnx` |
| 2 | 混合检索 + 调酒草稿 | PASS | 命中「加班到十一点」，向量 0.595434 / 全文 0.850931 / 加权 0.672083 |
| 3 | 写入新记忆 `/v1/remember` | PASS | `stored=true`，`memory_id=32` |
| 4 | 语义召回刚写入的记忆 | PASS | Top-1 即该条，向量 0.796177 / 全文 0.905572 / 加权 0.828996 |
| 5 | 酒柜意图 | PASS | `intent=shelf`，返回 6 条历史记忆 |
| 6 | 打烊意图 | PASS | `intent=close` |
| 7 | 酒柜与酒单持久化 | PASS | 记忆 1 条、酒单可读出（最新「旧巷拾光」） |

第 2、4 项同时验证了「向量召回」与「全文召回」两路都真实生效：
第 4 项中查询「自己煮的面是不是盐放多了」与记忆原文没有完全相同的措辞，
仍然通过向量相似度 + 关键词命中把刚写入的记忆排在第一位。

## 5. 对话样例（同一用户连续四轮）

```text
Q: 今天加班到很晚，有点想家
🍸 **今晚的酒：「晚风归途」**
*厚底威士忌杯 · 无酒精 · 今夜特调*

**配方**
- **苦橙皮** —— 把一天的疲惫削成一条卷，苦味先到，甜味后放。适合加班后的深夜，慢慢喝完，别急着回家。
- **气泡水** —— 把好消息打进气里，一口下去全是细小的烟花。今天值得多加两片柠檬。
- **你今晚的原话** —— “今天加班到很晚，有点想家”

**酒语**：把一天的疲惫削成一条卷，苦味先到，甜味后放。适合加班后的深夜，慢慢喝完，别急着回家。

**回甘**：这是你的第一杯。今晚这句话，会替你记住。

`今晚第 1 杯 · 酒柜里 0 段记忆 · seekdb 混合检索命中 4 条`
----------------------------------------
Q: 楼下的猫今天肯让我摸头了
🍸 **今晚的酒：「第七码头备忘录」**
*马天尼杯 · 低度 · 今夜特调*

**配方**
- **晚香玉** —— 花香在夜里才浓，像一个人走后留下的香水味。浪漫从来不讲道理。
- **柚子** —— 剥开柚子时的那声轻响，是事情终于放下的声音。苦味过后，喉咙里会回甘。
- **你今晚的原话** —— “楼下的猫今天肯让我摸头了”

**酒语**：花香在夜里才浓，像一个人走后留下的香水味。浪漫从来不讲道理。

**回甘**：11月9日你说过「今天加班到很晚，有点想家」。今晚它沉在杯底，慢慢地散开。

`今晚第 2 杯 · 酒柜里 1 段记忆 · seekdb 混合检索命中 5 条`
----------------------------------------
Q: 看看我的酒柜
📜 **你的酒柜**（2 段记忆）

- `11月9日` 楼下的猫今天肯让我摸头了
- `11月9日` 今天加班到很晚，有点想家

这些都被 seekdb 收在杯底，随时可以再翻出来。

`酒柜 2 段记忆 · 累计 2 杯`
----------------------------------------
Q: 打烊吧，晚安
🌙 **打烊了。**

今晚你在这里存下 2 段记忆，喝过 2 杯。

带走今晚这杯的话：11月9日你说过「今天加班到很晚，有点想家」。

明天见。路灯会记得你走过的方向。
```

第二轮的「回甘」引用了第一轮真实写入 seekdb 的原话，
这是 `/v1/remember` → `/v1/recall` 记忆闭环的直接证据。

## 6. Dify DSL 结构自检

```text
kind app version 0.1.5 mode advanced-chat
nodes ['start', 'recall', 'bartender', 'remember', 'answer']
edges_ok True
```

Dify 端为可视化导入，需在评审环境执行（本机未安装 Docker 版 Dify）：

1. 导入 `dsl/midnight_memory_bar.yml`；
2. 按 Dify 部署方式改两个 HTTP 节点的主机名（Docker：`host.docker.internal`，本机：`127.0.0.1`，Cloud：内网穿透地址）；
3. 把 LLM 节点切换为环境中已配置的对话模型；
4. 发布，使用第 5 节的问题逐条体验。
