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

"""Chunked Context Packs and the prefix-reuse delta protocol that rebuilds them."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"

_DEFAULT_CHUNK_BYTES = 512
_MAX_BYTE_COUNT_ATTEMPTS = 8
_DIGEST_PREFIX = "sha256:"
_DIGEST_HEX_LENGTH = 64


class ContextPackError(Exception):
    """A Context Pack or delta violates the wire contract or cannot be rebuilt."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One complete context body split into deterministic, UTF-8 safe chunks."""

    items: tuple[bytes, ...]
    digest: str

    @classmethod
    def build(cls, items: Iterable[bytes], /) -> ContextPack:
        """Build a complete pack from ordered chunks and their derived digest."""

        chunks = tuple(items)
        _validate_items(chunks)
        return cls(chunks, _pack_digest(chunks))

    @classmethod
    def from_content(cls, content: str | bytes, *, chunk_bytes: int = _DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Split one context body into deterministic chunks that stay inside ``chunk_bytes``."""

        raw = content.encode("utf-8") if isinstance(content, str) else content
        return cls.build(_chunk_utf8(raw, chunk_bytes))

    @classmethod
    def from_bytes(cls, data: bytes | bytearray, /) -> ContextPack:
        """Decode and validate one serialized complete pack."""

        payload = _load_object(data)
        _require_fields(payload, "schema", "items", "digest", "byte_count")
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("context pack schema is not supported")
        items = _decode_items(payload["items"])
        digest = payload["digest"]
        if not isinstance(digest, str) or digest != _pack_digest(items):
            raise ContextPackError("context pack digest does not match its items")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count < 0:
            raise ContextPackError("context pack byte count must be a non-negative integer")
        pack = cls(items, digest)
        if byte_count != len(pack.to_bytes()):
            raise ContextPackError("context pack byte count does not match its encoding")
        return pack

    def to_bytes(self) -> bytes:
        """Serialize the pack so that its byte count includes the count field itself."""

        payload: dict[str, Any] = {
            "schema": CONTEXT_PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
        }
        count = 0
        for _ in range(_MAX_BYTE_COUNT_ATTEMPTS):
            payload["byte_count"] = count
            encoded = _canonical_json(payload)
            if len(encoded) == count:
                return encoded
            count = len(encoded)
        raise ContextPackError("context pack byte count does not converge")

    @property
    def byte_count(self) -> int:
        """Return the exact serialized size of the pack."""

        return len(self.to_bytes())


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A retained base prefix plus the target suffix needed to rebuild one pack."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Reuse the longest common chunk prefix of both packs and carry the rest."""

        prefix = _common_prefix_length(base.items, target.items)
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @classmethod
    def from_bytes(cls, data: bytes | bytearray, /) -> ContextPackDelta:
        """Decode and validate one serialized delta."""

        payload = _load_object(data)
        _require_fields(payload, "schema", "base_digest", "target_digest", "prefix_items", "appended")
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("context pack delta schema is not supported")
        base_digest = _require_digest(payload["base_digest"], "base digest")
        target_digest = _require_digest(payload["target_digest"], "target digest")
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("context pack delta prefix must be a non-negative integer")
        return cls(base_digest, target_digest, prefix, _decode_items(payload["appended"]))

    def to_bytes(self) -> bytes:
        """Serialize the delta without the derived byte count field."""

        return _canonical_json({
            "schema": CONTEXT_PACK_DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        })

    @property
    def byte_count(self) -> int:
        """Return the exact serialized size of the delta."""

        return len(self.to_bytes())


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Rebuild the target pack from one base pack and its delta, or fail on any mismatch."""

    if delta.base_digest != base.digest:
        raise ContextPackError("context pack delta was not built from this base pack")
    prefix = delta.prefix_items
    if type(prefix) is not int or prefix < 0:
        raise ContextPackError("context pack delta prefix must be a non-negative integer")
    if prefix > len(base.items):
        raise ContextPackError("context pack delta prefix exceeds the base pack")
    _validate_items(delta.appended)
    target = ContextPack.build((*base.items[:prefix], *delta.appended))
    if target.digest != delta.target_digest:
        raise ContextPackError("rebuilt context pack does not match the delta target digest")
    return target


def _chunk_utf8(raw: bytes, chunk_bytes: int) -> tuple[bytes, ...]:
    """Split encoded text into deterministic chunks without breaking a UTF-8 character."""

    if chunk_bytes < 1:
        raise ContextPackError("chunk limit must be a positive byte count")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("context content must be valid UTF-8") from error
    chunks: list[bytes] = []
    pending = bytearray()
    for character in text:
        encoded = character.encode("utf-8")
        if len(encoded) > chunk_bytes:
            raise ContextPackError("context content contains a character wider than the chunk limit")
        if len(pending) + len(encoded) > chunk_bytes:
            chunks.append(bytes(pending))
            pending = bytearray()
        pending += encoded
    if pending:
        chunks.append(bytes(pending))
    return tuple(chunks)


def _validate_items(items: tuple[bytes, ...]) -> None:
    for item in items:
        if not isinstance(item, bytes):
            raise ContextPackError("context pack items must be bytes")
        try:
            item.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextPackError("context pack items must be valid UTF-8") from error


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": CONTEXT_PACK_SCHEMA, "items": _encode_items(items)}
    return _DIGEST_PREFIX + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _common_prefix_length(left: tuple[bytes, ...], right: tuple[bytes, ...]) -> int:
    prefix = 0
    for first, second in zip(left, right, strict=False):
        if first != second:
            break
        prefix += 1
    return prefix


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_object(data: bytes | bytearray) -> dict[str, Any]:
    try:
        payload = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ContextPackError("context pack payload must be valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ContextPackError("context pack payload must be a JSON object")
    return payload


def _require_fields(payload: dict[str, Any], *fields: str) -> None:
    if set(payload) != set(fields):
        raise ContextPackError(f"context pack payload must contain exactly {', '.join(sorted(fields))}")


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX):
        raise ContextPackError(f"context pack {label} must be a sha256 digest")
    hexdigest = value.removeprefix(_DIGEST_PREFIX)
    if len(hexdigest) != _DIGEST_HEX_LENGTH or any(character not in "0123456789abcdef" for character in hexdigest):
        raise ContextPackError(f"context pack {label} must be a sha256 digest")
    return value


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("context pack items must be an array of Base64 strings")
    decoded: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError("context pack items must be an array of Base64 strings")
        try:
            decoded.append(base64.b64decode(entry, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("context pack items must be valid Base64") from error
    return tuple(decoded)


__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
