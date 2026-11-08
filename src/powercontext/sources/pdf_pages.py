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

"""Physical-page Sources for offline financial-report PDF documents."""

from __future__ import annotations

import bisect
import hashlib
import io
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import BaseModel, JsonValue
from pypdf import PdfReader

from powercontext.builtin.artifacts.search import analyze_fts_query, analyze_text
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime import ScopedMemoryApplication

PDF_PAGE_SOURCE_NAME = "pdf-page"

_SEARCH_RESULT_LIMIT = 50
_MAX_QUERY_VARIANTS = 12
_BM25_K1 = 1.2
_BM25_B = 0.75
_PROXIMITY_WINDOW = 6
_PROXIMITY_WEIGHT = 0.4
_MIN_PREFIX_LENGTH = 4
_SUFFIX_MARGIN = 4
_MAX_PREFIX_MATCHES = 24
_SYNONYM_WEIGHT = 0.45
_STATEMENT_BONUS_RATIO = 0.8
_STATEMENT_NUMERIC_RATIO = 0.1
_STATEMENT_HEADING_TOKENS = 48
_PAGE_ADJACENCY_WEIGHT = 0.12

_GEOGRAPHY_SYNONYMS = (
    "region",
    "regions",
    "country",
    "countries",
    "territory",
    "territories",
    "segment",
    "segments",
    "division",
    "divisions",
)

_FINANCIAL_QUERY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "geographic": _GEOGRAPHY_SYNONYMS,
    "geography": _GEOGRAPHY_SYNONYMS,
    "geographies": _GEOGRAPHY_SYNONYMS,
    "legal": ("litigation", "proceedings", "lawsuit", "lawsuits", "claims", "regulatory", "investigations"),
    "battles": ("litigation", "proceedings", "lawsuit", "lawsuits", "claims"),
    "litigation": ("legal", "proceedings", "lawsuit", "claims"),
    "restructuring": ("restructuring", "charges", "severance", "impairment"),
    "capex": ("capital", "expenditures", "purchases", "property", "plant", "equipment"),
    "ebitda": ("operating", "profit", "income", "depreciation", "amortization"),
    "margin": ("margin", "percent", "percentage", "rate"),
    "revenue": ("revenue", "sales", "net"),
    "sales": ("revenue", "sales", "net"),
    "expenses": ("expense", "expenses", "costs", "cost"),
    "costs": ("cost", "costs", "expenses", "expense"),
}

_FUNCTION_WORDS = frozenset({
    "a",
    "an",
    "the",
    "and",
    "or",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "what",
    "which",
    "who",
    "how",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "i",
    "we",
    "you",
    "it",
    "its",
    "our",
    "your",
    "this",
    "that",
    "these",
    "those",
    "please",
    "if",
    "then",
    "any",
    "all",
    "also",
    "using",
    "use",
    "only",
    "give",
    "respond",
    "response",
    "answer",
    "question",
    "assume",
    "assuming",
    "perspective",
    "analyst",
    "state",
    "define",
    "defined",
    "rely",
    "relying",
    "details",
    "shown",
    "within",
    "according",
    "based",
    "explicitly",
    "outlined",
    "quantity",
    "kindly",
})


class PDFDocumentInput(BaseModel):
    """One complete offline PDF document and the identity it is stored under."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical PDF page addressed by its document revision and page number."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A captured PDF page usable as retrievable evidence."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int, /) -> str:
    """Return the stable, delimiter-safe identity of one physical PDF page."""

    document_id = _validated_document_id(document_id)
    _validated_sha256(document_sha256)
    if page_number < 1:
        raise ValueError("pdf page number must be a positive physical page index")
    prefix = f"{PDF_PAGE_SOURCE_NAME}:{page_number}:{document_sha256}:"
    direct = f"{prefix}{quote(document_id, safe='')}"
    if len(direct) <= MAX_SOURCE_ID_LENGTH:
        return direct
    digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
    return f"{prefix}~{digest}"


def parse_pdf_document(value: PDFDocumentInput, /) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one PDF document or fail without partial pages."""

    document_id = _validated_document_id(value.document_id)
    content = value.content
    if not content:
        raise ValueError("pdf content must not be empty")
    digest = hashlib.sha256(content).hexdigest()
    reader = _open_reader(content)
    try:
        texts = tuple(_page_text(page) for page in reader.pages)
    except Exception as error:  # noqa: BLE001 - pypdf raises a wide mix of parser errors
        raise ValueError("pdf content could not be parsed completely") from error
    if not texts:
        raise ValueError("pdf content must contain at least one physical page")
    return tuple(
        PDFPageInput(
            document_id=document_id,
            document_sha256=digest,
            page_number=number,
            text=text,
        )
        for number, text in enumerate(texts, start=1)
    )


class PDFPageSourceAdapter:
    """Resolve and read physical PDF page Sources."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one physical PDF page as canonical textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata=_evidence_metadata(source),
        )


def _evidence_metadata(source: PDFPageSource) -> dict[str, JsonValue]:
    return {
        "document_id": source.document_id,
        "document_sha256": source.document_sha256,
        "page_number": source.page_number,
    }


async def search_pdf_pages(
    memory: ScopedMemoryApplication,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` exact citations of distinct pages that answer ``question``."""

    if limit < 1 or not question.strip():
        return ()
    terms = _evidence_terms(question)
    if not terms:
        return ()
    candidates = await _memory_candidates(memory, question)
    ranked = _rank_candidates(candidates, terms)
    matches: list[MemoryCitation] = []
    seen_pages: set[object] = set()
    for citation, page_key in ranked:
        if len(matches) >= limit:
            break
        key = page_key if page_key is not None else (citation.entry_id, citation.entry_version_id)
        if key in seen_pages:
            continue
        seen_pages.add(key)
        matches.append(citation)
    return tuple(matches)


@dataclass
class _Candidate:
    """One Memory entry that may become a page citation."""

    citation: MemoryCitation
    text: str
    page_key: object | None = None


async def _memory_candidates(memory: ScopedMemoryApplication, question: str) -> tuple[_Candidate, ...]:
    """Collect the searchable Memory entries, preferring one complete listing."""

    listing = getattr(memory, "list", None)
    if callable(listing):
        page = await listing()
        candidates = tuple(
            _Candidate(citation=record.citation, text=record.entry.text, page_key=_entry_page_key(record.entry.sources))
            for record in page.entries
        )
        pdf_pages = tuple(candidate for candidate in candidates if candidate.page_key is not None)
        if pdf_pages:
            return pdf_pages
        if candidates:
            return candidates
    return await _searched_candidates(memory, question)


async def _searched_candidates(memory: ScopedMemoryApplication, question: str) -> tuple[_Candidate, ...]:
    """Fall back to Memory search when a complete listing is unavailable."""

    from powercontext.builtin.artifacts.memory.models import MemoryCitation as Citation
    from powercontext.builtin.runtime import SearchMemoryRequest

    candidates: dict[tuple[str, str], _Candidate] = {}
    if not question.strip():
        return ()
    for variant in _query_variants(_evidence_terms(question), question):
        page = await memory.search(SearchMemoryRequest(query=variant, mode="fts", limit=_SEARCH_RESULT_LIMIT))
        for hit in page.hits:
            key = (hit.entry_id, hit.entry_version_id)
            if key not in candidates:
                candidates[key] = _Candidate(
                    citation=Citation(
                        memory_ref=hit.memory_ref,
                        entry_id=hit.entry_id,
                        entry_version_id=hit.entry_version_id,
                    ),
                    text=hit.text,
                )
    return tuple(candidates.values())


def _entry_page_key(sources: tuple[object, ...], /) -> tuple[str, int, str] | None:
    """Derive the physical page identity of one entry from its PDF page Source."""

    for reference in sources:
        source_type = getattr(reference, "source_type", None)
        source_id = getattr(reference, "source_id", None)
        if source_type != PDF_PAGE_SOURCE_NAME or not isinstance(source_id, str):
            continue
        parts = source_id.split(":")
        if len(parts) != 4 or parts[0] != PDF_PAGE_SOURCE_NAME or not parts[1].isdigit():
            continue
        return (parts[2], int(parts[1]), parts[3])
    return None


def _rank_candidates(
    candidates: tuple[_Candidate, ...],
    terms: tuple[str, ...],
) -> list[tuple[MemoryCitation, object | None]]:
    """Rank candidates with BM25 over morphologically expanded query terms."""

    if not candidates:
        return []
    analyzed = tuple(_analyze_page(candidate.text) for candidate in candidates)
    token_counts = tuple(item[0] for item in analyzed)
    token_positions = tuple(item[1] for item in analyzed)
    page_tokens = tuple(item[2] for item in analyzed)
    lengths = tuple(len(tokens) for tokens in page_tokens)
    vocabulary = tuple(sorted({token for counts in token_counts for token in counts}))
    expansions = _expansions(terms, vocabulary)
    variants_by_term = {expansion.term: expansion.variants for expansion in expansions}
    total = len(candidates)
    average_length = max(sum(lengths) / total, 1.0)
    document_frequency = tuple(
        sum(1 for counts in token_counts if any(counts[token] for token in expansion.variants))
        for expansion in expansions
    )
    idf_scores = tuple(
        math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5)) * expansion.weight
        for frequency, expansion in zip(document_frequency, expansions, strict=True)
    )
    weights = {expansion.term: idf for expansion, idf in zip(expansions, idf_scores, strict=True)}
    pairs = _term_pairs(terms)
    markers = _statement_markers(terms)
    scores: list[float] = []
    for index, counts in enumerate(token_counts):
        length = lengths[index]
        score = 0.0
        for expansion, idf in zip(expansions, idf_scores, strict=True):
            frequency = sum(counts[token] for token in expansion.variants)
            if not frequency:
                continue
            normalization = _BM25_K1 * (1.0 - _BM25_B + _BM25_B * (length / average_length))
            score += idf * (frequency * (_BM25_K1 + 1.0)) / (frequency + normalization)
        score += _proximity_bonus(pairs, weights, variants_by_term, token_positions[index])
        scores.append(score)
    _apply_statement_bonus(scores, page_tokens, markers)
    _apply_adjacency_bonus(scores, candidates)
    ordered = sorted(range(total), key=lambda index: (-scores[index], candidates[index].citation.entry_version_id))
    return [(candidates[index].citation, candidates[index].page_key) for index in ordered]


def _proximity_bonus(
    pairs: tuple[tuple[str, str], ...],
    weights: dict[str, float],
    variants_by_term: dict[str, tuple[str, ...]],
    positions: dict[str, tuple[int, ...]],
) -> float:
    """Reward neighbouring query terms that appear near each other on one page."""

    if not pairs:
        return 0.0
    bonus = 0.0
    for left, right in pairs:
        left_positions = _variant_positions(variants_by_term[left], positions)
        if not left_positions:
            continue
        right_positions = _variant_positions(variants_by_term[right], positions)
        if not right_positions:
            continue
        if _within_window(left_positions, right_positions):
            bonus += _PROXIMITY_WEIGHT * (weights[left] + weights[right]) / 2.0
    return bonus


def _apply_statement_bonus(
    scores: list[float],
    page_tokens: tuple[tuple[str, ...], ...],
    markers: tuple[tuple[str, ...], ...],
) -> None:
    """Prefer pages that are themselves the primary statement a question asks about."""

    if not markers or not scores:
        return
    highest = max(scores)
    if highest <= 0.0:
        return
    bonus = highest * _STATEMENT_BONUS_RATIO
    for index, tokens in enumerate(page_tokens):
        if _is_statement_page(tokens, markers):
            scores[index] += bonus


def _apply_adjacency_bonus(scores: list[float], candidates: tuple[_Candidate, ...]) -> None:
    """Prefer pages next to the strongest page because statements commonly continue over a page break."""

    indexes: dict[tuple[str, int, str], int] = {}
    for index, candidate in enumerate(candidates):
        key = candidate.page_key
        if isinstance(key, tuple) and len(key) == 3 and isinstance(key[1], int):
            indexes[key] = index
    boosts: list[tuple[int, float]] = []
    for index, candidate in enumerate(candidates):
        key = candidate.page_key
        if not isinstance(key, tuple) or len(key) != 3:
            continue
        digest, page_number, document = key
        neighbours = [
            scores[neighbour]
            for offset in (-1, 1)
            if (neighbour := indexes.get((digest, page_number + offset, document))) is not None
        ]
        if neighbours:
            boosts.append((index, max(neighbours) * _PAGE_ADJACENCY_WEIGHT))
    for index, boost in boosts:
        scores[index] += boost


def _is_statement_page(tokens: tuple[str, ...], markers: tuple[tuple[str, ...], ...]) -> bool:
    if not tokens:
        return False
    numeric = sum(1 for token in tokens if token.isdigit())
    if numeric / len(tokens) < _STATEMENT_NUMERIC_RATIO:
        return False
    heading = tokens[:_STATEMENT_HEADING_TOKENS]
    return any(_contains_subsequence(heading, marker) for marker in markers)


def _contains_subsequence(tokens: tuple[str, ...], marker: tuple[str, ...]) -> bool:
    width = len(marker)
    return any(tokens[start : start + width] == marker for start in range(len(tokens) - width + 1))


def _statement_markers(terms: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    known = set(terms)
    markers: list[tuple[str, ...]] = []
    if known & {"income", "profit", "earnings", "revenue", "sales", "expenses", "costs", "margin"}:
        markers.append(("statement", "of", "income"))
        markers.append(("statements", "of", "income"))
    if known & {"capex", "depreciation", "amortization"} or ("cash" in known and known & {"flow", "flows"}):
        markers.append(("statement", "of", "cash", "flows"))
    if known & {"balance", "assets", "liabilities", "debt", "inventory"}:
        markers.append(("balance", "sheet"))
    if "equity" in known and known & {"statement", "statements"}:
        markers.append(("statement", "of", "equity"))
    return tuple(markers)


def _variant_positions(variants: tuple[str, ...], positions: dict[str, tuple[int, ...]]) -> tuple[int, ...]:
    found: list[int] = []
    for variant in variants:
        found.extend(positions.get(variant, ()))
    return tuple(sorted(found))


def _within_window(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    for first in left:
        index = bisect.bisect_left(right, first - _PROXIMITY_WINDOW)
        if index < len(right) and right[index] <= first + _PROXIMITY_WINDOW:
            return True
    return False


def _analyze_page(text: str) -> tuple[Counter[str], dict[str, tuple[int, ...]], tuple[str, ...]]:
    tokens = tuple(analyze_text(text).split())
    positions: dict[str, list[int]] = {}
    for position, token in enumerate(tokens):
        positions.setdefault(token, []).append(position)
    return Counter(tokens), {token: tuple(found) for token, found in positions.items()}, tokens


def _evidence_terms(question: str) -> tuple[str, ...]:
    """Return ordered, de-duplicated evidence terms for one natural-language question."""

    terms: list[str] = []
    seen: set[str] = set()
    for term in analyze_fts_query(question).split():
        if term in seen or term in _FUNCTION_WORDS:
            continue
        if len(term) < 2 and not term.isdigit():
            continue
        seen.add(term)
        terms.append(term)
    return tuple(terms)


def _query_variants(terms: tuple[str, ...], question: str) -> tuple[str, ...]:
    """Derive bounded lexical queries so Memory search can surface broad candidate pages."""

    variants: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in variants:
            variants.append(value)

    add(question)
    add(" ".join(terms))
    for left, right in _term_pairs(terms):
        add(f"{left} {right}")
    for term in sorted(terms, key=lambda value: (value.isdigit(), -len(value), value)):
        add(term)
    return tuple(variants[:_MAX_QUERY_VARIANTS])


def _term_pairs(terms: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(zip(terms, terms[1:], strict=False))


@dataclass(frozen=True)
class _Expansion:
    """One weighted group of index tokens that all express one query term."""

    term: str
    variants: tuple[str, ...]
    weight: float


def _expansions(
    terms: tuple[str, ...],
    vocabulary: tuple[str, ...],
) -> tuple[_Expansion, ...]:
    """Expand each query term to exact, morphologically related and synonymous index tokens."""

    expanded: list[_Expansion] = []
    seen: set[str] = set()
    for term in terms:
        expanded.append(_Expansion(term, _term_variants(term, vocabulary), 1.0))
        seen.add(term)
        for synonym in _FINANCIAL_QUERY_SYNONYMS.get(term, ()):
            if synonym in seen:
                continue
            expanded.append(_Expansion(synonym, _term_variants(synonym, vocabulary), _SYNONYM_WEIGHT))
            seen.add(synonym)
    return tuple(expanded)


def _term_variants(term: str, vocabulary: tuple[str, ...]) -> tuple[str, ...]:
    if term.isdigit():
        return (term,)
    variants = {term}
    if len(term) >= _MIN_PREFIX_LENGTH + 2:
        stem = term[: max(_MIN_PREFIX_LENGTH, len(term) - _SUFFIX_MARGIN)]
        start = bisect.bisect_left(vocabulary, stem)
        stop = bisect.bisect_right(vocabulary, f"{stem}\uffff")
        for candidate in vocabulary[start:stop][:_MAX_PREFIX_MATCHES]:
            variants.add(candidate)
    return tuple(sorted(variants))


def _open_reader(content: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("pdf content requires a password that was not supplied")
        if len(reader.pages) < 1:
            raise ValueError("pdf content must contain at least one physical page")
        return reader
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001 - pypdf raises a wide mix of parser errors
        raise ValueError("pdf content is not a readable PDF document") from error


def _page_text(page: object) -> str:
    text = page.extract_text()  # type: ignore[attr-defined]
    return text if isinstance(text, str) else ""


def _validated_document_id(document_id: str) -> str:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("pdf document id must not be blank")
    if document_id != document_id.strip():
        raise ValueError("pdf document id must not contain leading or trailing whitespace")
    return document_id


def _validated_sha256(document_sha256: str) -> None:
    if not isinstance(document_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", document_sha256):
        raise ValueError("pdf document sha256 must be a lowercase hexadecimal digest")


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    projections=(PDFPageTextEvidenceProjection(),),
)

__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
