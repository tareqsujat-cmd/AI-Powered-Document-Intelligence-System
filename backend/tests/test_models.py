import random
import string
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DataError, IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.db.enums import (
    DocumentStatus,
    ExtractionMethod,
    FileType,
    ProcessingEvent,
    Severity,
)
from app.db.models import (
    EMBEDDING_DIMENSIONS,
    Document,
    DocumentPage,
    Embedding,
    ExtractedText,
    ExtractionResult,
    LineItem,
    ProcessingHistory,
    ValidationError,
)

pytestmark = pytest.mark.db


def make_document(**overrides: object) -> Document:
    values: dict[str, object] = {
        "original_filename": "invoice.pdf",
        "storage_path": f"/data/uploads/{uuid.uuid4()}.pdf",
        "sha256": "a" * 64,
        "file_type": FileType.PDF,
        "mime_type": "application/pdf",
        "size_bytes": 1024,
    }
    return Document(**{**values, **overrides})


def make_result(document: Document, **overrides: object) -> ExtractionResult:
    values: dict[str, object] = {"document": document, "method": ExtractionMethod.LLM}
    return ExtractionResult(**{**values, **overrides})


def add_page_text(db: Session, document: Document, content: str, page_number: int = 1) -> None:
    page = DocumentPage(document_id=document.id, page_number=page_number)
    db.add(page)
    db.flush()
    db.add(ExtractedText(document_id=document.id, page_id=page.id, content=content))
    db.commit()


def persisted(db: Session, document: Document) -> Document:
    db.add(document)
    db.commit()
    return document


def test_document_defaults(db: Session) -> None:
    document = persisted(db, make_document())

    assert document.status is DocumentStatus.QUEUED
    assert document.needs_review is False
    assert document.is_retryable is True
    assert document.created_at is not None
    assert document.updated_at is not None


def test_failed_document_requires_a_reason(db: Session) -> None:
    db.add(make_document(status=DocumentStatus.FAILED))

    with pytest.raises(IntegrityError, match="failed_has_reason"):
        db.commit()


def test_failed_document_with_reason_is_accepted(db: Session) -> None:
    document = persisted(
        db, make_document(status=DocumentStatus.FAILED, failure_reason="encrypted PDF")
    )

    assert db.get(Document, document.id).failure_reason == "encrypted PDF"


def test_invalid_status_rejected_by_database_check(db: Session) -> None:
    document = persisted(db, make_document())

    with pytest.raises(IntegrityError, match="document_status"):
        db.execute(
            text("UPDATE documents SET status = 'bogus' WHERE id = :id"), {"id": document.id}
        )


def test_progress_out_of_range_rejected(db: Session) -> None:
    db.add(make_document(progress=101))

    with pytest.raises(IntegrityError, match="progress_range"):
        db.commit()


def test_unknown_invoice_fields_are_stored_as_null_with_a_reason(db: Session) -> None:
    document = persisted(db, make_document())
    result = make_result(
        document,
        field_metadata={"total": {"confidence": 0.0, "reason": "not found in document"}},
    )
    db.add(result)
    db.commit()
    db.expire(result)

    assert result.total is None
    assert result.invoice_number is None
    assert result.field_metadata["total"]["reason"] == "not found in document"


def test_money_keeps_decimal_precision(db: Session) -> None:
    document = persisted(db, make_document())
    result = make_result(
        document,
        subtotal=Decimal("1234567.1200"),
        tax=Decimal("0.1"),
        total=Decimal("1234567.2200"),
        invoice_date=date(2026, 1, 31),
    )
    db.add(result)
    db.commit()
    db.expire(result)

    assert result.subtotal + result.tax == result.total


def test_one_document_can_hold_many_current_invoices(db: Session) -> None:
    document = persisted(db, make_document())
    db.add_all(
        [make_result(document, invoice_index=index, page_start=index + 1) for index in range(3)]
    )
    db.commit()

    assert db.scalar(select(func.count()).select_from(ExtractionResult)) == 3


def test_only_one_current_version_per_invoice(db: Session) -> None:
    document = persisted(db, make_document())
    db.add(make_result(document, version=1))
    db.commit()
    db.add(make_result(document, version=2))

    with pytest.raises(IntegrityError, match="uq_extraction_results_current_invoice"):
        db.commit()


def test_superseded_versions_are_kept(db: Session) -> None:
    document = persisted(db, make_document())
    db.add(make_result(document, version=1, is_current=False))
    db.add(make_result(document, version=2))
    db.commit()

    assert db.scalar(select(func.count()).select_from(ExtractionResult)) == 2


def test_duplicate_keys_are_allowed_so_they_can_be_flagged(db: Session) -> None:
    first, second = persisted(db, make_document()), persisted(db, make_document())
    db.add_all(
        [make_result(first, duplicate_key="k" * 64), make_result(second, duplicate_key="k" * 64)]
    )
    db.commit()

    found = db.scalars(
        select(ExtractionResult).where(
            ExtractionResult.duplicate_key == "k" * 64, ExtractionResult.is_current
        )
    ).all()
    assert len(found) == 2


def test_duplicate_page_number_rejected(db: Session) -> None:
    document = persisted(db, make_document())
    db.add_all(
        [
            DocumentPage(document_id=document.id, page_number=1),
            DocumentPage(document_id=document.id, page_number=1),
        ]
    )

    with pytest.raises(IntegrityError):
        db.commit()


def test_page_number_is_one_based(db: Session) -> None:
    document = persisted(db, make_document())
    db.add(DocumentPage(document_id=document.id, page_number=0))

    with pytest.raises(IntegrityError, match="page_number_positive"):
        db.commit()


def test_line_items_are_ordered_by_position(db: Session) -> None:
    document = persisted(db, make_document())
    result = make_result(document)
    result.line_items = [
        LineItem(position=2, description="b"),
        LineItem(position=1, description="a"),
    ]
    db.add(result)
    db.commit()
    db.expire(result)

    assert [item.description for item in result.line_items] == ["a", "b"]


def test_deleting_document_cascades_to_every_dependent_table(db: Session) -> None:
    document = persisted(db, make_document())
    add_page_text(db, document, "text")
    result = make_result(document)
    result.line_items = [LineItem(position=1)]
    db.add(result)
    db.flush()
    db.add_all(
        [
            ValidationError(
                document_id=document.id,
                extraction_result_id=result.id,
                code="TOTAL_MISMATCH",
                severity=Severity.ERROR,
                message="m",
            ),
            ProcessingHistory(document_id=document.id, event=ProcessingEvent.UPLOADED),
            Embedding(
                document_id=document.id,
                chunk_index=0,
                content="c",
                embedding=[0.0] * EMBEDDING_DIMENSIONS,
                model="m",
            ),
        ]
    )
    db.commit()

    db.execute(delete(Document).where(Document.id == document.id))
    db.commit()

    for model in (
        DocumentPage,
        ExtractedText,
        ExtractionResult,
        LineItem,
        ValidationError,
        ProcessingHistory,
        Embedding,
    ):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__


def test_cross_document_error_survives_deletion_of_related_document(db: Session) -> None:
    document, other = persisted(db, make_document()), persisted(db, make_document())
    result = make_result(document)
    db.add(result)
    db.flush()
    error = ValidationError(
        document_id=document.id,
        extraction_result_id=result.id,
        related_document_id=other.id,
        code="PDF_EXCEL_TOTAL_MISMATCH",
        severity=Severity.ERROR,
        message="m",
    )
    db.add(error)
    db.commit()

    db.execute(delete(Document).where(Document.id == other.id))
    db.commit()
    db.expire(error)

    assert error.related_document_id is None


def test_correction_history_keeps_before_and_after(db: Session) -> None:
    document = persisted(db, make_document())
    db.add(
        ProcessingHistory(
            document_id=document.id,
            event=ProcessingEvent.MANUAL_CORRECTION,
            before={"total": "10.00"},
            after={"total": "12.00"},
            actor="reviewer",
        )
    )
    db.commit()

    entry = db.scalars(select(ProcessingHistory)).one()
    assert entry.before == {"total": "10.00"}
    assert entry.after == {"total": "12.00"}


def test_keyword_search_finds_stemmed_terms_on_the_right_document(db: Session) -> None:
    match, other = persisted(db, make_document()), persisted(db, make_document())
    add_page_text(db, match, "Consulting services invoiced to Contoso, ref INV-2026-001")
    add_page_text(db, other, "Unrelated delivery note")

    for query in ("consult", "contoso", "INV-2026-001"):
        found = db.scalars(
            select(ExtractedText.document_id).where(
                ExtractedText.content_tsv.op("@@")(func.websearch_to_tsquery("english", query))
            )
        ).all()
        assert found == [match.id], query


def test_tsvector_does_not_overflow_on_huge_spreadsheet_text(db: Session) -> None:
    # Unindexed, this exceeds the 1 MB tsvector limit and the INSERT itself would fail.
    rng = random.Random(1)
    alphabet = string.ascii_lowercase + string.digits
    tail_marker = "zzzuniquetailmarker"
    content = " ".join("".join(rng.choices(alphabet, k=5)) for _ in range(800_000))
    document = persisted(db, make_document())

    add_page_text(db, document, f"headermarker {content} {tail_marker}")

    stored = db.scalars(select(ExtractedText)).one()
    assert len(stored.content) > 4_000_000
    assert db.scalar(
        select(func.count())
        .select_from(ExtractedText)
        .where(ExtractedText.content_tsv.op("@@")(func.to_tsquery("english", "headermarker")))
    )


def test_semantic_search_orders_by_cosine_distance(db: Session) -> None:
    document = persisted(db, make_document())

    def unit(axis: int) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        vector[axis] = 1.0
        return vector

    db.add_all(
        [
            Embedding(
                document_id=document.id,
                chunk_index=axis,
                content=f"chunk {axis}",
                embedding=unit(axis),
                model="m",
            )
            for axis in range(3)
        ]
    )
    db.commit()

    nearest = db.scalars(
        select(Embedding).order_by(Embedding.embedding.cosine_distance(unit(1))).limit(1)
    ).one()
    assert nearest.chunk_index == 1


def test_wrong_embedding_dimension_rejected(db: Session) -> None:
    document = persisted(db, make_document())
    db.add(
        Embedding(
            document_id=document.id, chunk_index=0, content="c", embedding=[0.1, 0.2], model="m"
        )
    )

    with pytest.raises((DataError, StatementError)):
        db.commit()
