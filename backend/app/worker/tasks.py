import logging
import uuid
from functools import lru_cache
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from redis import Redis

from app.core.config import get_settings
from app.db.enums import ProcessingEvent
from app.db.models import Document
from app.db.session import get_session_factory
from app.extraction.errors import LLMNotConfiguredError
from app.extraction.llm_client import GeminiClient, OpenAICompatibleClient, create_llm_client
from app.extraction.pacing import RedisPacer
from app.pipeline.processor import TaskSuperseded, fail_document, process_document
from app.pipeline.service import (
    PROCESS_TASK_NAME,
    enqueue_processing,
    prepare_reprocess,
    record_event,
    stalled_documents,
)
from app.processing.errors import ProcessingError
from app.search.embeddings import get_embedder
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

UNEXPECTED_FAILURE = "Processing failed because of an unexpected error. It can be retried."
TIMED_OUT = "Processing took longer than the time limit. It can be retried."


@celery_app.task(name="system.ping")
def ping() -> str:
    """Confirms a worker can receive and run a task."""
    return "pong"


@lru_cache
def _cached_llm_client() -> GeminiClient | OpenAICompatibleClient:
    settings = get_settings()
    pacer = None
    if settings.llm_requests_per_minute:
        redis_client = Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        pacer = RedisPacer(redis_client, settings.llm_requests_per_minute)
    return create_llm_client(settings, pacer=pacer)


def _llm_client_or_none() -> GeminiClient | OpenAICompatibleClient | None:
    """Spreadsheets with recognizable columns work without a key, so a missing key is not fatal."""
    try:
        return _cached_llm_client()
    except LLMNotConfiguredError:
        return None


@celery_app.task(bind=True, name=PROCESS_TASK_NAME)
def process_document_task(self: Any, document_id: str) -> str:
    settings = get_settings()
    identifier = uuid.UUID(document_id)
    with get_session_factory()() as session:
        try:
            ran = process_document(
                session,
                identifier,
                settings=settings,
                get_llm_client=_llm_client_or_none,
                embedder=get_embedder(),
                task_id=self.request.id,
            )
            return "completed" if ran else "skipped"
        except TaskSuperseded:
            session.rollback()
            logger.info("task superseded", extra={"document_id": document_id})
            return "superseded"
        except ProcessingError as exc:
            if exc.transient and self.request.retries < settings.task_max_retries:
                session.rollback()
                document = session.get(Document, identifier)
                if document is not None:
                    record_event(
                        session,
                        document,
                        ProcessingEvent.RETRY_SCHEDULED,
                        stage=document.stage,
                        message=exc.reason,
                    )
                    session.commit()
                delay = settings.task_retry_delay_seconds * 2**self.request.retries
                raise self.retry(
                    exc=exc, countdown=delay, max_retries=settings.task_max_retries
                ) from exc
            fail_document(session, identifier, exc.reason, task_id=self.request.id)
            return "failed"
        except SoftTimeLimitExceeded:
            fail_document(session, identifier, TIMED_OUT, task_id=self.request.id)
            return "failed"
        except Exception:
            logger.exception("processing crashed", extra={"document_id": document_id})
            fail_document(session, identifier, UNEXPECTED_FAILURE, task_id=self.request.id)
            return "failed"


@celery_app.task(name="documents.recover_stalled")
def recover_stalled_documents() -> int:
    """Give documents whose worker died to a new task, which resumes from the stored work.

    A task whose worker is killed outright only returns to the queue after Redis's visibility
    timeout, which is hours. Replacing the task id also stops any stale copy of the old one.
    """
    settings = get_settings()
    recovered = 0
    with get_session_factory()() as session:
        for document in stalled_documents(session, settings):
            logger.warning("recovering stalled document", extra={"document_id": str(document.id)})
            prepare_reprocess(session, document, resume=True)
            if enqueue_processing(session, document):
                recovered += 1
    return recovered
