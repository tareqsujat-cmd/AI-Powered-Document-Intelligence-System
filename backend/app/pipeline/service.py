"""Queueing and reprocessing, shared by the API and the worker's recovery job."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.enums import DocumentStatus, ProcessingEvent, ProcessingStage
from app.db.models import Document, DocumentPage, Embedding, ExtractionResult, ProcessingHistory
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

PROCESS_TASK_NAME = "documents.process"
QUEUE_UNAVAILABLE = "The processing queue is unavailable. Try again shortly."
MAX_REPROCESS_BATCH = 500


def record_event(
    session: Session,
    document: Document,
    event: ProcessingEvent,
    *,
    stage: ProcessingStage | None = None,
    message: str | None = None,
    duration_ms: int | None = None,
    actor: str = "system",
) -> None:
    session.add(
        ProcessingHistory(
            document_id=document.id,
            event=event,
            stage=stage,
            attempt=document.attempt_count,
            message=message,
            duration_ms=duration_ms,
            actor=actor,
        )
    )


def enqueue_processing(session: Session, document: Document) -> bool:
    """Queue a document and commit. Returns False, with the document failed, if the queue is down.

    The task id is saved first: a worker only acts on the task named on the document, so a stale or
    redelivered task can never run alongside a newer one.
    """
    task_id = str(uuid.uuid4())
    document.task_id = task_id
    document.status = DocumentStatus.QUEUED
    document.failure_reason = None
    session.commit()
    try:
        celery_app.send_task(
            PROCESS_TASK_NAME, args=[str(document.id)], task_id=task_id, retry=False
        )
    except Exception:
        logger.exception("could not queue document", extra={"document_id": str(document.id)})
        document.status = DocumentStatus.FAILED
        document.failure_reason = QUEUE_UNAVAILABLE
        document.is_retryable = True
        session.commit()
        return False
    return True


def is_stalled(document: Document, settings: Settings) -> bool:
    """A document marked processing that has not moved in a long time has lost its worker."""
    if document.status is not DocumentStatus.PROCESSING:
        return False
    idle = datetime.now(UTC) - document.updated_at
    return idle > timedelta(seconds=settings.stalled_after_seconds)


def is_busy(document: Document, settings: Settings) -> bool:
    if document.status is DocumentStatus.QUEUED:
        return True
    return document.status is DocumentStatus.PROCESSING and not is_stalled(document, settings)


def prepare_reprocess(
    session: Session, document: Document, *, resume: bool, actor: str = "system"
) -> None:
    """Get a document ready to run again. Does not commit or queue it.

    resume=True keeps the work already done, so a failure in a late stage does not repeat OCR.
    resume=False starts over. Earlier extraction results stay in the database as old versions, which
    includes any manual corrections made to them.
    """
    record_event(
        session,
        document,
        ProcessingEvent.REPROCESS_REQUESTED,
        message="Resuming from the last completed work." if resume else "Starting over.",
        actor=actor,
    )
    document.stage = ProcessingStage.UPLOAD
    document.processing_finished_at = None
    if resume:
        return

    document.progress = 0
    document.processing_started_at = None
    document.page_count = None
    document.needs_review = False
    document.text_extracted_at = None
    session.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
    session.execute(delete(Embedding).where(Embedding.document_id == document.id))
    session.execute(
        update(ExtractionResult)
        .where(ExtractionResult.document_id == document.id, ExtractionResult.is_current)
        .values(is_current=False)
    )


def retryable_failed_documents(
    session: Session, limit: int = MAX_REPROCESS_BATCH
) -> list[Document]:
    return list(
        session.scalars(
            select(Document)
            .where(Document.status == DocumentStatus.FAILED, Document.is_retryable)
            .order_by(Document.created_at)
            .limit(limit)
        )
    )


def stalled_documents(session: Session, settings: Settings) -> list[Document]:
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.stalled_after_seconds)
    return list(
        session.scalars(
            select(Document)
            .where(Document.status == DocumentStatus.PROCESSING, Document.updated_at < cutoff)
            .limit(MAX_REPROCESS_BATCH)
        )
    )
