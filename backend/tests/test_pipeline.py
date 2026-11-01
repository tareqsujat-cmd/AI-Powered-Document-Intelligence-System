from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.enums import (
    DocumentStatus,
    ExtractionMethod,
    FileType,
    PageStatus,
    ProcessingEvent,
    ProcessingStage,
    TextSource,
)
from app.db.models import (
    Document,
    DocumentPage,
    Embedding,
    ExtractedText,
    ExtractionResult,
    ProcessingHistory,
    ValidationError,
)
from app.extraction.errors import LLMRejectedError, LLMUnavailableError
from app.pipeline import processor
from app.pipeline.processor import TaskSuperseded, fail_document, process_document
from app.pipeline.service import (
    QUEUE_UNAVAILABLE,
    enqueue_processing,
    is_busy,
    is_stalled,
    prepare_reprocess,
    retryable_failed_documents,
    stalled_documents,
)
from app.processing.errors import CorruptedFileError, EmptyFileError, EncryptedFileError
from app.validation.issues import ValidationCode
from tests.pipeline_helpers import (
    FakeEmbedder,
    FakeLLM,
    invoice_answer,
    invoice_lines,
    make_document,
    make_pdf,
)

pytestmark = pytest.mark.db


def run(
    db: Session,
    document: Document,
    settings: Settings,
    llm: FakeLLM | None = None,
    embedder: FakeEmbedder | None = None,
    **kwargs: Any,
) -> bool:
    return process_document(
        db,
        document.id,
        settings=settings,
        get_llm_client=lambda: llm or FakeLLM(),
        embedder=embedder or FakeEmbedder(),
        **kwargs,
    )


def history(
    db: Session, document: Document
) -> list[tuple[ProcessingEvent, ProcessingStage | None]]:
    rows = db.scalars(
        select(ProcessingHistory)
        .where(ProcessingHistory.document_id == document.id)
        .order_by(ProcessingHistory.id)
    ).all()
    return [(row.event, row.stage) for row in rows]


def count(db: Session, model: Any, document: Document) -> int:
    return db.scalar(
        select(func.count()).select_from(model).where(model.document_id == document.id)
    )


class TestHappyPath:
    def test_a_clean_pdf_completes_with_everything_stored(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))

        assert run(db, document, settings) is True

        assert document.status is DocumentStatus.COMPLETED
        assert document.stage is ProcessingStage.FINISHED
        assert document.progress == 100
        assert document.page_count == 1
        assert document.needs_review is False
        assert document.failure_reason is None
        assert document.attempt_count == 1
        assert document.processing_started_at is not None
        assert document.processing_finished_at is not None
        assert document.text_extracted_at is not None

        page = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document.id)).one()
        assert page.source is TextSource.TEXT_LAYER
        assert page.status is PageStatus.OK
        text = db.scalars(
            select(ExtractedText).where(ExtractedText.document_id == document.id)
        ).one()
        assert "INV-500" in text.content

        result = db.scalars(
            select(ExtractionResult).where(ExtractionResult.document_id == document.id)
        ).one()
        assert result.invoice_number == "INV-500"
        assert result.total == Decimal("1100.0000")
        assert result.is_current
        assert result.line_items[0].description == "Consulting services"
        assert count(db, Embedding, document) >= 1

    def test_every_stage_is_recorded_in_order_with_durations(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))

        run(db, document, settings)

        stages = (
            ProcessingStage.TEXT_EXTRACTION,
            ProcessingStage.LLM_EXTRACTION,
            ProcessingStage.VALIDATION,
            ProcessingStage.INDEXING,
        )
        expected = [
            pair
            for stage in stages
            for pair in (
                (ProcessingEvent.STAGE_STARTED, stage),
                (ProcessingEvent.STAGE_COMPLETED, stage),
            )
        ]
        assert history(db, document) == expected
        completed = db.scalars(
            select(ProcessingHistory).where(
                ProcessingHistory.document_id == document.id,
                ProcessingHistory.event == ProcessingEvent.STAGE_COMPLETED,
            )
        ).all()
        assert all(row.duration_ms is not None and row.duration_ms >= 0 for row in completed)

    def test_progress_only_moves_forward_through_each_stage(
        self, db: Session, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf", pages=[invoice_lines()] * 3))
        seen: list[int] = []
        original = processor._stage

        def recording(session, doc, stage, progress):
            seen.append(progress)
            return original(session, doc, stage, progress)

        monkeypatch.setattr(processor, "_stage", recording)

        run(db, document, settings)

        assert seen == sorted(seen)
        assert seen[0] >= 1
        assert document.progress == 100

    def test_a_batch_pdf_stores_each_invoice_and_its_page_range(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        path = make_pdf(tmp_path / "a.pdf", pages=[invoice_lines("A-100"), invoice_lines("A-200")])
        document = make_document(db, path)
        llm = FakeLLM(invoice_answer("A-100"), invoice_answer("A-200"))

        run(db, document, settings, llm)

        results = db.scalars(
            select(ExtractionResult)
            .where(ExtractionResult.document_id == document.id)
            .order_by(ExtractionResult.invoice_index)
        ).all()
        assert [(r.invoice_number, r.page_start, r.page_end) for r in results] == [
            ("A-100", 1, 1),
            ("A-200", 2, 2),
        ]

    def test_a_recognizable_spreadsheet_needs_no_llm_or_key(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        path = tmp_path / "a.csv"
        path.write_text(
            "Invoice No,Invoice Date,Supplier,Customer,Subtotal,Tax,Total,Currency\n"
            "A-1,2026-05-01,Northwind,Contoso,100.00,10.00,110.00,GBP\n",
            encoding="utf-8",
        )
        document = make_document(db, path, FileType.CSV)

        process_document(
            db,
            document.id,
            settings=settings,
            get_llm_client=lambda: None,
            embedder=FakeEmbedder(),
        )

        result = db.scalars(select(ExtractionResult)).one()
        assert document.status is DocumentStatus.COMPLETED
        assert result.method is ExtractionMethod.RULE_BASED
        assert result.total == Decimal("110.0000")
        page = db.scalars(select(DocumentPage)).one()
        assert page.table_data is not None
        assert page.table_data["headers"][0] == "Invoice No"

    def test_validation_problems_flag_for_review_and_are_stored(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        path = make_pdf(tmp_path / "a.pdf", pages=[invoice_lines(total="1,500.00")])
        document = make_document(db, path)

        run(db, document, settings, FakeLLM(invoice_answer(total="1,500.00")))

        assert document.status is DocumentStatus.COMPLETED
        assert document.needs_review is True
        codes = set(
            db.scalars(
                select(ValidationError.code).where(ValidationError.document_id == document.id)
            )
        )
        assert ValidationCode.TOTAL_MISMATCH.value in codes

    def test_a_second_upload_is_flagged_as_a_duplicate_of_the_first(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        first = make_document(db, make_pdf(tmp_path / "a.pdf"))
        second = make_document(db, make_pdf(tmp_path / "b.pdf"))

        run(db, first, settings)
        run(db, second, settings)

        assert first.needs_review is False
        assert second.needs_review is True
        error = db.scalars(
            select(ValidationError).where(ValidationError.document_id == second.id)
        ).one()
        assert error.code == ValidationCode.DUPLICATE_INVOICE.value
        assert error.related_document_id == first.id

    def test_a_failed_page_among_good_ones_warns_on_the_invoice(
        self, db: Session, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = make_pdf(tmp_path / "a.pdf", pages=[invoice_lines(), invoice_lines()])
        document = make_document(db, path)
        real = processor.extract_pages

        def with_bad_page(*args: Any, **kwargs: Any):
            for page in real(*args, **kwargs):
                if page.page_number == 2:
                    page.status, page.text, page.error = PageStatus.FAILED, "", "Page 2 is damaged."
                yield page

        monkeypatch.setattr(processor, "extract_pages", with_bad_page)

        run(db, document, settings)

        assert document.status is DocumentStatus.COMPLETED
        assert document.needs_review is True
        result = db.scalars(select(ExtractionResult)).one()
        assert result.warnings == ["Page 2 could not be read, so this invoice may be incomplete."]


class TestFailures:
    def test_a_missing_stored_file_fails_without_revealing_the_path(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, tmp_path / "secret-folder" / "gone.pdf", size_bytes=1)

        with pytest.raises(CorruptedFileError) as caught:
            run(db, document, settings)

        assert "secret-folder" not in caught.value.reason

    def test_an_encrypted_pdf_raises_its_specific_error(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        import pymupdf

        path = tmp_path / "a.pdf"
        with pymupdf.open() as source:
            source.new_page().insert_text((50, 50), "secret")
            source.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
        document = make_document(db, path)

        with pytest.raises(EncryptedFileError):
            run(db, document, settings)

    def test_a_blank_pdf_raises_empty_but_keeps_its_pages(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf", pages=[[]]))

        with pytest.raises(EmptyFileError):
            run(db, document, settings)

        assert count(db, DocumentPage, document) == 1

    def test_fail_document_records_the_reason_and_stays_retryable(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="task-1")

        fail_document(db, document.id, "The file is damaged.", task_id="task-1")

        assert document.status is DocumentStatus.FAILED
        assert document.failure_reason == "The file is damaged."
        assert document.is_retryable is True
        assert document.processing_finished_at is not None
        assert history(db, document)[-1][0] is ProcessingEvent.STAGE_FAILED

    def test_a_superseded_task_cannot_fail_a_document(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="new-task")

        fail_document(db, document.id, "stale failure", task_id="old-task")

        assert document.status is DocumentStatus.QUEUED

    def test_indexing_failure_keeps_the_extracted_data(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))

        class BrokenEmbedder(FakeEmbedder):
            def encode(self, texts):
                raise RuntimeError("model file is corrupt")

        run(db, document, settings, embedder=BrokenEmbedder())

        assert document.status is DocumentStatus.COMPLETED
        assert count(db, ExtractionResult, document) == 1
        assert (ProcessingEvent.STAGE_FAILED, ProcessingStage.INDEXING) in history(db, document)

    def test_a_permanent_llm_rejection_propagates(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))

        with pytest.raises(LLMRejectedError):
            run(db, document, settings, FakeLLM(LLMRejectedError("bad key")))


class TestResuming:
    def test_llm_failure_then_retry_does_not_repeat_text_extraction(
        self, db: Session, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))
        reads = {"count": 0}
        real = processor.extract_pages

        def counting(*args: Any, **kwargs: Any):
            reads["count"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(processor, "extract_pages", counting)

        with pytest.raises(LLMUnavailableError):
            run(db, document, settings, FakeLLM(LLMUnavailableError("quota")))
        assert document.text_extracted_at is not None
        assert document.status is DocumentStatus.PROCESSING

        run(db, document, settings, FakeLLM())

        assert reads["count"] == 1
        assert document.status is DocumentStatus.COMPLETED
        assert count(db, DocumentPage, document) == 1
        assert document.attempt_count == 2

    def test_a_batch_interrupted_part_way_keeps_finished_invoices_and_spends_no_more_on_them(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        pages = [invoice_lines(f"B-{index}00") for index in range(1, 5)]
        document = make_document(db, make_pdf(tmp_path / "a.pdf", pages=pages))
        failing = FakeLLM(
            invoice_answer("B-100"), invoice_answer("B-200"), LLMUnavailableError("quota")
        )

        with pytest.raises(LLMUnavailableError):
            run(db, document, settings, failing)
        assert count(db, ExtractionResult, document) == 2

        retry = FakeLLM(invoice_answer("B-300"), invoice_answer("B-400"))
        run(db, document, settings, retry)

        assert retry.calls == 2
        results = db.scalars(
            select(ExtractionResult)
            .where(ExtractionResult.document_id == document.id)
            .order_by(ExtractionResult.invoice_index)
        ).all()
        assert [result.invoice_number for result in results] == ["B-100", "B-200", "B-300", "B-400"]
        assert all(result.version == 1 for result in results)

    def test_a_crash_mid_extraction_resumes_at_the_next_page(
        self, db: Session, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf", pages=[invoice_lines()] * 5))
        real = processor.extract_pages
        starts: list[int] = []

        def crash_after_three(*args: Any, **kwargs: Any):
            starts.append(kwargs["start_page"])
            for page in real(*args, **kwargs):
                if page.page_number == 4 and len(starts) == 1:
                    db.commit()
                    raise KeyboardInterrupt  # stands in for the worker being killed
                yield page

        monkeypatch.setattr(processor, "extract_pages", crash_after_three)

        with pytest.raises(KeyboardInterrupt):
            run(db, document, settings)
        db.rollback()
        stored = count(db, DocumentPage, document)
        assert stored == 3

        run(db, document, settings)

        assert starts == [1, 4]
        assert count(db, DocumentPage, document) == 5
        assert document.status is DocumentStatus.COMPLETED
        numbers = db.scalars(
            select(DocumentPage.page_number)
            .where(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number)
        ).all()
        assert numbers == [1, 2, 3, 4, 5]

    def test_a_retry_of_unreadable_pages_tries_them_again(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf", pages=[[]]))

        with pytest.raises(EmptyFileError):
            run(db, document, settings)
        document.status = DocumentStatus.QUEUED
        db.commit()

        with pytest.raises(EmptyFileError):
            run(db, document, settings)

        assert count(db, DocumentPage, document) == 1

    def test_indexing_again_replaces_embeddings_rather_than_duplicating_them(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))

        run(db, document, settings)
        before = count(db, Embedding, document)
        prepare_reprocess(db, document, resume=True)
        document.status = DocumentStatus.QUEUED
        db.commit()
        run(db, document, settings)

        assert count(db, Embedding, document) == before


class TestOwnership:
    def test_a_stale_task_does_nothing(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="current-task")

        assert run(db, document, settings, task_id="old-task") is False

        assert document.status is DocumentStatus.QUEUED
        assert count(db, DocumentPage, document) == 0

    def test_the_owning_task_runs(self, db: Session, tmp_path: Path, settings: Settings) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="current-task")

        assert run(db, document, settings, task_id="current-task") is True

    def test_a_task_stops_when_it_loses_ownership_part_way(
        self, db: Session, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="task-a")
        real = processor.save_invoice

        def taken_over(session, doc, invoice):
            raise AssertionError("must not store anything after losing ownership")

        monkeypatch.setattr(processor, "save_invoice", taken_over)
        original_load = processor._load_pages

        def replace_owner(session, doc):
            doc.task_id = "task-b"
            session.commit()
            return original_load(session, doc)

        monkeypatch.setattr(processor, "_load_pages", replace_owner)

        with pytest.raises(TaskSuperseded):
            run(db, document, settings, task_id="task-a")

        assert real is not None
        assert count(db, ExtractionResult, document) == 0

    def test_a_finished_document_is_not_processed_again_by_a_duplicate_delivery(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))
        run(db, document, settings)
        llm = FakeLLM()

        assert run(db, document, settings, llm) is False

        assert llm.calls == 0
        assert document.attempt_count == 1

    def test_a_deleted_document_is_skipped(self, db: Session, settings: Settings) -> None:
        import uuid

        assert (
            process_document(
                db,
                uuid.uuid4(),
                settings=settings,
                get_llm_client=lambda: None,
                embedder=FakeEmbedder(),
            )
            is False
        )


class TestService:
    def test_enqueue_saves_the_task_id_before_sending(
        self, db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))
        sent: list[dict[str, Any]] = []

        def fake_send(name: str, **kwargs: Any) -> None:
            db.expire_all()
            sent.append({"name": name, "stored_task_id": document.task_id, **kwargs})

        monkeypatch.setattr("app.pipeline.service.celery_app.send_task", fake_send)

        assert enqueue_processing(db, document) is True

        assert sent[0]["name"] == "documents.process"
        assert sent[0]["args"] == [str(document.id)]
        assert sent[0]["task_id"] == sent[0]["stored_task_id"]
        assert document.status is DocumentStatus.QUEUED

    def test_queue_down_fails_the_document_with_a_retryable_reason(
        self, db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))

        def refuse(*_a: Any, **_k: Any) -> None:
            raise ConnectionError("redis down at internal-host")

        monkeypatch.setattr("app.pipeline.service.celery_app.send_task", refuse)

        assert enqueue_processing(db, document) is False

        assert document.status is DocumentStatus.FAILED
        assert document.failure_reason == QUEUE_UNAVAILABLE
        assert document.is_retryable is True

    def test_full_reprocess_clears_pages_and_supersedes_results(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))
        run(db, document, settings)

        prepare_reprocess(db, document, resume=False, actor="reviewer")
        document.status = DocumentStatus.QUEUED
        db.commit()

        assert count(db, DocumentPage, document) == 0
        assert count(db, Embedding, document) == 0
        assert document.text_extracted_at is None
        assert document.progress == 0
        assert not db.scalars(select(ExtractionResult).where(ExtractionResult.is_current)).all()

        run(db, document, settings)

        results = db.scalars(
            select(ExtractionResult)
            .where(ExtractionResult.document_id == document.id)
            .order_by(ExtractionResult.version)
        ).all()
        assert [(r.version, r.is_current) for r in results] == [(1, False), (2, True)]
        entry = db.scalars(
            select(ProcessingHistory).where(
                ProcessingHistory.event == ProcessingEvent.REPROCESS_REQUESTED
            )
        ).one()
        assert entry.actor == "reviewer"

    def test_resume_reprocess_keeps_earlier_work(
        self, db: Session, tmp_path: Path, settings: Settings
    ) -> None:
        document = make_document(db, make_pdf(tmp_path / "a.pdf"))
        run(db, document, settings)

        prepare_reprocess(db, document, resume=True)
        db.commit()

        assert count(db, DocumentPage, document) == 1
        assert document.text_extracted_at is not None
        assert db.scalars(select(ExtractionResult).where(ExtractionResult.is_current)).one()

    def test_retryable_failed_documents_lists_only_those(self, db: Session, tmp_path: Path) -> None:
        path = make_pdf(tmp_path / "a.pdf")
        make_document(db, path, status=DocumentStatus.COMPLETED)
        retryable = make_document(db, path, status=DocumentStatus.FAILED, failure_reason="x")
        make_document(
            db, path, status=DocumentStatus.FAILED, failure_reason="x", is_retryable=False
        )

        assert [document.id for document in retryable_failed_documents(db)] == [retryable.id]

    def test_stall_detection(self, db: Session, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, stalled_after_seconds=60)
        path = make_pdf(tmp_path / "a.pdf")
        old = datetime.now(UTC) - timedelta(minutes=5)
        stalled = make_document(db, path, status=DocumentStatus.PROCESSING, updated_at=old)
        active = make_document(db, path, status=DocumentStatus.PROCESSING)
        queued = make_document(db, path, status=DocumentStatus.QUEUED, updated_at=old)
        done = make_document(db, path, status=DocumentStatus.COMPLETED, updated_at=old)

        assert is_stalled(stalled, settings) is True
        assert is_stalled(active, settings) is False
        assert is_busy(stalled, settings) is False
        assert is_busy(active, settings) is True
        assert is_busy(queued, settings) is True
        assert is_busy(done, settings) is False
        assert [document.id for document in stalled_documents(db, settings)] == [stalled.id]
