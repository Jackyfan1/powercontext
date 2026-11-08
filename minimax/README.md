# 规有所依 · SDD 工程上下文管理系统（MiniMax 赛题）

为订单查询业务工程实现一个工程上下文模块：把**规格、代码、测试**导入 seekdb，按任务检索出当前有效的规格条目、待修改的代码符号和验证测试，叠加 PowerContext 中保存的任务状态，形成开发上下文交给 **MiniMax** 完成一次真实的代码修改，最后用同一组测试证明「修改前失败、修改后通过、回归不退化」。

## 目录结构

```
minimax/
├── specs/order-query.md              # 需求规格（S1-S5，可修改后重新导入）
├── tasks/T-1001.json                 # 验证样例的任务描述
├── business/                         # 业务工程（订单查询服务 + 测试）
│   ├── app/orders.py                 # 目标文件（当前为 MiniMax 修改后的版本）
│   ├── tests/test_orders.py          # target / regression 两组测试
│   └── pytest.ini
├── fixtures/before/app/orders.py     # 修改前代码快照
├── context_module/sdd_context/       # 上下文模块（CLI）
│   ├── store.py                      # seekdb 存取与检索
│   ├── importer.py                   # 规格/代码符号/测试解析
│   ├── selector.py                   # 上下文装配
│   ├── state.py                      # PowerContext 状态读写
│   └── cli.py                        # 命令入口
├── agent/minimax_agent.py            # MiniMax 接入（OpenAI 兼容 HTTP API）
├── scripts/run_sample.py             # 验证样例一键复现
├── records/                          # 运行产物：日志、diff、开发记录
└── docs/verification-sample.md       # 验证样例说明
```

## 三项技术的实际作用

| 技术 | 在本作品中的作用 | 接入方式 |
| --- | --- | --- |
| MiniMax | 读取开发上下文，生成 `business/app/orders.py` 的完整修改；请求/响应、补丁与开发记录全部落盘 | HTTP API（OpenAI 兼容 `/chat/completions`，Bearer Token），默认 `MiniMax-M3` |
| seekdb | 存放规格条目、代码符号、测试用例三类工程资料，按任务关键词做真实 SQL 检索，检索结果直接进入上下文 | `pymysql` 直连 seekdb（MySQL 协议，默认 `127.0.0.1:2881`），库名 `sdd_context` |
| PowerContext | 保存任务进展与验证结果；模块重启后从同一状态库读取并继续使用 | `powercontext[builtin]==1.2.0` 进程内 runtime + SQLite 状态库 `.run/powercontext.db` |

## 依赖版本

- Python 3.12+（本机在 3.14 实测）
- `powercontext[builtin]==1.2.0`、`pymysql>=1.1`、`requests>=2.31`、`pytest>=8.0`

```bash
python -m venv .venv
.venv/Scripts/pip install -r minimax/requirements.txt    # Windows
# source .venv/bin/activate && pip install -r minimax/requirements.txt  # Linux/macOS
```

## 配置

```bash
cp minimax/.env.example minimax/.env
# 填写 MINIMAX_API_KEY（MiniMax Token Plan），其余保持默认即可
```

| 变量 | 说明 |
| --- | --- |
| `SEEKDB_HOST/PORT/USER/PASSWORD/DATABASE` | seekdb 连接信息，默认 `127.0.0.1:2881`，root/空密码，库 `sdd_context` |
| `MINIMAX_BASE_URL` | 默认 `https://api.minimax.chat/v1` |
| `MINIMAX_MODEL` | 默认 `MiniMax-M3` |
| `MINIMAX_API_KEY` | MiniMax Token Plan 的 API Key，仅本地 `.env` 保存，不提交仓库 |

## 启动 seekdb

任选其一，保证 `127.0.0.1:2881` 可连接、root 可登录即可（本作品不依赖业务工程原有数据库）：

```bash
# 方式一：已有 seekdb 安装
seekdb --base-dir /path/to/seekdb_data --port 2881

# 方式二：本机 WSL（oceanbase-desktop 发行版，实测环境）
wsl -d oceanbase-desktop -u root -- /usr/bin/seekdb --base-dir /root/seekdb140 --port 2881 --nodaemon
```

## 数据初始化与调用入口

在 `minimax/context_module` 目录执行（需 `PYTHONPATH=.`）：

```bash
python -m sdd_context init                                     # 建库建表
python -m sdd_context import                                   # 导入规格/代码/测试
python -m sdd_context context --task ../tasks/T-1001.json      # 生成开发上下文（自动读取 PowerContext 状态）
python -m sdd_context record  --task ../tasks/T-1001.json --payload ../.run/state-final.json
python -m sdd_context state   --task-id T-1001                 # 新进程读取已保存的任务状态
```

上下文输出：`.run/context-T-1001.json`（机器可读）与 `.run/context-T-1001.md`（人可读），其中包含：

- 本次任务适用的规格条目（含文件路径与行号、版本号）
- 需要修改的代码文件与符号
- 用于验证功能与兼容性的测试
- 已完成事项、待办事项、已有验证结果（来自 PowerContext）

规格更新后重新执行 `import`，检索结果会使用更新后的内容（版本号随之递增），不会返回固定答案。

## 测试命令

```bash
cd minimax/business
python -m pytest -m target -q       # 目标功能测试
python -m pytest -m regression -q   # 回归测试
python -m pytest -q                 # 全部
```

## 一键验证样例

```bash
cd minimax
python scripts/run_sample.py
```

流程：恢复修改前快照 → 目标测试失败 / 回归测试通过 → seekdb 导入 → 生成上下文 → MiniMax 修改代码 → 目标测试与回归测试全部通过 → 写入 PowerContext → 新进程读取状态。产物见 `records/`（`context-T-1001.json/.md`、`before-target.log`、`after-target.log`、`change.diff`、`minimax-session.json`、`dev-record.md`、`summary.json`）。

## 已知限制

- PowerContext 的 `OceanBase` profile 会校验 `ob_compatibility_mode`，seekdb 1.4 未暴露该变量，因此 PowerContext 状态库使用 SQLite（`powercontext[builtin]`），seekdb 专门承担工程资料存储与检索。
- 检索采用关键词 `LIKE` + 命中计分排序，未启用向量检索；资料规模为教程级（约 20 条文档）。
- MiniMax 输出按「完整文件 + 代码块」约定解析；若模型返回解释性文字，会被忽略。生成结果仍需人工复核后才能进入主分支。
- `run_sample.py` 会重置 `.run/powercontext.db` 与 seekdb 中 `documents` 表，请勿在共享环境中执行。
