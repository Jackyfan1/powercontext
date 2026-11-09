# 开发记录

## 赛题理解

题目：**端侧 AI 推理性能优化与 seekdb 混合检索**（20 分，困难）。要求在浏览器完成文档向量化，结合 seekdb 的存储与检索能力，构建轻量个人知识库，并完成「导入语料 → 本地向量化 → 写入 seekdb → 混合检索」全流程。

参考文档给出的 7 条检查项是本实现的验收基线，逐条落到功能点（见 README 对照表）。

## 技术选型

| 位置 | 选择 | 原因 |
| --- | --- | --- |
| 浏览器推理 | `transformers.js` 3.8 + ONNX Runtime Web | 官方支持 WebGPU/WASM 双后端、`dtype` 切换与本地模型路径，无需云端 |
| embedding 模型 | `bge-small-zh-v1.5`（512 维） | 中文效果稳定、体积小（FP32 95 MB），且官方提供 FP16/INT8 转换版 |
| 后端 | FastAPI + pymysql | 浏览器不能讲 MySQL 协议，用最小本机进程代理 seekdb |
| 数据库 | seekdb 1.4.0.0（WSL 单机） | 同时具备 `VECTOR` + HNSW 与 `FULLTEXT`(ngram)，混合检索可下推到 SQL |
| 前端 | 原生 ES Module + Web Worker | 无构建链，便于评审直接启动 |

## 实施步骤

1. **能力验证**（`_scratch/probe*.py`，未入库）
   - 确认 seekdb 支持 `VECTOR(512)` + `VECTOR INDEX ... WITH (distance=cosine, type=hnsw)`；
   - 确认 `FULLTEXT INDEX (title, content) WITH PARSER ngram` 与中文 `MATCH ... AGAINST` 可用；
   - 验证「向量分 + 全文分」加权排序可在一条 SQL 内完成（派生表 + `ORDER BY`）。
2. **数据层**：`collections / documents / chunks / sessions` 四张表，`chunks` 同时挂 HNSW 与 ngram 全文索引。
3. **后端**：集合、文档、检索、会话、索引刷新、量化报告落盘共 12 个接口。
4. **前端**：
   - `embed-worker.js` 在 Worker 内加载模型，`detectWebGPU()` 决定初始后端；
   - 导入按 4 块一批编码，进度条 + 日志；
   - 导入后用标题做全文检索，验证新内容立即可检索；
   - 检索支持混合/向量/全文三模式与权重滑块；
   - 「量化对比」按 FP32 → FP16 → INT8 依次加载，统计体积、耗时、余弦一致性、Top-1 一致率，并回传后端写入 `docs/quantization-results.json`。
5. **验证**：`scripts/smoke_test.py`（服务端链路）+ 浏览器实测（导入/检索/刷新/会话恢复）。

## 排错记录

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `seekdb` 启动后进程消失、2881 未监听 | 用 `wsl -- bash -lc "nohup ... &"` 启动时，wsl.exe 退出后发行版被回收 | 改为保持一个长驻 WSL 会话运行 seekdb（PowerShell 后台任务），确认 `ss -ltn` 有 2881 |
| `pymysql.err.OperationalError: (1049, "Unknown database 'stepseek'")` | 首次启动时数据库尚未创建，连接串直接指定了库名 | `get_conn()` 捕获 1049 后回退到实例级连接 |
| `(1046, 'No database selected')` | 建库用独立连接，随后 DDL 复用了未选库的连接 | `init_database()` 在同一连接内 `CREATE DATABASE` → `USE` → 建表 |
| 中文全文检索无结果 | 默认分词器不切中文 | 全文索引显式 `WITH PARSER ngram` |

## 服务端冒烟结果

`python scripts/smoke_test.py`（seekdb 1.4.0.0）：

```
[1] 健康检查            PASS  seekdb 可用 / 存在 VECTOR 与 FULLTEXT 索引
[2] 创建测试集合        PASS
[3] 增量写入文档与向量  PASS x3
[4] 重复导入应被识别    PASS
[5] 三种检索模式        PASS x5（hybrid 首位命中「seekdb 检索」）
[6] 刷新索引后新内容可检索 PASS（ANALYZE 67.69 ms）
[7] 会话状态持久化      PASS
[8] 清理                PASS
```

## 浏览器端实测结果

在 Chromium 内核浏览器（支持 WebGPU）访问 `http://127.0.0.1:8011/`，四组用例均通过：

| 用例 | 操作 | 结果 |
| --- | --- | --- |
| 单篇导入 + 检索 | 粘贴一篇文本 → 导入 → 混合检索 | 写入 seekdb 成功（1 块 / 37 字 / 27.4 ms）；首位 score 0.7100（向量 0.7657 / 全文 0.5800，SQL 5.45 ms） |
| 示例语料批量导入 | 点「载入示例语料」 | 导入 4 篇示例（累计 5 篇 / 10 个分块）；查询「浏览器里怎么做本地向量化」命中 5 条，最高 0.4877 |
| 量化实测 | 点「量化对比」 | 依次加载 FP32/FP16/INT8 并回传，结果落盘 `docs/quantization-results.json`（详见 `docs/quantization.md`） |
| WASM 强制降级 | 访问 `?backend=wasm` | 顶部芯片显示 `WASM · INT8`（无 WebGPU）；日志依次输出「URL 指定 backend=wasm」「WebGPU 不可用，使用 INT8(q8) 权重」；模型加载 442 ms；检索「浏览器里怎么做本地向量化」命中 5 条，嵌入 42 ms / SQL 11.76 ms，首位 0.4896 |

降级用例证明：WebGPU 不可用时应用会自动切到 WASM + INT8，并在界面与日志中明确提示，导入与检索主链路不受影响。

## 待办 / 后续

- 混合检索的派生表写法在十万级分块时可能退化为全表扫描，可改为「向量 TopN ∪ 全文 TopN」两路候选后再加权。
- 可加入 rerank 小模型进一步提升精度（会增加端侧体积，需权衡）。
