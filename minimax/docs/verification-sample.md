# 验证样例：T-1001 订单查询支持金额上限并修正下限边界

## 任务描述

业务工程 `business/` 是一个订单查询服务。本次要做一项局部修改：

- 新增 `max_amount` 上限筛选（`amount <= max_amount`）；
- 修正 `min_amount` 边界为闭区间（`amount >= min_amount`）；
- 不得改变状态筛选、排序、分页、非法状态报错与无筛选返回全部的行为。

任务定义见 `tasks/T-1001.json`。

## 有效规格

`specs/order-query.md` 中与本任务直接相关的条目：

| 条目 | 内容 |
| --- | --- |
| S1 筛选维度 | 支持 `status`、`min_amount`、`max_amount`，可组合 |
| S2 金额边界 | 闭区间，等于边界的订单必须保留 |
| S3 非法状态 | 非 `paid/pending/cancelled` 抛 `ValueError` |
| S4 排序与分页 | `created_at` 倒序，先 `offset` 后 `limit` |
| S5 兼容性 | 无筛选返回全部，既有行为不变 |

## 初始数据

测试内固定数据集 `business/tests/test_orders.py::orders`：

| 订单 | 状态 | 金额 | 创建时间 |
| --- | --- | --- | --- |
| A-1001 | paid | 100.0 | 2026-01-01T10:00:00 |
| A-1002 | paid | 150.0 | 2026-01-02T10:00:00 |
| A-1003 | pending | 200.0 | 2026-01-03T10:00:00 |
| A-1004 | paid | 260.0 | 2026-01-04T10:00:00 |
| A-1005 | cancelled | 80.0 | 2026-01-05T10:00:00 |

## 操作步骤

1. `cp .env.example .env`，填入 `MINIMAX_API_KEY`；启动 seekdb（见 README）。
2. 恢复初始代码：`cp fixtures/before/app/orders.py business/app/orders.py`。
3. 修改前测试：`cd business && python -m pytest -m target -q` 与 `python -m pytest -m regression -q`。
4. 初始化资料库：`cd context_module && python -m sdd_context init && python -m sdd_context import`。
5. 生成上下文：`python -m sdd_context context --task ../tasks/T-1001.json`，检查输出了 S1/S2/S5 等规格条目、`list_orders` 符号与两组测试。
6. 交给 MiniMax：`python agent/minimax_agent.py --context .run/context-T-1001.json --target business/app/orders.py`。
7. 修改后测试：重复第 3 步。
8. 写入状态并重启读取：`python -m sdd_context record ...` 后在新进程执行 `python -m sdd_context state --task-id T-1001`。

以上步骤也可以用 `python scripts/run_sample.py` 一次完成。

## 预期结果

| 检查 | 期望 |
| --- | --- |
| 修改前目标测试 | 2 failed（`max_amount` 参数不存在、`min_amount` 边界被排除） |
| 修改前回归测试 | 5 passed |
| 上下文输出 | 含规格条目、代码符号、目标/回归测试与来源路径行号 |
| MiniMax 记录 | `records/minimax-session.json` + `records/change.diff` 与提交代码一致 |
| 修改后目标测试 | 2 passed |
| 修改后回归测试 | 5 passed |
| 状态复用 | 新进程 `state` 输出 `status=verified` 与三条 verification |

## 规格更新验证

修改 `specs/order-query.md` 中 S2 的表述（例如补充「等于上限的订单同样保留」），重新执行 `python -m sdd_context import` 与 `context`，输出的 `spec_items` 会带新的内容与递增的 `version`，用于证明模块不是返回固定答案。

## 重置方法

```bash
# 恢复初始代码
cp fixtures/before/app/orders.py business/app/orders.py
# 清空状态库与资料表（示例脚本会自动执行）
rm -f .run/powercontext.db .run/powercontext.db.scheduler
# seekdb 中执行：DROP TABLE IF EXISTS sdd_context.documents;
```
