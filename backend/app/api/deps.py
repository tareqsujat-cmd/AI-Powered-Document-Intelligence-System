import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import Select, select
from sqlalchemy.orm import Session, defer, selectinload

from app.core.config import Settings, get_settings
from app.db.models import Document, ExtractionResult
from app.db.session import get_db

SessionDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_document_or_404(
    session: Session, document_id: uuid.UUID, *, lock: bool = False
) -> Document:
    """lock=True holds the row until commit, so a status check stays true while acting on it."""
    document = session.get(Document, document_id, with_for_update=True if lock else None)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    return document


def results_with_details() -> Select[ExtractionResult]:
    """Results with everything the API returns, loaded up front. raw_response is never returned."""
    return select(ExtractionResult).options(
        defer(ExtractionResult.raw_response),
        selectinload(ExtractionResult.line_items),
        selectinload(ExtractionResult.validation_errors),
    )
