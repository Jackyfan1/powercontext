# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Captured Pi session branch events and their incremental connector."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"

_CHECKPOINT_VERSION = 1
_UNKNOWN = "unknown"


class PiSessionInput(BaseModel):
    """One complete Pi session archive and the branch leaf to capture."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch with its untouched source record."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A captured Pi session event usable as retrievable evidence."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Parse every visible event from the selected branch, root first, or fail atomically."""

    records = _read_records(value.content)
    session_id = _session_id(records[0])
    nodes = _index_nodes(records[1:])
    leaf_id = value.leaf_id if value.leaf_id is not None else next(reversed(nodes))
    if leaf_id not in nodes:
        raise ValueError(f"pi session leaf {leaf_id!r} does not exist")
    events = (_visible_event(session_id, nodes[event_id]) for event_id in _ancestor_chain(nodes, leaf_id))
    return tuple(event for event in events if event is not None)


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return the stable, delimiter-safe identity of one Pi session event."""

    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("pi session id must be non-empty")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("pi event id must be non-empty")
    direct = f"{PI_EVENT_SOURCE_NAME}:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(direct) <= MAX_SOURCE_ID_LENGTH:
        return direct
    digest = hashlib.sha256(f"{session_id}\x00{event_id}".encode("utf-8")).hexdigest()
    return f"{PI_EVENT_SOURCE_NAME}-h:{digest}"


class PiEventSourceAdapter:
    """Resolve and read Pi session event Sources."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=pi_event_source_id(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            source_id=pi_event_source_id(value.session_id, value.event_id),
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one Pi session event as canonical textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiSessionConnector:
    """Capture the visible events of one Pi session branch without duplicate work."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = self._path.read_bytes()
        events = parse_pi_session(PiSessionInput(content=content, leaf_id=self._leaf_id))
        imported = _imported_events(session.checkpoint)
        pending: list[tuple[str, PiEventInput, str]] = []
        for event in events:
            source_id = pi_event_source_id(event.session_id, event.event_id)
            digest = _event_digest(event)
            recorded = imported.get(source_id)
            if recorded is None:
                pending.append((source_id, event, digest))
            elif recorded != digest:
                raise ValueError(f"pi event {event.event_id!r} changed after it was imported")
        checkpoint = list(imported.items())
        for source_id, event, digest in pending:
            outcome = await session.submit(source_id, PI_EVENT_SOURCE_NAME, event)
            if outcome.status is ConnectorSubmissionStatus.ACCEPTED:
                checkpoint.append((source_id, digest))
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=_encode_checkpoint(checkpoint),
        )


def _read_records(content: bytes) -> list[dict[str, Any]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("pi session content must be strict UTF-8") from error
    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"pi session line {number} is not valid JSON") from error
        if not isinstance(record, dict):
            raise ValueError(f"pi session line {number} must be a JSON object")
        records.append(record)
    if not records:
        raise ValueError("pi session must start with a session header")
    return records


def _session_id(header: dict[str, Any]) -> str:
    if header.get("type") != "session":
        raise ValueError("pi session must start with a session header")
    if header.get("version") != 3:
        raise ValueError("pi session header must declare version 3")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("pi session header must declare a non-empty id")
    return session_id


def _index_nodes(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for record in records:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("pi session records must declare a non-empty id")
        if event_id in nodes:
            raise ValueError(f"pi session record {event_id!r} is duplicated")
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id:
                raise ValueError(f"pi session record {event_id!r} must declare a string parentId")
            if parent_id not in nodes:
                raise ValueError(f"pi session record {event_id!r} references an unknown parent")
        event_type = record.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError(f"pi session record {event_id!r} must declare a type")
        nodes[event_id] = record
    if not nodes:
        raise ValueError("pi session must contain at least one event record")
    return nodes


def _ancestor_chain(nodes: dict[str, dict[str, Any]], leaf_id: str) -> tuple[str, ...]:
    chain: list[str] = []
    current: str | None = leaf_id
    while current is not None:
        chain.append(current)
        parent = nodes[current].get("parentId")
        current = parent if isinstance(parent, str) else None
    chain.reverse()
    return tuple(chain)


def _visible_event(session_id: str, record: dict[str, Any]) -> PiEventInput | None:
    event_type = record["type"]
    body: str | None
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        body = _message_body(role, message)
    elif event_type in {"compaction", "branch_summary"}:
        role = event_type
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else None
    elif event_type == "custom_message":
        role = "custom"
        body = "\n".join(_text_blocks(record.get("content")))
    else:
        return None
    if body is None or not body.strip():
        return None
    parent_id = record.get("parentId")
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=parent_id if isinstance(parent_id, str) else None,
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _message_body(role: str, message: dict[str, Any]) -> str:
    if role == "toolResult":
        tool_name = _as_text(message.get("toolName")) or _UNKNOWN
        tool_call_id = _as_text(message.get("toolCallId")) or _UNKNOWN
        return f"[tool_result {tool_name} {tool_call_id}] {_message_text(message)}"
    if role == "bashExecution":
        return f"$ {_as_text(message.get('command'))}\n{_as_text(message.get('output'))}"
    return _message_text(message)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "toolCall":
            parts.append(_tool_call_text(block))
    return "\n".join(parts)


def _tool_call_text(block: dict[str, Any]) -> str:
    name = _as_text(block.get("name")) or _UNKNOWN
    call_id = _as_text(block.get("id")) or _UNKNOWN
    arguments = block.get("arguments")
    encoded = json.dumps(
        {} if arguments is None else arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"[tool_call {name} {call_id}] {encoded}"


def _text_blocks(content: JsonValue | None) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _event_digest(event: PiEventInput) -> str:
    encoded = json.dumps(event.record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _imported_events(checkpoint: JsonValue | None) -> dict[str, str]:
    if not isinstance(checkpoint, dict):
        return {}
    entries = checkpoint.get("events")
    if not isinstance(entries, list):
        return {}
    imported: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source_id = entry.get("source_id")
        digest = entry.get("digest")
        if isinstance(source_id, str) and isinstance(digest, str):
            imported[source_id] = digest
    return imported


def _encode_checkpoint(entries: list[tuple[str, str]], /) -> dict[str, JsonValue]:
    return {
        "version": _CHECKPOINT_VERSION,
        "events": [{"source_id": source_id, "digest": digest} for source_id, digest in entries],
    }


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)

__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
