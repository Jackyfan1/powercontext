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

"""Share one Embedding request between concurrent callers and cache successful vectors."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

_DEFAULT_MAX_BATCH_SIZE = 64
_DEFAULT_COALESCE_MS = 5.0
_DEFAULT_CACHE_SIZE = 1024


@dataclass(slots=True)
class _Slot:
    """A text still being computed, shared by every caller that already asked for it."""

    future: asyncio.Future[tuple[object, EmbeddingVector, InferenceUsage]]


class CoalescingEmbeddingModel:
    """Merge concurrent Embeds into bounded provider batches and reuse completed vectors.

    Texts requested within one coalescing window, or while a batch is still running, join the
    same provider request. Every caller keeps its own usage and receives vectors in its own
    original order, and cancelling one caller never cancels the shared work of the others.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = _DEFAULT_COALESCE_MS,
        cache_size: int = _DEFAULT_CACHE_SIZE,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max batch size must be positive")
        if coalesce_ms < 0:
            raise ValueError("coalescing window must not be negative")
        if cache_size < 0:
            raise ValueError("cache size must not be negative")
        self.profile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, _Slot] = {}
        self._pending: dict[str, None] = {}
        self._flush_task: asyncio.Task[None] | None = None

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one ordered vector per input text, sharing all provider work."""

        items = tuple(texts)
        if not items:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        unique = tuple(dict.fromkeys(items))
        cached: dict[str, EmbeddingVector] = {}
        slots: dict[str, _Slot] = {}
        for text in unique:
            vector = self._cache_get(text)
            if vector is not None:
                cached[text] = vector
                continue
            slot = self._inflight.get(text)
            if slot is None:
                slot = self._register(text)
            slots[text] = slot
        vectors: dict[str, EmbeddingVector] = {}
        usages: dict[object, InferenceUsage] = {}
        for text in unique:
            if text in cached:
                vectors[text] = cached[text]
                continue
            token, vector, usage = await asyncio.shield(slots[text].future)
            vectors[text] = vector
            usages[token] = usage
        return EmbeddingResult(vectors=tuple(vectors[text] for text in items), usage=_merge_usage(usages.values()))

    def _register(self, text: str) -> _Slot:
        slot = _Slot(asyncio.get_running_loop().create_future())
        self._inflight[text] = slot
        self._pending[text] = None
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush())
        return slot

    async def _flush(self) -> None:
        try:
            while True:
                if self._coalesce_seconds > 0:
                    await asyncio.sleep(self._coalesce_seconds)
                else:
                    await asyncio.sleep(0)
                texts = tuple(self._pending)
                self._pending.clear()
                if not texts:
                    return
                for start in range(0, len(texts), self._max_batch_size):
                    await self._run_batch(texts[start : start + self._max_batch_size])
                if not self._pending:
                    return
        finally:
            self._flush_task = None
            if self._pending:
                self._flush_task = asyncio.create_task(self._flush())

    async def _run_batch(self, texts: tuple[str, ...]) -> None:
        try:
            result = await self._model.embed(texts)
            vectors = tuple(result.vectors)
        except Exception as error:
            self._reject(texts, error)
            return
        if len(vectors) != len(texts):
            self._reject(texts, ValueError("embedding vector count does not match the batch size"))
            return
        usage = result.usage if result.usage is not None else InferenceUsage(requests=0)
        token = object()
        for text, vector in zip(texts, vectors, strict=True):
            if self._cache_size:
                self._cache_put(text, vector)
            slot = self._inflight.pop(text, None)
            self._pending.pop(text, None)
            if slot is not None and not slot.future.done():
                slot.future.set_result((token, vector, usage))

    def _reject(self, texts: tuple[str, ...], error: BaseException) -> None:
        for text in texts:
            slot = self._inflight.pop(text, None)
            self._pending.pop(text, None)
            if slot is not None and not slot.future.done():
                slot.future.set_exception(error)

    def _cache_get(self, text: str) -> EmbeddingVector | None:
        if not self._cache_size:
            return None
        vector = self._cache.pop(text, None)
        if vector is None:
            return None
        self._cache[text] = vector
        return vector

    def _cache_put(self, text: str, vector: EmbeddingVector) -> None:
        self._cache[text] = tuple(vector)
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _merge_usage(usages: Any) -> InferenceUsage:
    counts = tuple(usages)
    if not counts:
        return InferenceUsage(requests=0)
    return InferenceUsage(
        requests=sum(usage.requests for usage in counts),
        input_tokens=_sum_tokens(usage.input_tokens for usage in counts),
        output_tokens=_sum_tokens(usage.output_tokens for usage in counts),
    )


def _sum_tokens(values: Any) -> int | None:
    tokens = tuple(values)
    if not tokens or any(value is None for value in tokens):
        return None
    return sum(tokens)


__all__ = ["CoalescingEmbeddingModel"]
