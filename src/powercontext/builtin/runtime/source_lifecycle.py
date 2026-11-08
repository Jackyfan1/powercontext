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

"""Propagate Source lifecycle events to dependent Memory and Topic Memory nodes."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, field_validator
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import PowerContextError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime


SOURCE = "source"
MEMORY = "memory"
TOPIC = "topic"
TOPIC_MEMORY_FAMILY = "topic-memory"
_POLICIES = frozenset({"all", "any"})
_PURPOSES = frozenset({"generation", "audit"})
_REASON = "source-lifecycle"
_MEMORY_STATE_ACTIVE = "active"
_MEMORY_STATE_INACTIVE = "inactive"

_NODES_TABLE = Table(
    "pc_source_lifecycle_nodes",
    MetaData(),
    Column("scope_id", String(128), primary_key=True),
    Column("node_id", String(128), primary_key=True),
    Column("family", String(16), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("parents", Text, nullable=False),
    Column("policy", String(8), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("reference", Text, nullable=False),
    Column("available", Integer, nullable=False),
    Column("last_sequence", Integer, nullable=False),
)

_SCHEMA = """CREATE TABLE IF NOT EXISTS pc_source_lifecycle_nodes (
    scope_id VARCHAR(128) NOT NULL,
    node_id VARCHAR(128) NOT NULL,
    family VARCHAR(16) NOT NULL,
    ordinal INTEGER NOT NULL,
    parents TEXT NOT NULL,
    policy VARCHAR(8) NOT NULL,
    revision INTEGER NOT NULL,
    reference TEXT NOT NULL,
    available INTEGER NOT NULL,
    last_sequence INTEGER NOT NULL,
    PRIMARY KEY (scope_id, node_id)
)"""


class SourceLifecycleError(ValueError):
    """Report a lifecycle graph, event, or reference that cannot be honored."""


class SourceLifecycleEvent(BaseModel):
    """One validated availability event for a registered Source node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: StrictInt
    sequence: StrictInt
    available: StrictBool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")  # noqa: TRY003
        return value

    @field_validator("revision", "sequence")
    @classmethod
    def require_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("value must be positive")  # noqa: TRY003
        return value


@dataclass(frozen=True)
class _Node:
    node_id: str
    family: str
    ordinal: int
    parents: tuple[str, ...]
    policy: str
    revision: int
    reference: str
    available: bool
    last_sequence: int


class SourceLifecycleApplication:
    """Select the per-scope Source lifecycle service and cache Topic visibility."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase | None) -> None:
        self._runtime = runtime
        self._database = database
        self._disabled_topics: dict[str, frozenset[str]] = {}

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleService:
        return ScopedSourceLifecycleService(self, scope_id)

    async def disabled_topic_ids(self, scope_id: str, /) -> frozenset[str]:
        """Return the Topic Artifact identities that must not enter recall."""

        scope = validate_scope_id(scope_id)
        cached = self._disabled_topics.get(scope)
        if cached is not None:
            return cached
        if self._database is None:
            return frozenset()
        async with self._database.transaction() as connection:
            await _ensure_schema(connection)
            nodes = await _load_nodes(connection, scope)
        disabled = frozenset(
            _artifact_id(node) for node in nodes if node.family == TOPIC and not node.available
        )
        self._disabled_topics[scope] = disabled
        return disabled

    def invalidate(self, scope_id: str) -> None:
        self._disabled_topics.pop(scope_id, None)


class ScopedSourceLifecycleService:
    """Register evidence dependencies and propagate Source availability in one scope."""

    def __init__(self, application: SourceLifecycleApplication, scope_id: str) -> None:
        self._application = application
        self._runtime = application._runtime
        self._database = application._database
        self.scope_id = validate_scope_id(scope_id)

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        _validate_node_id(node_id)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source reference must be a SourceRef")
        reference = _dump_source(source_ref)
        async with self._runtime._context(self.scope_id):
            async with self._runtime._locked(self.scope_id):
                nodes = await self._nodes()
                existing = _find_node(nodes, node_id)
                if existing is not None:
                    if (
                        existing.family == SOURCE
                        and existing.reference == reference
                        and existing.revision == revision
                    ):
                        return
                    raise SourceLifecycleError("node is already registered with another binding")
                await self._require_readable_source(source_ref)
                await self._insert(
                    _Node(
                        node_id=node_id,
                        family=SOURCE,
                        ordinal=len(nodes),
                        parents=(),
                        policy="all",
                        revision=revision,
                        reference=reference,
                        available=True,
                        last_sequence=0,
                    )
                )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        _validate_node_id(node_id)
        _validate_policy(policy)
        ordered = _validate_parents(node_id, parents)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory reference must be a MemoryCitation")
        reference = _dump_citation(citation)
        async with self._runtime._context(self.scope_id) as context:
            async with self._runtime._locked(self.scope_id):
                if citation.memory_ref.artifact_id != context.artifacts.memory_artifact_id:
                    raise SourceLifecycleError("citation belongs to another Memory")
                nodes = await self._nodes()
                existing = _find_node(nodes, node_id)
                if existing is not None:
                    if _matches(existing, MEMORY, reference, ordered, policy):
                        return
                    raise SourceLifecycleError("node is already registered with another binding")
                _require_registered_parents(nodes, ordered)
                available = _derived_availability(nodes, ordered, policy)
                service = context.artifacts.memory
                head = await service.head(context.artifacts.memory_artifact_id)
                try:
                    entry = await service.validate_citation(citation)
                except PowerContextError as error:
                    raise SourceLifecycleError("Memory citation is not readable") from error
                manifest = {item.entry_id: item for item in head.content.manifest.entries}
                item = manifest.get(entry.entry_id)
                if item is None or item.entry_version_id != entry.entry_version_id:
                    raise SourceLifecycleError("Memory citation is not readable")
                if not available:
                    await service.forget(head, entries=(entry,), reason=_REASON)
                await self._insert(
                    _Node(
                        node_id=node_id,
                        family=MEMORY,
                        ordinal=len(nodes),
                        parents=ordered,
                        policy=policy,
                        revision=citation.memory_ref.revision,
                        reference=reference,
                        available=available,
                        last_sequence=0,
                    )
                )
                self._application.invalidate(self.scope_id)

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        _validate_node_id(node_id)
        _validate_policy(policy)
        ordered = _validate_parents(node_id, parents)
        if not isinstance(artifact_ref, ArtifactRef):
            raise SourceLifecycleError("topic reference must be an ArtifactRef")
        if artifact_ref.family != TOPIC_MEMORY_FAMILY:
            raise SourceLifecycleError("topic reference must belong to Topic Memory")
        reference = _dump_artifact(artifact_ref)
        async with self._runtime._context(self.scope_id):
            async with self._runtime._locked(self.scope_id):
                nodes = await self._nodes()
                existing = _find_node(nodes, node_id)
                if existing is not None:
                    if _matches(existing, TOPIC, reference, ordered, policy):
                        return
                    raise SourceLifecycleError("node is already registered with another binding")
                _require_registered_parents(nodes, ordered)
                try:
                    await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                        artifact_ref.family,
                        artifact_ref.artifact_id,
                        artifact_ref.revision,
                    )
                except PowerContextError as error:
                    raise SourceLifecycleError("Topic Memory reference is not readable") from error
                available = _derived_availability(nodes, ordered, policy)
                await self._insert(
                    _Node(
                        node_id=node_id,
                        family=TOPIC,
                        ordinal=len(nodes),
                        parents=ordered,
                        policy=policy,
                        revision=artifact_ref.revision,
                        reference=reference,
                        available=available,
                        last_sequence=0,
                    )
                )
                self._application.invalidate(self.scope_id)

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event must be a SourceLifecycleEvent")
        async with self._runtime._context(self.scope_id):
            async with self._runtime._locked(self.scope_id):
                nodes = await self._record_event(event)
                available = _propagate(nodes, {event.node_id: event.available})
                await self._apply_memory(nodes, available)
                await self._persist(nodes, available)
                self._application.invalidate(self.scope_id)
        return sorted(node_id for node_id, active in available.items() if active)

    async def resolve(self, node_id: str, purpose: str = "generation") -> object:
        _validate_node_id(node_id)
        if purpose not in _PURPOSES:
            raise SourceLifecycleError("purpose must be generation or audit")
        node = _find_node(await self._nodes(), node_id)
        if node is None:
            raise SourceLifecycleError("node is not registered")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("node is not available for generation")
        return _load_reference(node)

    async def _nodes(self) -> tuple[_Node, ...]:
        if self._database is None:
            raise SourceLifecycleError("Source lifecycle persistence is not configured")
        async with self._database.transaction() as connection:
            await _ensure_schema(connection)
            return await _load_nodes(connection, self.scope_id)

    async def _insert(self, node: _Node) -> None:
        if self._database is None:
            raise SourceLifecycleError("Source lifecycle persistence is not configured")
        async with self._database.transaction() as connection:
            await _ensure_schema(connection)
            if await _node_exists(connection, self.scope_id, node.node_id):
                raise SourceLifecycleError("node is already registered with another binding")
            await connection.execute(
                insert(_NODES_TABLE).values(
                    scope_id=self.scope_id,
                    node_id=node.node_id,
                    family=node.family,
                    ordinal=node.ordinal,
                    parents=json.dumps(list(node.parents)),
                    policy=node.policy,
                    revision=node.revision,
                    reference=node.reference,
                    available=int(node.available),
                    last_sequence=node.last_sequence,
                )
            )

    async def _record_event(self, event: SourceLifecycleEvent) -> tuple[_Node, ...]:
        if self._database is None:
            raise SourceLifecycleError("Source lifecycle persistence is not configured")
        async with self._database.transaction() as connection:
            await _ensure_schema(connection)
            nodes = await _load_nodes(connection, self.scope_id)
            node = _find_node(nodes, event.node_id)
            if node is None or node.family != SOURCE:
                raise SourceLifecycleError("event target is not a registered Source")
            if node.revision != event.revision:
                raise SourceLifecycleError("event revision does not match the registered Source")
            if event.sequence < node.last_sequence:
                raise SourceLifecycleError("event sequence must not decrease")
            if event.sequence == node.last_sequence and event.available != node.available:
                raise SourceLifecycleError("event sequence was already used with another payload")
            if event.sequence > node.last_sequence:
                await connection.execute(
                    update(_NODES_TABLE)
                    .where(
                        _NODES_TABLE.c.scope_id == self.scope_id,
                        _NODES_TABLE.c.node_id == node.node_id,
                    )
                    .values(available=int(event.available), last_sequence=event.sequence)
                )
                node = _replace(node, available=event.available, last_sequence=event.sequence)
                nodes = tuple(node if item.node_id == node.node_id else item for item in nodes)
            return nodes

    async def _apply_memory(self, nodes: Sequence[_Node], available: dict[str, bool]) -> None:
        memory_nodes = tuple(node for node in nodes if node.family == MEMORY)
        if not memory_nodes:
            return
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            head = await service.head(context.artifacts.memory_artifact_id)
            manifest = {item.entry_id: item for item in head.content.manifest.entries}
            for node in memory_nodes:
                citation = _load_citation(node)
                entry = await service.validate_citation(citation)
                item = manifest.get(entry.entry_id)
                if item is None or item.entry_version_id != entry.entry_version_id:
                    raise SourceLifecycleError("registered Memory entry is no longer the current body")
                wanted = available[node.node_id]
                if wanted and item.state == _MEMORY_STATE_INACTIVE:
                    head = await service.reactivate(head, entries=(entry,), reason=_REASON)
                elif not wanted and item.state == _MEMORY_STATE_ACTIVE:
                    head = await service.forget(head, entries=(entry,), reason=_REASON)
                else:
                    continue
                manifest = {item.entry_id: item for item in head.content.manifest.entries}

    async def _persist(self, nodes: Sequence[_Node], available: dict[str, bool]) -> None:
        if self._database is None:
            raise SourceLifecycleError("Source lifecycle persistence is not configured")
        async with self._database.transaction() as connection:
            await _ensure_schema(connection)
            for node in nodes:
                active = available[node.node_id]
                if node.family == SOURCE or node.available != active:
                    await connection.execute(
                        update(_NODES_TABLE)
                        .where(
                            _NODES_TABLE.c.scope_id == self.scope_id,
                            _NODES_TABLE.c.node_id == node.node_id,
                        )
                        .values(available=int(active))
                    )

    async def _require_readable_source(self, source_ref: SourceRef) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_source(
                source_ref.source_type,
                source_ref.source_id,
            )
        except PowerContextError as error:
            raise SourceLifecycleError("Source reference is not readable") from error


async def _ensure_schema(connection: AsyncConnection) -> None:
    await connection.exec_driver_sql(_SCHEMA)


async def _load_nodes(connection: AsyncConnection, scope_id: str) -> tuple[_Node, ...]:
    rows = (
        (
            await connection.execute(
                select(_NODES_TABLE)
                .where(_NODES_TABLE.c.scope_id == scope_id)
                .order_by(_NODES_TABLE.c.ordinal)
            )
        )
        .mappings()
        .all()
    )
    return tuple(_node_from_row(row) for row in rows)


async def _node_exists(connection: AsyncConnection, scope_id: str, node_id: str) -> bool:
    row = (
        (
            await connection.execute(
                select(_NODES_TABLE.c.node_id).where(
                    _NODES_TABLE.c.scope_id == scope_id,
                    _NODES_TABLE.c.node_id == node_id,
                )
            )
        )
        .first()
    )
    return row is not None


def _node_from_row(values: Mapping[str, Any]) -> _Node:
    return _Node(
        node_id=str(values["node_id"]),
        family=str(values["family"]),
        ordinal=int(values["ordinal"]),
        parents=tuple(json.loads(str(values["parents"]))),
        policy=str(values["policy"]),
        revision=int(values["revision"]),
        reference=str(values["reference"]),
        available=bool(values["available"]),
        last_sequence=int(values["last_sequence"]),
    )


def _replace(node: _Node, *, available: bool, last_sequence: int) -> _Node:
    return _Node(
        node_id=node.node_id,
        family=node.family,
        ordinal=node.ordinal,
        parents=node.parents,
        policy=node.policy,
        revision=node.revision,
        reference=node.reference,
        available=available,
        last_sequence=last_sequence,
    )


def _find_node(nodes: Sequence[_Node], node_id: str) -> _Node | None:
    return next((node for node in nodes if node.node_id == node_id), None)


def _matches(
    node: _Node,
    family: str,
    reference: str,
    parents: tuple[str, ...],
    policy: str,
) -> bool:
    return node.family == family and node.reference == reference and node.parents == parents and node.policy == policy


def _propagate(nodes: Sequence[_Node], overrides: dict[str, bool]) -> dict[str, bool]:
    available: dict[str, bool] = {}
    for node in nodes:
        if node.family == SOURCE:
            available[node.node_id] = overrides.get(node.node_id, node.available)
            continue
        support = [available[parent] for parent in node.parents]
        available[node.node_id] = all(support) if node.policy == "all" else any(support)
    return available


def _derived_availability(nodes: Sequence[_Node], parents: tuple[str, ...], policy: str) -> bool:
    available = {node.node_id: node.available for node in nodes}
    support = [available[parent] for parent in parents]
    return all(support) if policy == "all" else any(support)


def _validate_node_id(node_id: str) -> None:
    if not isinstance(node_id, str) or not node_id.strip():
        raise SourceLifecycleError("node_id must be a non-blank string")


def _validate_policy(policy: str) -> None:
    if policy not in _POLICIES:
        raise SourceLifecycleError("policy must be all or any")


def _validate_parents(node_id: str, parents: Sequence[str]) -> tuple[str, ...]:
    ordered = tuple(parents)
    if not ordered:
        raise SourceLifecycleError("a derived node requires at least one parent")
    for parent in ordered:
        if not isinstance(parent, str) or not parent.strip():
            raise SourceLifecycleError("parent identifiers must be non-blank strings")
        if parent == node_id:
            raise SourceLifecycleError("a node cannot depend on itself")
    if len(set(ordered)) != len(ordered):
        raise SourceLifecycleError("parents must not repeat")
    return ordered


def _require_registered_parents(nodes: Sequence[_Node], parents: tuple[str, ...]) -> None:
    registered = {node.node_id for node in nodes}
    for parent in parents:
        if parent not in registered:
            raise SourceLifecycleError("parents must be registered before their dependents")


def _dump_source(source_ref: SourceRef) -> str:
    return json.dumps(
        {"source_type": source_ref.source_type, "source_id": source_ref.source_id},
        sort_keys=True,
    )


def _dump_citation(citation: MemoryCitation) -> str:
    return json.dumps(
        {
            "memory_ref": citation.memory_ref.model_dump(mode="json"),
            "entry_id": citation.entry_id,
            "entry_version_id": citation.entry_version_id,
        },
        sort_keys=True,
    )


def _dump_artifact(artifact_ref: ArtifactRef) -> str:
    return json.dumps(artifact_ref.model_dump(mode="json"), sort_keys=True)


def _load_reference(node: _Node) -> object:
    payload = json.loads(node.reference)
    if node.family == SOURCE:
        return SourceRef(source_type=str(payload["source_type"]), source_id=str(payload["source_id"]))
    if node.family == MEMORY:
        return MemoryCitation(
            memory_ref=ArtifactRef.model_validate(payload["memory_ref"]),
            entry_id=str(payload["entry_id"]),
            entry_version_id=str(payload["entry_version_id"]),
        )
    return ArtifactRef.model_validate(payload)


def _load_citation(node: _Node) -> MemoryCitation:
    reference = _load_reference(node)
    if not isinstance(reference, MemoryCitation):
        raise SourceLifecycleError("registered node does not carry a Memory citation")
    return reference


def _artifact_id(node: _Node) -> str:
    return str(json.loads(node.reference)["artifact_id"])


__all__ = ["SourceLifecycleApplication", "SourceLifecycleError", "SourceLifecycleEvent"]
