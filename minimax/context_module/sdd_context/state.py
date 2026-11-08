"""PowerContext 状态层：保存并复用任务进展与验证结果。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from powercontext.builtin.persistence.sqlite.profile import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    MemoryEntryInput,
    RememberMemoryRequest,
    open_builtin_runtime,
)

STATE_KIND = "task-state"


def _config(db_path: Path) -> BuiltinConfig:
    return BuiltinConfig(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{db_path.as_posix()}")
    )


async def _write_state(db_path: Path, payload: dict[str, Any]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config = _config(db_path)
    text = json.dumps(payload, ensure_ascii=False)
    async with open_builtin_runtime(config, scheduler_path=str(db_path) + ".scheduler") as runtime:
        scope = await runtime.scopes.bootstrap_default()
        await runtime.memory.for_scope(scope.scope_id).remember(
            RememberMemoryRequest(
                entries=(MemoryEntryInput(kind=STATE_KIND, text=text),)
            )
        )


async def _read_state(db_path: Path, task_id: str) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    config = _config(db_path)
    async with open_builtin_runtime(config, scheduler_path=str(db_path) + ".scheduler") as runtime:
        scope = await runtime.scopes.default_scope()
        page = await runtime.memory.for_scope(scope.scope_id).list()
        records: list[dict[str, Any]] = []
        for item in page.entries:
            entry = item.entry
            if getattr(entry, "kind", "") != STATE_KIND:
                continue
            try:
                payload = json.loads(entry.text)
            except (TypeError, ValueError):
                continue
            if task_id and payload.get("task_id") != task_id:
                continue
            records.append(payload)
        return records


def write_state(db_path: Path, payload: dict[str, Any]) -> None:
    asyncio.run(_write_state(db_path, payload))


def read_state(db_path: Path, task_id: str = "") -> list[dict[str, Any]]:
    return asyncio.run(_read_state(db_path, task_id))


def latest_state(db_path: Path, task_id: str) -> dict[str, Any] | None:
    records = read_state(db_path, task_id)
    return records[-1] if records else None
