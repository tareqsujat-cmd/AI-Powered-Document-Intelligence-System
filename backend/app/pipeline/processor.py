"""The processing pipeline: one document through every stage, resumable at any point.

Every stage stores its work as it goes, so a retry or a replacement worker carries on rather than
starting over. Repeating OCR because of a single LLM rate limit could cost many minutes.
"""

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.enums import DocumentStatus, PageStatus, ProcessingEvent, ProcessingStage
from app.db.models import Document, DocumentPage, Embedding, ExtractedText
from app.extraction.extractor import extract_invoices
from app.extraction.llm_client import LLMClient
from app.extraction.persistence import save_invoice
from app.extraction.schemas import InvoiceData
from app.pipeline.review import current_results, refresh_needs_review
from app.pipeline.service import record_event
from app.processing.errors import CorruptedFileError, ProcessingError
from app.processing.extract import count_pages, extract_pages
from app.processing.results import PageResult, SheetTable
from app.search.embeddings import Embedder, TextChunk, chunk_pages

logger = logging.getLogger(__name__)

# Share of the progress bar given to each stage: text extraction is usually by far the longest.
_TEXT_START, _TEXT_END = 5, 60
_LLM_END = 85
_VALIDATION_END = 90
_INDEX_END = 99
# OCR pages take seconds each, so committing each one costs nothing. Text layer pages can take
# milliseconds each, where committing one by one would be the slowest part of the job.
_COMMIT_INTERVAL_SECONDS = 1.0
_EMBEDDING_BATCH = 64


def _heartbeat(document: Document) -> None:
    """Show this document is alive. Stall detection cannot rely on some other column changing."""
    document.updated_at = datetime.now(UTC)


class TaskSuperseded(Exception):
    """A newer task now owns this document, so this one must stop writing to it."""


def _ensure_owner(session: Session, document_id: uuid.UUID, task_id: str | None) -> None:
    if task_id is None:
        return
    owner = session.scalar(select(Document.task_id).where(Document.id == document_id))
    if owner != task_id:
        raise TaskSuperseded


@contextmanager
def _stage(
    session: Session, document: Document, stage: ProcessingStage, progress: int
) -> Iterator[None]:
    document.stage = stage
    document.progress = max(document.progress, progress)
    record_event(session, document, ProcessingEvent.STAGE_STARTED, stage=stage)
    session.commit()
    started = time.perf_counter()
    yield
    record_event(
        session,
        document,
        ProcessingEvent.STAGE_COMPLETED,
        stage=stage,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    session.commit()


def _span_progress(done: int, total: int | None, start: int, end: int) -> int:
    if not total:
        return start
    return start + (end - start) * min(done, total) // total


def _store_page(session: Session, document: Document, page: PageResult) -> None:
    stored = DocumentPage(
        document_id=document.id,
        page_number=page.page_number,
        sheet_name=page.sheet_name,
        source=page.source,
        status=page.status,
        ocr_confidence=page.ocr_confidence,
        processing_ms=page.processing_ms,
        error=page.error,
        warnings=page.warnings,
        table_data=asdict(page.table) if page.table is not None else None,
    )
    session.add(stored)
    session.flush()
    if page.text:
        session.add(ExtractedText(document_id=document.id, page_id=stored.id, content=page.text))


def _extract_text(
    session: Session, document: Document, settings: Settings, task_id: str | None
) -> None:
    path = Path(document.storage_path)
    if not path.is_file():
        raise CorruptedFileError("The stored file is missing. Upload it again.")

    stored_pages = (
        session.scalar(
            select(func.max(DocumentPage.page_number)).where(
                DocumentPage.document_id == document.id
            )
        )
        or 0
    )
    has_earlier_text = bool(
        session.scalar(
            select(func.count())
            .select_from(ExtractedText)
            .where(ExtractedText.document_id == document.id)
        )
    )
    if stored_pages and not has_earlier_text:
        # Nothing readable so far, so a retry should try those pages again rather than skip them.
        session.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
        stored_pages = 0
    document.page_count = count_pages(path, document.file_type)
    last_commit = time.monotonic()
    pages = extract_pages(
        path,
        document.file_type,
        settings,
        start_page=stored_pages + 1,
        has_earlier_text=has_earlier_text,
    )
    try:
        for page in pages:
            _store_page(session, document, page)
            stored_pages = page.page_number
            now = time.monotonic()
            if now - last_commit >= _COMMIT_INTERVAL_SECONDS:
                _ensure_owner(session, document.id, task_id)
                document.progress = max(
                    document.progress,
                    _span_progress(stored_pages, document.page_count, _TEXT_START, _TEXT_END),
                )
                _heartbeat(document)
                session.commit()
                last_commit = now
    except ProcessingError:
        # Keep what was read, as it explains the failure. The document fails either way.
        session.commit()
        raise
    document.page_count = stored_pages
    document.text_extracted_at = datetime.now(UTC)
    session.commit()


def _load_pages(session: Session, document: Document) -> Iterator[PageResult]:
    rows = session.execute(
        select(DocumentPage, ExtractedText.content)
        .outerjoin(ExtractedText, ExtractedText.page_id == DocumentPage.id)
        .where(DocumentPage.document_id == document.id)
        .order_by(DocumentPage.page_number)
    )
    for page, content in rows:
        yield PageResult(
            page_number=page.page_number,
            text=content or "",
            source=page.source,
            status=page.status,
            ocr_confidence=page.ocr_confidence,
            error=page.error,
            sheet_name=page.sheet_name,
            warnings=list(page.warnings),
            table=SheetTable(**page.table_data) if page.table_data else None,
        )


def _extract_invoices(
    session: Session,
    document: Document,
    llm_client: LLMClient | None,
    settings: Settings,
    task_id: str | None,
) -> None:
    done = {result.invoice_index for result in current_results(session, document)}
    pages = list(_load_pages(session, document))
    span_total = max(sum(1 for page in pages if page.status is PageStatus.OK), 1)

    def store(invoice: InvoiceData) -> None:
        _ensure_owner(session, document.id, task_id)
        save_invoice(session, document, invoice)
        document.progress = max(
            document.progress,
            _span_progress(invoice.page_end or 0, span_total, _TEXT_END, _LLM_END),
        )
        _heartbeat(document)
        session.commit()

    extract_invoices(
        pages, llm_client, settings, completed_indexes=frozenset(done), on_invoice=store
    )
    if not current_results(session, document):
        raise ProcessingError("No invoice data could be extracted.")


def _index_text(
    session: Session, document: Document, embedder: Embedder, settings: Settings
) -> None:
    session.execute(delete(Embedding).where(Embedding.document_id == document.id))
    session.commit()

    def page_texts() -> Iterator[tuple[int, str]]:
        rows = session.execute(
            select(DocumentPage.page_number, ExtractedText.content)
            .join(ExtractedText, ExtractedText.page_id == DocumentPage.id)
            .where(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number)
        ).all()
        yield from ((row.page_number, row.content) for row in rows)

    chunks = chunk_pages(page_texts(), settings.max_embedding_chunks_per_document)
    batch = []
    for chunk in chunks:
        batch.append(chunk)
        if len(batch) >= _EMBEDDING_BATCH:
            _store_embeddings(session, document, batch, embedder)
            batch = []
    if batch:
        _store_embeddings(session, document, batch, embedder)


def _store_embeddings(
    session: Session, document: Document, batch: list[TextChunk], embedder: Embedder
) -> None:
    vectors = embedder.encode([chunk.content for chunk in batch])
    session.add_all(
        Embedding(
            document_id=document.id,
            page_number=chunk.page_number,
            chunk_index=chunk.index,
            content=chunk.content,
            embedding=vector,
            model=embedder.model_name,
        )
        for chunk, vector in zip(batch, vectors, strict=True)
    )
    _heartbeat(document)
    session.commit()


def fail_document(
    session: Session, document_id: uuid.UUID, reason: str, *, task_id: str | None = None
) -> None:
    """Record a final failure, if this task still owns the document. It can be retried by hand."""
    session.rollback()
    document = session.get(Document, document_id)
    if document is None or (task_id is not None and document.task_id != task_id):
        return
    document.status = DocumentStatus.FAILED
    document.failure_reason = reason
    document.is_retryable = True
    document.processing_finished_at = datetime.now(UTC)
    record_event(
        session, document, ProcessingEvent.STAGE_FAILED, stage=document.stage, message=reason
    )
    session.commit()


def process_document(
    session: Session,
    document_id: uuid.UUID,
    *,
    settings: Settings,
    get_llm_client: Callable[[], LLMClient | None],
    embedder: Embedder,
    task_id: str | None = None,
) -> bool:
    """Run or resume every stage. Returns False if there was nothing for this task to do.

    Raises ProcessingError on failure, leaving the document for the caller to retry or fail.
    """
    document = session.get(Document, document_id)
    if document is None:
        logger.warning("document no longer exists", extra={"document_id": str(document_id)})
        return False
    if task_id is not None and document.task_id != task_id:
        logger.info("task superseded", extra={"document_id": str(document_id)})
        return False
    if document.status not in (DocumentStatus.QUEUED, DocumentStatus.PROCESSING):
        return False

    document.status = DocumentStatus.PROCESSING
    document.attempt_count += 1
    document.processing_started_at = document.processing_started_at or datetime.now(UTC)
    session.commit()

    if document.text_extracted_at is None:
        with _stage(session, document, ProcessingStage.TEXT_EXTRACTION, _TEXT_START):
            _extract_text(session, document, settings, task_id)

    with _stage(session, document, ProcessingStage.LLM_EXTRACTION, _TEXT_END):
        _extract_invoices(session, document, get_llm_client(), settings, task_id)

    with _stage(session, document, ProcessingStage.VALIDATION, _LLM_END):
        _ensure_owner(session, document.id, task_id)
        refresh_needs_review(session, document)
        document.progress = _VALIDATION_END
        session.commit()

    with _stage(session, document, ProcessingStage.INDEXING, _VALIDATION_END):
        try:
            _index_text(session, document, embedder, settings)
        except Exception:
            # Extracted data is more valuable than search. Keep it, and record what went wrong.
            logger.exception("indexing failed", extra={"document_id": str(document_id)})
            session.rollback()
            record_event(
                session,
                document,
                ProcessingEvent.STAGE_FAILED,
                stage=ProcessingStage.INDEXING,
                message="Semantic search indexing failed. Keyword search still works.",
            )
        document.progress = _INDEX_END
        session.commit()

    document.status = DocumentStatus.COMPLETED
    document.stage = ProcessingStage.FINISHED
    document.progress = 100
    document.failure_reason = None
    document.processing_finished_at = datetime.now(UTC)
    session.commit()
    return True
