"""MiniMax 开发助手接入：用开发上下文生成代码修改，并落地补丁与开发记录。"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDS = PROJECT_ROOT / "records"
CODE_BLOCK = re.compile(r"```(?:python)?\s*(?P<code>.*?)```", re.S)
THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_messages(context: dict[str, Any], target_path: str, current_code: str) -> list[dict[str, str]]:
    task = context["task"]
    spec_lines = "\n\n".join(
        f"[{item['doc_id']}] {item['title']}\n{item['content']}" for item in context["spec_items"]
    )
    test_lines = "\n\n".join(
        f"[{item['doc_id']}]\n{item['content']}" for item in context["tests"]
    )
    system = (
        "你是参与真实项目的资深 Python 工程师。"
        "你只能修改用户指定的文件，必须保留既有行为。"
        "输出必须是完整的、可直接保存的文件内容，放在一个 ```python 代码块中，不要输出解释文字。"
    )
    user = f"""## 开发任务
任务编号：{task['task_id']}
标题：{task['title']}
描述：{task['description']}

## 必须实现的变更
{json.dumps(task.get('expected_change', []), ensure_ascii=False, indent=2)}

## 必须保持的既有行为
{json.dumps(task.get('must_preserve', []), ensure_ascii=False, indent=2)}

## 适用规格条目（来自 seekdb 检索）
{spec_lines}

## 需要修改的文件：{target_path}
```python
{current_code}
```

## 用于验证的测试（来自 seekdb 检索，禁止修改）
{test_lines}

请只修改 {target_path}，返回修改后的完整文件内容。"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def call_minimax(base_url: str, api_key: str, model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 8192},
        timeout=180,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"MiniMax 调用失败：HTTP {response.status_code} {response.text[:500]}")
    return response.json()


def extract_code(content: str) -> str:
    cleaned = THINK_BLOCK.sub("", content)
    match = CODE_BLOCK.search(cleaned)
    code = match.group("code") if match else cleaned
    return code.strip("\n") + "\n"


def write_records(
    records_dir: Path,
    context: dict[str, Any],
    target: Path,
    before: str,
    after: str,
    request_meta: dict[str, Any],
    raw_response: dict[str, Any],
    content: str,
    elapsed_ms: int,
) -> dict[str, str]:
    records_dir.mkdir(parents=True, exist_ok=True)
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{target.name}",
            tofile=f"b/{target.name}",
        )
    )
    diff_path = records_dir / "change.diff"
    diff_path.write_text(diff, encoding="utf-8")

    session_path = records_dir / "minimax-session.json"
    session = {
        "task_id": context["task"]["task_id"],
        "target_file": target.name,
        "model": request_meta["model"],
        "base_url": request_meta["base_url"],
        "started_at": request_meta["started_at"],
        "elapsed_ms": elapsed_ms,
        "context_sources": context["sources"],
        "request_messages": request_meta["messages"],
        "response_id": raw_response.get("id"),
        "usage": raw_response.get("usage"),
        "response_content": content,
        "diff": diff,
    }
    session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    usage = raw_response.get("usage") or {}
    record_path = records_dir / "dev-record.md"
    record_path.write_text(
        "\n".join(
            [
                "# MiniMax 开发记录",
                "",
                f"- 任务：{context['task']['task_id']} {context['task']['title']}",
                f"- 模型：{request_meta['model']}（{request_meta['base_url']}）",
                f"- 调用时间：{request_meta['started_at']}，耗时 {elapsed_ms} ms",
                f"- Token 用量：{json.dumps(usage, ensure_ascii=False)}",
                "",
                "## 提供给 MiniMax 的上下文来源",
                *[f"- `{item['path']}` L{item['lines']}（{item['doc_id']}）" for item in context["sources"]],
                "",
                "## 本次任务要求",
                *[f"- 变更：{item}" for item in context["task"].get("expected_change", [])],
                *[f"- 保持：{item}" for item in context["task"].get("must_preserve", [])],
                "",
                "## 代码修改过程",
                f"- 目标文件：`{target.name}`",
                f"- 模型返回内容长度：{len(content)} 字符",
                f"- 生成补丁：`records/change.diff`",
                "",
                "```diff",
                diff.strip() or "(无差异)",
                "```",
                "",
                f"完整请求与响应见 `{session_path.name}`（不含 API Key）。",
            ]
        ),
        encoding="utf-8",
    )
    return {"diff": str(diff_path), "session": str(session_path), "dev_record": str(record_path)}


def main() -> int:
    parser = argparse.ArgumentParser(prog="minimax-agent", description="用 MiniMax 完成一次代码修改")
    parser.add_argument("--context", required=True, help="sdd-context 生成的上下文 JSON")
    parser.add_argument("--target", required=True, help="要修改的业务代码路径")
    parser.add_argument("--records", default=str(DEFAULT_RECORDS), help="记录输出目录")
    args = parser.parse_args()

    load_env(PROJECT_ROOT / ".env")
    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("缺少 MINIMAX_API_KEY，请在 minimax/.env 中填写后再运行。")
    base_url = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
    model = os.environ.get("MINIMAX_MODEL", "MiniMax-M3")

    context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    target = Path(args.target).resolve()
    before = target.read_text(encoding="utf-8")
    messages = build_messages(context, target.relative_to(PROJECT_ROOT).as_posix(), before)

    started_at = datetime.now().isoformat(timespec="seconds")
    start = time.perf_counter()
    raw = call_minimax(base_url, api_key, model, messages)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    content = raw["choices"][0]["message"].get("content") or ""
    code = extract_code(content)
    if not code.strip():
        raise SystemExit("MiniMax 未返回可用代码。")

    target.write_text(code, encoding="utf-8")
    paths = write_records(
        Path(args.records),
        context,
        target,
        before,
        code,
        {
            "model": model,
            "base_url": base_url,
            "started_at": started_at,
            "messages": messages,
        },
        raw,
        content,
        elapsed_ms,
    )

    print(
        json.dumps(
            {
                "applied": str(target),
                "elapsed_ms": elapsed_ms,
                "usage": raw.get("usage"),
                **paths,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
