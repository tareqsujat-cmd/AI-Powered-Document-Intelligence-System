"""Validation results, stored. Shared by the pipeline and by manual corrections."""

from datetime import date

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from app.db.enums import Severity
from app.db.models import Document, ExtractionResult, ValidationError
from app.extraction.persistence import invoice_from_result
from app.validation.cross_document import check_against_stored
from app.validation.issues import ValidationIssue
from app.validation.rules import validate_invoice


def _issue_key(
    code: str, field: str | None, expected: str | None, actual: str | None
) -> tuple[str, str | None, str | None, str | None]:
    return (code, field, expected, actual)


def store_validation(
    session: Session, document: Document, result: ExtractionResult, *, today: date | None = None
) -> list[ValidationIssue]:
    """Replace a result's validation errors with a fresh check. Does not commit."""
    invoice = invoice_from_result(result)
    issues = [
        *validate_invoice(invoice, today=today),
        *check_against_stored(
            session, document.id, document.file_type, invoice, exclude_result_id=result.id
        ),
    ]
    # A person's decision stands for as long as the issue is the same, so re-checking after an
    # unrelated correction does not bring back everything they already looked at.
    resolved = {
        _issue_key(row.code, row.field, row.expected_value, row.actual_value)
        for row in session.scalars(
            select(ValidationError).where(
                ValidationError.extraction_result_id == result.id, ValidationError.is_resolved
            )
        )
    }
    session.execute(
        delete(ValidationError).where(ValidationError.extraction_result_id == result.id)
    )
    session.add_all(
        ValidationError(
            document_id=document.id,
            extraction_result_id=result.id,
            related_document_id=issue.related_document_id,
            code=issue.code.value,
            severity=issue.severity,
            field=issue.field,
            message=issue.message,
            expected_value=issue.expected_value,
            actual_value=issue.actual_value,
            is_resolved=_issue_key(
                issue.code.value, issue.field, issue.expected_value, issue.actual_value
            )
            in resolved,
        )
        for issue in issues
    )
    session.flush()
    return issues


def current_results(session: Session, document: Document) -> list[ExtractionResult]:
    return list(
        session.scalars(
            select(ExtractionResult)
            .where(ExtractionResult.document_id == document.id, ExtractionResult.is_current)
            .order_by(ExtractionResult.invoice_index)
        )
    )


def update_needs_review(session: Session, document: Document) -> bool:
    """A document needs review while any current invoice has an unresolved error or warning."""
    document.needs_review = bool(
        session.scalar(
            select(
                exists().where(
                    ValidationError.document_id == document.id,
                    ValidationError.extraction_result_id == ExtractionResult.id,
                    ExtractionResult.is_current,
                    ValidationError.severity != Severity.INFO,
                    ValidationError.is_resolved.is_(False),
                )
            )
        )
    )
    return document.needs_review


def refresh_needs_review(session: Session, document: Document) -> bool:
    """Recheck every current invoice in a document, and set its review flag. Does not commit."""
    for result in current_results(session, document):
        store_validation(session, document, result)
    return update_needs_review(session, document)
