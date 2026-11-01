import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import SessionDep, get_document_or_404, results_with_details
from app.api.rate_limit import limit_requests
from app.api.schemas import (
    CorrectionRequest,
    LineItemCorrection,
    ResultOut,
    ReviewRequest,
    ReviewResponse,
    ValidationErrorOut,
    plain_value,
)
from app.db.enums import DocumentStatus, ProcessingEvent
from app.db.models import Document, ExtractionResult, LineItem, ProcessingHistory, ValidationError
from app.extraction.normalize import duplicate_key, normalize_key
from app.extraction.persistence import invoice_from_result
from app.extraction.schemas import INVOICE_FIELDS
from app.pipeline.review import refresh_needs_review, update_needs_review

router = APIRouter(tags=["corrections"], dependencies=[Depends(limit_requests)])

_BUSY_STATUSES = (DocumentStatus.QUEUED, DocumentStatus.PROCESSING)
_LineValues = tuple[str | None, Decimal | None, Decimal | None, Decimal | None]


def _apply_fields(
    result: ExtractionResult, body: CorrectionRequest
) -> tuple[dict[str, Any], dict[str, Any]]:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    metadata = dict(result.field_metadata)
    for name in INVOICE_FIELDS:
        if name not in body.model_fields_set:
            continue
        old, new = getattr(result, name), getattr(body, name)
        if old == new:
            continue
        setattr(result, name, new)
        before[name], after[name] = plain_value(old), plain_value(new)
        cleared = new is None
        metadata[name] = {
            "confidence": 0.0 if cleared else 1.0,
            "reason": f"{'Cleared' if cleared else 'Corrected'} by {body.actor}.",
            "source_snippet": None,
            "source_page": None,
        }
    if before:
        # A new dict, because SQLAlchemy does not see changes made inside a JSONB value.
        result.field_metadata = metadata
    return before, after


def _line_values(item: LineItem | LineItemCorrection) -> _LineValues:
    return (item.description, item.quantity, item.unit_price, item.amount)


def _plain_lines(lines: list[_LineValues]) -> list[dict[str, str | None]]:
    return [
        {
            "description": description,
            "quantity": plain_value(quantity),
            "unit_price": plain_value(unit_price),
            "amount": plain_value(amount),
        }
        for description, quantity, unit_price, amount in lines
    ]


def _replace_line_items(
    session: Session, result: ExtractionResult, items: list[LineItemCorrection]
) -> tuple[list[dict[str, str | None]], list[dict[str, str | None]]] | None:
    """Returns the lines before and after, or None if nothing changed."""
    old = [_line_values(item) for item in result.line_items]
    new = [_line_values(item) for item in items]
    if old == new:
        return None
    # Flushed first: SQLAlchemy inserts before it deletes, which would break the unique position.
    result.line_items.clear()
    session.flush()
    result.line_items.extend(
        LineItem(
            position=position,
            description=item.description,
            quantity=item.quantity,
            unit_price=item.unit_price,
            amount=item.amount,
            field_metadata={"confidence": 1.0, "source_snippet": None, "source_page": None},
        )
        for position, item in enumerate(items, start=1)
    )
    return _plain_lines(old), _plain_lines(new)


def _refresh_documents_citing(session: Session, document: Document) -> None:
    """Other documents may report a conflict with values that have just changed.

    A locked one is being corrected right now, and will check against this one itself. A busy one
    gets checked at the end of its processing.
    """
    citing = select(ValidationError.document_id).where(
        ValidationError.related_document_id == document.id,
        ValidationError.document_id != document.id,
    )
    others = session.scalars(
        select(Document)
        .where(Document.id.in_(citing), Document.status.not_in(_BUSY_STATUSES))
        .with_for_update(skip_locked=True)
    ).all()
    for other in others:
        refresh_needs_review(session, other)


@router.patch("/documents/{document_id}/results/{result_id}", response_model=ResultOut)
def correct_result(
    document_id: uuid.UUID, result_id: uuid.UUID, body: CorrectionRequest, session: SessionDep
) -> ResultOut:
    # Locked, so that reprocessing cannot start between this check and the commit.
    document = get_document_or_404(session, document_id, lock=True)
    result = session.scalar(
        results_with_details().where(
            ExtractionResult.id == result_id, ExtractionResult.document_id == document_id
        )
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Result not found.")
    if not result.is_current:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This result has been replaced by a newer version. Reload it and try again.",
        )
    if document.status in _BUSY_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The document is being processed. Try again when it has finished.",
        )

    before, after = _apply_fields(result, body)
    if "line_items" in body.model_fields_set:
        changed_lines = _replace_line_items(session, result, body.line_items)
        if changed_lines:
            before["line_items"], after["line_items"] = changed_lines
    if not before:
        return ResultOut.of(result)

    result.invoice_key = normalize_key(result.invoice_number) or None
    result.duplicate_key = duplicate_key(
        result.supplier_name, result.invoice_number, result.invoice_date, result.total
    )
    result.overall_confidence = invoice_from_result(result).overall_confidence
    session.add(
        ProcessingHistory(
            document_id=document.id,
            event=ProcessingEvent.MANUAL_CORRECTION,
            attempt=document.attempt_count,
            message=f"Corrected invoice {result.invoice_index}.",
            before=before,
            after=after,
            actor=body.actor,
        )
    )
    refresh_needs_review(session, document)
    _refresh_documents_citing(session, document)
    session.commit()

    # store_validation replaces validation errors directly in the database.
    corrected = session.scalars(
        results_with_details()
        .where(ExtractionResult.id == result_id)
        .execution_options(populate_existing=True)
    ).one()
    return ResultOut.of(corrected)


@router.patch(
    "/documents/{document_id}/validation-errors/{error_id}", response_model=ReviewResponse
)
def review_validation_error(
    document_id: uuid.UUID, error_id: uuid.UUID, body: ReviewRequest, session: SessionDep
) -> ReviewResponse:
    """Mark an issue as reviewed: a false alarm, or a value a person has confirmed is right."""
    document = get_document_or_404(session, document_id, lock=True)
    error = session.scalar(
        select(ValidationError)
        .join(ExtractionResult, ValidationError.extraction_result_id == ExtractionResult.id)
        .where(
            ValidationError.id == error_id,
            ValidationError.document_id == document_id,
            ExtractionResult.is_current,
        )
    )
    if error is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Validation error not found.")
    if document.status in _BUSY_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The document is being processed. Try again when it has finished.",
        )
    if error.is_resolved != body.is_resolved:
        session.add(
            ProcessingHistory(
                document_id=document.id,
                event=ProcessingEvent.MANUAL_CORRECTION,
                attempt=document.attempt_count,
                message=f"{'Resolved' if body.is_resolved else 'Reopened'} issue {error.code}.",
                before={"code": error.code, "field": error.field, "is_resolved": error.is_resolved},
                after={"code": error.code, "field": error.field, "is_resolved": body.is_resolved},
                actor=body.actor,
            )
        )
        error.is_resolved = body.is_resolved
        session.flush()
        update_needs_review(session, document)
        session.commit()
    return ReviewResponse(
        validation_error=ValidationErrorOut.of(error), needs_review=document.needs_review
    )
