import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.rate_limit import limit_searches
from app.db.enums import DocumentStatus, FileType
from app.db.session import get_db
from app.search.embeddings import Embedder, get_embedder
from app.search.service import MAX_RESULTS, SearchFilters, SearchMode, search

router = APIRouter(prefix="/search", tags=["search"], dependencies=[Depends(limit_searches)])


class SearchHitResponse(BaseModel):
    document_id: uuid.UUID
    filename: str
    page_number: int | None
    snippet: str
    score: float
    matched_by: list[str]


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    results: list[SearchHitResponse]


@router.get("", response_model=SearchResponse)
def search_documents(
    db: Annotated[Session, Depends(get_db)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    q: Annotated[str, Query(min_length=1, max_length=500)],
    mode: SearchMode = SearchMode.HYBRID,
    file_type: FileType | None = None,
    status: DocumentStatus | None = None,
    needs_review: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_RESULTS)] = 20,
) -> SearchResponse:
    """Search processed documents by keyword, by meaning, or both, best match first."""
    filters = SearchFilters(file_type=file_type, status=status, needs_review=needs_review)
    hits = search(db, q, mode, embedder, filters, limit)
    return SearchResponse(
        query=q,
        mode=mode,
        results=[
            SearchHitResponse(
                document_id=hit.document_id,
                filename=hit.filename,
                page_number=hit.page_number,
                snippet=hit.snippet,
                score=hit.score,
                matched_by=hit.matched_by,
            )
            for hit in hits
        ],
    )
