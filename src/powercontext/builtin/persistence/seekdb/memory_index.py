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

"""seekdb Memory search index combining FTS and vector candidates in one statement."""

from __future__ import annotations

from pyobvector import VECTOR
from sqlalchemy import Table, TextClause, bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemoryCapabilities,
    MemoryChannelHit,
    MemoryProjection,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.persistence.memory_index import memory_channel_hits
from powercontext.builtin.persistence.oceanbase.memory_index import (
    OceanBaseMemoryFTSIndex,
    OceanBaseMemoryVectorIndex,
)
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql

_SEEKDB_HYBRID_SEARCH_SQL = """
SELECT channel, memory_artifact_id, entry_id, entry_version_id, text, distance, fts_score
FROM (
    SELECT
        'fts' AS channel,
        f.memory_artifact_id AS memory_artifact_id,
        f.entry_id AS entry_id,
        f.entry_version_id AS entry_version_id,
        v.text AS text,
        NULL AS distance,
        MATCH(f.searchable_text) AGAINST (:analyzed_query) AS fts_score
    FROM pc_memory_entry_heads AS f
    JOIN pc_memory_entry_versions AS v
      ON v.scope_id = f.scope_id
     AND v.memory_artifact_id = f.memory_artifact_id
     AND v.entry_version_id = f.entry_version_id
    WHERE f.scope_id = :scope_id
      AND f.memory_artifact_id IN :memory_artifact_ids
      AND MATCH(f.searchable_text) AGAINST (:analyzed_query)
      {fts_tags}
    ORDER BY fts_score DESC, f.memory_artifact_id, f.entry_id, f.entry_version_id
    LIMIT :candidate_limit
) AS fts_candidates
UNION ALL
SELECT * FROM (
    SELECT
        'vector' AS channel,
        m.memory_artifact_id AS memory_artifact_id,
        m.entry_id AS entry_id,
        m.entry_version_id AS entry_version_id,
        v.text AS text,
        l2_distance(m.embedding, :query_vector) AS distance,
        0 AS fts_score
    FROM pc_memory_vector_entries AS m
    JOIN pc_memory_entry_versions AS v
      ON v.scope_id = m.scope_id
     AND v.memory_artifact_id = m.memory_artifact_id
     AND v.entry_version_id = m.entry_version_id
    WHERE m.scope_id = :scope_id
      AND m.memory_artifact_id IN :memory_artifact_ids
      {vector_tags}
    ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE
    LIMIT :candidate_limit
) AS vector_candidates
ORDER BY channel, fts_score DESC, distance ASC, memory_artifact_id, entry_id, entry_version_id
"""


class SeekDBMemoryIndex:
    """Reuse the OceanBase projections but read both candidate channels natively.

    Writes, hydration and completeness checks stay with the OceanBase FTS and vector
    strategies so the frozen schema and projection state remain identical. Only a
    ``hybrid`` search diverges: it issues one statement whose two ``UNION ALL``
    branches use the native full-text and vector indexes with their own candidate
    limits, then splits the ordered rows back into the two channel rankings.
    """

    def __init__(self, profile: EmbeddingProfile | None = None) -> None:
        self._fts = OceanBaseMemoryFTSIndex()
        self._vector = None if profile is None else OceanBaseMemoryVectorIndex(profile)
        self._indexes = (self._fts,) if self._vector is None else (self._fts, self._vector)
        self.capabilities = MemoryCapabilities(
            fts=True,
            vector=self._vector is not None,
            hybrid=self._vector is not None,
            tag_filter=True,
            embedding_profile=profile,
        )
        self.tables: tuple[Table, ...] = tuple(table for index in self._indexes for table in index.tables)
        self._hybrid_sql: dict[bool, TextClause] = (
            {}
            if profile is None
            else {
                False: _hybrid_statement(profile, tagged=False),
                True: _hybrid_statement(profile, tagged=True),
            }
        )

    async def initialize(self, connection: AsyncConnection, /) -> None:
        for index in self._indexes:
            await index.initialize(connection)

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        for index in self._indexes:
            await index.replace(connection, scope_id, memory_ref, projections)

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        for index in self._indexes:
            await index.delete(connection, scope_id, memory_ref, entry_ids)

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        for index in self._indexes:
            await index.upsert(connection, scope_id, memory_ref, projections)

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode == "hybrid" and self._vector is not None:
            return await self._hybrid_search(connection, scope_id, request)
        fts: tuple[MemoryChannelHit, ...] = ()
        vector: tuple[MemoryChannelHit, ...] = ()
        for index in self._indexes:
            channels = await index.search(connection, scope_id, request)
            fts += channels.fts
            vector += channels.vector
        return MemorySearchChannels(fts=fts, vector=vector)

    async def _hybrid_search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if self._vector is None:
            raise CapabilityNotSupportedError("hybrid")
        if request.embedding_profile != self._vector.profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        if not await self._vector.vector_complete(connection, scope_id, request.memories, self._vector.profile):
            raise CapabilityNotSupportedError("vector")
        rows = (
            await connection.execute(
                self._hybrid_sql[request.tag_filter is not None],
                {
                    "scope_id": scope_id,
                    "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
                    "analyzed_query": request.analyzed_query,
                    "query_vector": request.query_vector,
                    "candidate_limit": request.candidate_limit,
                    **memory_tag_parameters(request.tag_filter),
                },
            )
        ).mappings().all()
        return MemorySearchChannels(
            fts=memory_channel_hits(tuple(row for row in rows if row["channel"] == "fts"), request.memories),
            vector=memory_channel_hits(tuple(row for row in rows if row["channel"] == "vector"), request.memories),
        )

    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        if self._vector is None:
            return False
        return await self._vector.vector_complete(connection, scope_id, memories, profile)

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        for index in self._indexes:
            projections = await index.hydrate(connection, scope_id, projections)
        return projections


def _hybrid_statement(profile: EmbeddingProfile, *, tagged: bool) -> TextClause:
    sql = _SEEKDB_HYBRID_SEARCH_SQL.format(
        fts_tags=memory_tag_sql("f") if tagged else "",
        vector_tags=memory_tag_sql("m") if tagged else "",
    )
    parameters = [
        bindparam("memory_artifact_ids", expanding=True),
        bindparam("query_vector", type_=VECTOR(profile.dimension)),
    ]
    if tagged:
        parameters.extend((
            bindparam("tag_keys", expanding=True),
            bindparam("tag_hashes", expanding=True),
        ))
    return text(sql).bindparams(*parameters)
