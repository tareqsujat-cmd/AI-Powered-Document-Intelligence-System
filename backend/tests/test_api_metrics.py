from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.db.enums import (
    DocumentStatus,
    ExtractionMethod,
    PageStatus,
    ProcessingEvent,
    ProcessingStage,
    Severity,
    TextSource,
)
from app.db.models import (
    Document,
    DocumentPage,
    ExtractionResult,
    ProcessingHistory,
    ValidationError,
)
from tests.api_helpers import API, Api, add_result, make_document, make_invoice

pytestmark = pytest.mark.db
pytest_plugins = ["tests.api_helpers"]

START = datetime(2026, 1, 1, tzinfo=UTC)


def completed_in(db: Session, seconds: float, **fields: Any) -> Document:
    return make_document(
        db,
        processing_started_at=START,
        processing_finished_at=START + timedelta(seconds=seconds),
        **fields,
    )


def add_issue(
    db: Session,
    result: ExtractionResult,
    code: str = "total_mismatch",
    severity: Severity = Severity.ERROR,
) -> None:
    db.add(
        ValidationError(
            document_id=result.document_id,
            extraction_result_id=result.id,
            code=code,
            severity=severity,
            message=code,
        )
    )
    db.commit()


def add_stage_event(
    db: Session,
    document: Document,
    stage: ProcessingStage | None,
    duration_ms: int | None,
    event: ProcessingEvent = ProcessingEvent.STAGE_COMPLETED,
) -> None:
    db.add(
        ProcessingHistory(
            document_id=document.id, event=event, stage=stage, duration_ms=duration_ms
        )
    )
    db.commit()


def add_page(db: Session, document: Document, number: int, **fields: Any) -> None:
    db.add(DocumentPage(document_id=document.id, page_number=number, **fields))
    db.commit()


def test_metrics_on_an_empty_database(api: Api) -> None:
    response = api.client.get(f"{API}/metrics")

    assert response.status_code == 200
    assert response.json() == {
        "documents_total": 0,
        "documents_by_status": {"queued": 0, "processing": 0, "completed": 0, "failed": 0},
        "needs_review": 0,
        "documents_processed": 0,
        "pages_processed": 0,
        "pages_by_source": {"text_layer": 0, "ocr": 0, "spreadsheet": 0},
        "processing_time": {"mean_seconds": None, "p95_seconds": None},
        "mean_ms_per_page": None,
        "success_rate": None,
        "failure_rate": None,
        "validation_issues_by_code": {},
        "validation_issues_by_severity": {"info": 0, "warning": 0, "error": 0},
        "invoices_by_method": {"llm": 0, "rule_based": 0},
        "mean_seconds_by_stage": {},
    }


def test_metrics_on_a_populated_database(api: Api, db: Session) -> None:
    first = completed_in(db, 10, needs_review=True)
    second = completed_in(db, 20, needs_review=True)
    completed_in(db, 30)
    make_document(
        db,
        status=DocumentStatus.FAILED,
        failure_reason="Broke.",
        # Failed documents do not count towards processing times.
        processing_started_at=START,
        processing_finished_at=START + timedelta(hours=9),
    )
    make_document(db, status=DocumentStatus.QUEUED)
    make_document(db, status=DocumentStatus.PROCESSING, needs_review=False)

    add_page(db, first, 1, source=TextSource.TEXT_LAYER, processing_ms=10)
    add_page(db, first, 2, source=TextSource.TEXT_LAYER, processing_ms=20)
    add_page(db, first, 3, source=TextSource.OCR, processing_ms=3000)
    add_page(db, second, 1, source=TextSource.SPREADSHEET, processing_ms=90)
    # Failed: counted as a page, under no source, and with no time.
    add_page(db, second, 2, source=None, status=PageStatus.FAILED)

    old = add_result(db, first, make_invoice())
    add_issue(db, old, "stale_issue", Severity.ERROR)
    current = add_result(db, first, make_invoice())
    add_issue(db, current, "total_mismatch", Severity.ERROR)
    add_issue(db, current, "missing_field", Severity.WARNING)
    add_issue(db, current, "missing_field", Severity.WARNING)
    rule_based = add_result(db, second, make_invoice(method=ExtractionMethod.RULE_BASED))
    add_issue(db, rule_based, "no_line_items", Severity.INFO)

    add_stage_event(db, first, ProcessingStage.TEXT_EXTRACTION, 1000)
    add_stage_event(db, second, ProcessingStage.TEXT_EXTRACTION, 3000)
    add_stage_event(db, first, ProcessingStage.LLM_EXTRACTION, 500)
    # Not completed stages, so they must not change an average.
    add_stage_event(
        db, first, ProcessingStage.TEXT_EXTRACTION, 999_000, ProcessingEvent.STAGE_FAILED
    )
    add_stage_event(db, first, ProcessingStage.VALIDATION, None)
    add_stage_event(db, first, None, 999_000)

    body = api.client.get(f"{API}/metrics").json()

    assert body["documents_total"] == 6
    assert body["documents_by_status"] == {
        "queued": 1,
        "processing": 1,
        "completed": 3,
        "failed": 1,
    }
    assert body["needs_review"] == 2
    assert body["documents_processed"] == 3
    assert body["pages_processed"] == 5
    assert body["pages_by_source"] == {"text_layer": 2, "ocr": 1, "spreadsheet": 1}
    assert body["processing_time"]["mean_seconds"] == pytest.approx(20.0)
    # Interpolated: 95% of the way along 10, 20, 30.
    assert body["processing_time"]["p95_seconds"] == pytest.approx(29.0)
    assert body["mean_ms_per_page"] == pytest.approx(780.0)
    assert body["success_rate"] == pytest.approx(0.75)
    assert body["failure_rate"] == pytest.approx(0.25)
    assert body["validation_issues_by_code"] == {
        "total_mismatch": 1,
        "missing_field": 2,
        "no_line_items": 1,
    }
    assert body["validation_issues_by_severity"] == {"info": 1, "warning": 2, "error": 1}
    assert body["invoices_by_method"] == {"llm": 1, "rule_based": 1}
    assert body["mean_seconds_by_stage"] == {
        "text_extraction": pytest.approx(2.0),
        "llm_extraction": pytest.approx(0.5),
    }


def test_rates_are_null_until_a_document_finishes(api: Api, db: Session) -> None:
    make_document(db, status=DocumentStatus.QUEUED)
    make_document(db, status=DocumentStatus.PROCESSING)

    body = api.client.get(f"{API}/metrics").json()

    assert body["success_rate"] is None
    assert body["failure_rate"] is None


def test_all_failed_gives_a_zero_success_rate(api: Api, db: Session) -> None:
    make_document(db, status=DocumentStatus.FAILED, failure_reason="Broke.")

    body = api.client.get(f"{API}/metrics").json()

    assert body["success_rate"] == 0.0
    assert body["failure_rate"] == 1.0
    assert body["processing_time"] == {"mean_seconds": None, "p95_seconds": None}


def test_completed_document_without_timestamps_is_not_counted_in_times(
    api: Api, db: Session
) -> None:
    make_document(db)
    completed_in(db, 8)

    body = api.client.get(f"{API}/metrics").json()

    assert body["processing_time"]["mean_seconds"] == pytest.approx(8.0)
    assert body["processing_time"]["p95_seconds"] == pytest.approx(8.0)
