"""SDD 上下文模块命令行入口（CLI）。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .importer import iter_all_documents
from .selector import build_context, render_markdown
from .state import latest_state, write_state
from .store import SeekDBStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_DB = PROJECT_ROOT / ".run" / "powercontext.db"


def _store() -> SeekDBStore:
    return SeekDBStore(
        host=os.environ.get("SEEKDB_HOST", "127.0.0.1"),
        port=int(os.environ.get("SEEKDB_PORT", "2881")),
        user=os.environ.get("SEEKDB_USER", "root"),
        password=os.environ.get("SEEKDB_PASSWORD", ""),
        database=os.environ.get("SEEKDB_DATABASE", "sdd_context"),
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_init(args: argparse.Namespace) -> int:
    store = _store()
    store.ensure_schema()
    print(json.dumps(store.describe(), ensure_ascii=False, indent=2))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    store = _store()
    store.ensure_schema()
    documents = list(iter_all_documents(PROJECT_ROOT))
    if not documents:
        print("未找到可导入的工程资料", file=sys.stderr)
        return 2
    inserted, updated = store.upsert(documents)
    print(
        json.dumps(
            {
                "imported": len(documents),
                "inserted": inserted,
                "updated": updated,
                "store": store.describe(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    store = _store()
    store.ensure_schema()
    task = _load_json(Path(args.task))
    state_db = Path(args.state_db) if args.state_db else DEFAULT_STATE_DB
    state = latest_state(state_db, task["task_id"]) if state_db.exists() else None
    keywords = [item.strip() for item in args.keywords.split(",")] if args.keywords else None
    context = build_context(store, task, state, keywords)

    out_path = Path(args.out) if args.out else PROJECT_ROOT / ".run" / f"context-{task['task_id']}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = out_path.with_suffix(".md")
    markdown_path.write_text(render_markdown(context), encoding="utf-8")

    print(render_markdown(context))
    print(f"\n上下文已写入：{out_path}")
    print(f"可读版本：{markdown_path}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    task = _load_json(Path(args.task))
    payload = _load_json(Path(args.payload))
    payload.setdefault("task_id", task["task_id"])
    state_db = Path(args.state_db) if args.state_db else DEFAULT_STATE_DB
    write_state(state_db, payload)
    print(json.dumps({"saved": payload, "state_db": str(state_db)}, ensure_ascii=False, indent=2))
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    state_db = Path(args.state_db) if args.state_db else DEFAULT_STATE_DB
    current = latest_state(state_db, args.task_id)
    print(json.dumps({"state_db": str(state_db), "state": current}, ensure_ascii=False, indent=2))
    return 0 if current else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdd-context", description="SDD 工程上下文模块")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="初始化 seekdb 数据库与表")
    init_parser.set_defaults(func=cmd_init)

    import_parser = sub.add_parser("import", help="导入规格 / 代码 / 测试资料")
    import_parser.set_defaults(func=cmd_import)

    context_parser = sub.add_parser("context", help="生成任务开发上下文")
    context_parser.add_argument("--task", required=True, help="任务 JSON 文件")
    context_parser.add_argument("--out", default="", help="上下文输出路径")
    context_parser.add_argument("--state-db", default="", help="PowerContext 状态库路径")
    context_parser.add_argument("--keywords", default="", help="覆盖检索关键词，逗号分隔")
    context_parser.set_defaults(func=cmd_context)

    record_parser = sub.add_parser("record", help="把任务进展写入 PowerContext")
    record_parser.add_argument("--task", required=True, help="任务 JSON 文件")
    record_parser.add_argument("--payload", required=True, help="状态 JSON 文件")
    record_parser.add_argument("--state-db", default="", help="PowerContext 状态库路径")
    record_parser.set_defaults(func=cmd_record)

    state_parser = sub.add_parser("state", help="读取 PowerContext 中保存的任务状态")
    state_parser.add_argument("--task-id", required=True)
    state_parser.add_argument("--state-db", default="", help="PowerContext 状态库路径")
    state_parser.set_defaults(func=cmd_state)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
