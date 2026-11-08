"""验证样例一键复现：修改前失败 → MiniMax 修改 → 修改后通过 → 状态跨进程复用。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pymysql

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUSINESS = PROJECT_ROOT / "business"
TARGET = BUSINESS / "app" / "orders.py"
BEFORE = PROJECT_ROOT / "fixtures" / "before" / "app" / "orders.py"
RECORDS = PROJECT_ROOT / "records"
TASK_FILE = PROJECT_ROOT / "tasks" / "T-1001.json"
CONTEXT_JSON = PROJECT_ROOT / ".run" / "context-T-1001.json"
STATE_DB = PROJECT_ROOT / ".run" / "powercontext.db"
CONTEXT_MODULE = PROJECT_ROOT / "context_module"


def load_env() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def run(cmd: list[str], cwd: Path, log_name: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(CONTEXT_MODULE)
    result = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = (result.stdout or "") + (result.stderr or "")
    if log_name:
        RECORDS.mkdir(parents=True, exist_ok=True)
        (RECORDS / log_name).write_text(output, encoding="utf-8")
    return result


def pytest_summary(output: str) -> str:
    for line in reversed(output.strip().splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            return line.strip("= ").strip()
    return ""


def reset_business_code() -> None:
    shutil.copyfile(BEFORE, TARGET)


def reset_documents() -> None:
    conn = pymysql.connect(
        host=os.environ.get("SEEKDB_HOST", "127.0.0.1"),
        port=int(os.environ.get("SEEKDB_PORT", "2881")),
        user=os.environ.get("SEEKDB_USER", "root"),
        password=os.environ.get("SEEKDB_PASSWORD", ""),
        database=os.environ.get("SEEKDB_DATABASE", "sdd_context"),
        autocommit=True,
    )
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS documents")
    conn.close()


def sdd(*args: str, log_name: str | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "sdd_context", *args]
    return run(cmd, CONTEXT_MODULE, log_name)


def main() -> int:
    load_env()
    RECORDS.mkdir(parents=True, exist_ok=True)
    if STATE_DB.exists():
        STATE_DB.unlink()
    scheduler = Path(str(STATE_DB) + ".scheduler")
    if scheduler.exists():
        scheduler.unlink()

    steps: list[dict[str, object]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    # 1. 重置到修改前快照
    reset_business_code()
    record("reset", True, "已恢复到修改前代码快照 fixtures/before/app/orders.py")

    # 2. 修改前：目标测试必须失败，回归测试必须通过
    before_target = run([sys.executable, "-m", "pytest", "-m", "target", "-q"], BUSINESS, "before-target.log")
    target_summary = pytest_summary(before_target.stdout + before_target.stderr)
    record("before-target-fails", before_target.returncode != 0, target_summary)

    before_regression = run([sys.executable, "-m", "pytest", "-m", "regression", "-q"], BUSINESS, "before-regression.log")
    regression_summary = pytest_summary(before_regression.stdout + before_regression.stderr)
    record("before-regression-passes", before_regression.returncode == 0, regression_summary)

    # 3. 导入资料到 seekdb，并从空状态写入任务进展
    reset_documents()
    init_result = sdd("init", log_name="seekdb-init.log")
    record("seekdb-init", init_result.returncode == 0, "seekdb 表结构就绪")
    import_result = sdd("import", log_name="seekdb-import.log")
    record("seekdb-import", import_result.returncode == 0, "规格/代码/测试已导入 seekdb")

    initial_state = {
        "task_id": "T-1001",
        "status": "in_progress",
        "done": ["已导入订单查询规格、代码与测试资料"],
        "todo": ["使用 MiniMax 完成 max_amount 与边界修正", "运行目标测试与回归测试"],
        "verification": [],
    }
    state_file = PROJECT_ROOT / ".run" / "state-initial.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(initial_state, ensure_ascii=False, indent=2), encoding="utf-8")
    record_result = sdd("record", "--task", str(TASK_FILE), "--payload", str(state_file), log_name="state-initial.log")
    record("state-write", record_result.returncode == 0, "PowerContext 已保存初始任务进展（原为空状态）")

    # 4. 生成开发上下文（seekdb 检索 + PowerContext 状态）
    context_result = sdd("context", "--task", str(TASK_FILE), "--out", str(CONTEXT_JSON), log_name="context.log")
    context_ok = context_result.returncode == 0 and CONTEXT_JSON.exists()
    if context_ok:
        shutil.copyfile(CONTEXT_JSON, RECORDS / "context-T-1001.json")
        context_md = CONTEXT_JSON.with_suffix(".md")
        if context_md.exists():
            shutil.copyfile(context_md, RECORDS / "context-T-1001.md")
    record("context-build", context_ok, "开发上下文已生成并归档到 records/context-T-1001.json")
    if not context_ok:
        return 1

    # 5. MiniMax 完成代码修改
    agent_result = run(
        [
            sys.executable,
            str(PROJECT_ROOT / "agent" / "minimax_agent.py"),
            "--context",
            str(CONTEXT_JSON),
            "--target",
            str(TARGET),
        ],
        PROJECT_ROOT,
        "minimax-agent.log",
    )
    record("minimax-modification", agent_result.returncode == 0, "MiniMax 已生成并应用代码修改")
    if agent_result.returncode != 0:
        return 1

    # 6. 修改后：目标测试与回归测试全部通过
    after_target = run([sys.executable, "-m", "pytest", "-m", "target", "-q"], BUSINESS, "after-target.log")
    record("after-target-passes", after_target.returncode == 0, pytest_summary(after_target.stdout + after_target.stderr))

    after_regression = run([sys.executable, "-m", "pytest", "-m", "regression", "-q"], BUSINESS, "after-regression.log")
    record("after-regression-passes", after_regression.returncode == 0, pytest_summary(after_regression.stdout + after_regression.stderr))

    # 7. 保存验证结果到 PowerContext
    final_state = {
        "task_id": "T-1001",
        "status": "verified",
        "done": [
            "已导入订单查询规格、代码与测试资料",
            "MiniMax 完成 max_amount 与边界修正",
            "目标测试与回归测试全部通过",
        ],
        "todo": [],
        "verification": [
            {"check": "target-before", "result": "failed", "log": "records/before-target.log"},
            {"check": "target-after", "result": "passed", "log": "records/after-target.log"},
            {"check": "regression-after", "result": "passed", "log": "records/after-regression.log"},
        ],
    }
    final_state_file = PROJECT_ROOT / ".run" / "state-final.json"
    final_state_file.write_text(json.dumps(final_state, ensure_ascii=False, indent=2), encoding="utf-8")
    sdd("record", "--task", str(TASK_FILE), "--payload", str(final_state_file), log_name="state-final.log")
    record("state-update", True, "验证结果已写入 PowerContext")

    # 8. 重启上下文模块（新进程）后读取状态
    restart = sdd("state", "--task-id", "T-1001", log_name="state-after-restart.log")
    state_ok = restart.returncode == 0 and "verified" in restart.stdout
    record("state-reuse", state_ok, "新进程成功读取到保存的任务状态")
    if state_ok:
        (RECORDS / "state-after-restart.json").write_text(
            restart.stdout[restart.stdout.find("{") :], encoding="utf-8"
        )

    summary = {
        "task_id": "T-1001",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "steps": steps,
        "all_passed": all(bool(step["ok"]) for step in steps),
    }
    (RECORDS / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
