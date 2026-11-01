import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import ColumnElement, Select, func, literal, or_, select
from sqlalchemy.orm import Session

from app.db.enums import DocumentStatus, FileType
from app.db.models import Document, DocumentPage, Embedding, ExtractedText, ExtractionResult
from app.extraction.normalize import normalize_key
from app.search.embeddings import Embedder

MAX_RESULTS = 100
# Candidates fetched from each method, before grouping by document and merging.
_CANDIDATES = 200
# Below this cosine similarity, a passage has little to do with the query. Measured on invoice pages
# with all-MiniLM-L6-v2: related queries scored 0.154 or more, unrelated ones 0.123 or less.
MIN_SIMILARITY = 0.14
# The usual constant for reciprocal rank fusion. It stops one first place dominating the ranking.
_RRF_K = 60
_SNIPPET_OPTIONS = "StartSel=,StopSel=,MaxWords=40,MinWords=15,MaxFragments=1"
_FIELD_SNIPPET_CHARS = 200


class SearchMode(StrEnum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class SearchFilters:
    file_type: FileType | None = None
    status: DocumentStatus | None = None
    needs_review: bool | None = None


@dataclass(slots=True)
class SearchHit:
    document_id: uuid.UUID
    filename: str
    page_number: int | None
    snippet: str
    score: float
    matched_by: list[str]


def _filtered(
    statement: Select[*tuple[Any, ...]], filters: SearchFilters
) -> Select[*tuple[Any, ...]]:
    if filters.file_type is not None:
        statement = statement.where(Document.file_type == filters.file_type)
    if filters.status is not None:
        statement = statement.where(Document.status == filters.status)
    if filters.needs_review is not None:
        statement = statement.where(Document.needs_review.is_(filters.needs_review))
    return statement


def _best_per_document(hits: Sequence[SearchHit], limit: int) -> list[SearchHit]:
    """Hits arrive best first. A document appears once, at its best passage."""
    seen: set[uuid.UUID] = set()
    best: list[SearchHit] = []
    for hit in hits:
        if hit.document_id not in seen:
            seen.add(hit.document_id)
            best.append(hit)
    return best[:limit]


def _field_hits(session: Session, query: str, filters: SearchFilters) -> list[SearchHit]:
    """Exact invoice numbers and supplier names, which full-text search handles poorly."""
    key = normalize_key(query)
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conditions: list[ColumnElement[bool]] = [
        ExtractionResult.supplier_name.ilike(f"%{escaped}%", escape="\\"),
        ExtractionResult.customer_name.ilike(f"%{escaped}%", escape="\\"),
    ]
    if key:
        conditions.append(ExtractionResult.invoice_key == key)
    statement = (
        select(
            Document.id,
            Document.original_filename,
            ExtractionResult.page_start,
            ExtractionResult.invoice_number,
            ExtractionResult.supplier_name,
            ExtractionResult.invoice_key,
        )
        .join(ExtractionResult, ExtractionResult.document_id == Document.id)
        .where(ExtractionResult.is_current, or_(*conditions))
        .limit(_CANDIDATES)
    )
    hits = []
    for row in session.execute(_filtered(statement, filters)):
        exact_number = bool(key) and row.invoice_key == key
        details = " | ".join(part for part in (row.invoice_number, row.supplier_name) if part)
        hits.append(
            SearchHit(
                document_id=row.id,
                filename=row.original_filename,
                page_number=row.page_start,
                snippet=details[:_FIELD_SNIPPET_CHARS],
                # An exact invoice number is about as strong as a keyword match can be.
                score=1.0 if exact_number else 0.5,
                matched_by=["keyword"],
            )
        )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits


def keyword_search(
    session: Session, query: str, filters: SearchFilters, limit: int
) -> list[SearchHit]:
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank_cd(ExtractedText.content_tsv, tsquery)
    statement = (
        select(
            Document.id,
            Document.original_filename,
            DocumentPage.page_number,
            func.ts_headline("english", ExtractedText.content, tsquery, _SNIPPET_OPTIONS).label(
                "snippet"
            ),
            rank.label("rank"),
        )
        .join(ExtractedText, ExtractedText.document_id == Document.id)
        .join(DocumentPage, DocumentPage.id == ExtractedText.page_id)
        .where(ExtractedText.content_tsv.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(_CANDIDATES)
    )
    text_hits = [
        SearchHit(
            document_id=row.id,
            filename=row.original_filename,
            page_number=row.page_number,
            snippet=row.snippet,
            # ts_rank_cd is unbounded; this maps it below the score of an exact invoice number.
            score=float(row.rank) / (1.0 + float(row.rank)) * 0.9,
            matched_by=["keyword"],
        )
        for row in session.execute(_filtered(statement, filters))
    ]
    combined = sorted(
        [*_field_hits(session, query, filters), *text_hits],
        key=lambda hit: hit.score,
        reverse=True,
    )
    return _best_per_document(combined, limit)


def semantic_search(
    session: Session, query: str, embedder: Embedder, filters: SearchFilters, limit: int
) -> list[SearchHit]:
    vector = embedder.encode([query])[0]
    distance = Embedding.embedding.cosine_distance(vector)
    statement = (
        select(
            Document.id,
            Document.original_filename,
            Embedding.page_number,
            Embedding.content,
            (literal(1.0) - distance).label("similarity"),
        )
        .join(Embedding, Embedding.document_id == Document.id)
        .where(Embedding.model == embedder.model_name)
        .order_by(distance)
        .limit(_CANDIDATES)
    )
    hits = [
        SearchHit(
            document_id=row.id,
            filename=row.original_filename,
            page_number=row.page_number,
            snippet=row.content[: _FIELD_SNIPPET_CHARS * 2],
            score=float(row.similarity),
            matched_by=["semantic"],
        )
        for row in session.execute(_filtered(statement, filters))
        if float(row.similarity) >= MIN_SIMILARITY
    ]
    return _best_per_document(hits, limit)


def hybrid_search(
    session: Session, query: str, embedder: Embedder, filters: SearchFilters, limit: int
) -> list[SearchHit]:
    """Reciprocal rank fusion: ranks are comparable across methods, while raw scores are not."""
    keyword = keyword_search(session, query, filters, _CANDIDATES)
    semantic = semantic_search(session, query, embedder, filters, _CANDIDATES)

    merged: dict[uuid.UUID, SearchHit] = {}
    scores: dict[uuid.UUID, float] = {}
    for hits in (keyword, semantic):
        for rank, hit in enumerate(hits, start=1):
            scores[hit.document_id] = scores.get(hit.document_id, 0.0) + 1.0 / (_RRF_K + rank)
            if hit.document_id in merged:
                merged[hit.document_id].matched_by.extend(hit.matched_by)
            else:
                # Keyword hits come first, and their snippet shows the words that matched.
                merged[hit.document_id] = hit
    ranked = sorted(merged.values(), key=lambda hit: scores[hit.document_id], reverse=True)
    for hit in ranked:
        hit.score = scores[hit.document_id]
    return ranked[:limit]


def search(
    session: Session,
    query: str,
    mode: SearchMode,
    embedder: Embedder,
    filters: SearchFilters,
    limit: int,
) -> list[SearchHit]:
    if mode is SearchMode.KEYWORD:
        return keyword_search(session, query, filters, limit)
    if mode is SearchMode.SEMANTIC:
        return semantic_search(session, query, embedder, filters, limit)
    return hybrid_search(session, query, embedder, filters, limit)
