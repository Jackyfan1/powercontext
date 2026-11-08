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

"""Remote cross-encoder reranking over a batched HTTP scoring protocol."""

from __future__ import annotations

import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

_DEFAULT_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Score Memory hits with a remote service and order them by the returned scores.

    The service caps how many candidates one request may carry, so the pool is split into
    contiguous batches. Every candidate keeps its one-based rank in the complete pool, which
    lets the response scores map back to the original hits and the final selection stay global.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = _DEFAULT_POLICY_ID,
    ) -> None:
        if batch_size < 1:
            raise ValueError("rerank batch size must be positive")
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Return the globally highest scoring original ranks without changing hit identity."""

        _validate_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            ranks = tuple(range(start + 1, min(start + self._batch_size, len(candidates)) + 1))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [{"rank": rank, "text": candidates[rank - 1].text} for rank in ranks],
                },
            )
            requests += 1
            response.raise_for_status()
            scores.update(_parse_scores(response, ranks))
        selected = tuple(sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected,
            usage=InferenceUsage(requests=requests),
        )


def _parse_scores(response: httpx.Response, ranks: tuple[int, ...]) -> dict[int, float]:
    """Validate one batch response and return its finite scores keyed by global rank."""

    try:
        payload: Any = response.json()
    except ValueError as error:
        raise ValueError("rerank response must be valid JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("rerank response must contain exactly the scores field")
    records = payload["scores"]
    if not isinstance(records, list) or len(records) != len(ranks):
        raise ValueError("rerank response must score every candidate of the batch exactly once")
    expected = set(ranks)
    scores: dict[int, float] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"rank", "score"}:
            raise ValueError("rerank score records must contain exactly rank and score")
        rank = record["rank"]
        if type(rank) is not int or rank not in expected:
            raise ValueError("rerank response must only score the ranks of this batch")
        if rank in scores:
            raise ValueError("rerank response must score every candidate of the batch exactly once")
        score = record["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("rerank scores must be numbers")
        value = float(score)
        if not math.isfinite(value):
            raise ValueError("rerank scores must be finite")
        scores[rank] = value
    if scores.keys() != expected:
        raise ValueError("rerank response must score every candidate of the batch exactly once")
    return scores


def _validate_bounds(candidate_count: int, limit: int) -> None:
    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")
    if limit < 1 or limit > candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")


__all__ = ["HTTPMemoryReranker"]
