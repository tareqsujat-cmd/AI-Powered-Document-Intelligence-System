from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.enums import DocumentStatus, ProcessingEvent
from app.db.models import Document, ExtractionResult, ProcessingHistory
from app.extraction.errors import LLMNotConfiguredError, LLMRejectedError, LLMUnavailableError
from app.worker import tasks
from app.worker.celery_app import build_celery_config, celery_app
from tests.pipeline_helpers import FakeEmbedder, FakeLLM, make_document, make_pdf

pytestmark = pytest.mark.db


@pytest.fixture
def wired(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """Point the task at the test database and fake models, and stop it contacting a broker."""
    settings = Settings(
        _env_file=None,
        task_max_retries=2,
        task_retry_delay_seconds=10,
        stalled_after_seconds=60,
        storage_dir=tmp_path,
    )
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    monkeypatch.setattr(tasks, "get_session_factory", lambda: factory)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "get_embedder", FakeEmbedder)
    return settings


def set_llm(monkeypatch: pytest.MonkeyPatch, llm: FakeLLM) -> None:
    monkeypatch.setattr(tasks, "_llm_client_or_none", lambda: llm)


def run_task(document: Document, *, retries: int = 0) -> Any:
    """Run the task body in this process, as a worker holding this delivery would."""
    task = tasks.process_document_task
    task.push_request(id=document.task_id, retries=retries, called_directly=False)
    try:
        return task.run(str(document.id))
    finally:
        task.pop_request()


def fresh(db: Session, document: Document) -> Document:
    db.expire_all()
    refreshed = db.get(Document, document.id)
    assert refreshed is not None
    return refreshed


def events(db: Session, document: Document) -> list[ProcessingEvent]:
    return list(
        db.scalars(
            select(ProcessingHistory.event)
            .where(ProcessingHistory.document_id == document.id)
            .order_by(ProcessingHistory.id)
        )
    )


def test_the_tasks_are_registered() -> None:
    assert "documents.process" in celery_app.tasks
    assert "documents.recover_stalled" in celery_app.tasks


def test_recovery_runs_on_a_schedule() -> None:
    schedule = build_celery_config(Settings(_env_file=None))["beat_schedule"]

    assert schedule["recover-stalled-documents"]["task"] == "documents.recover_stalled"


def test_a_clean_run_completes(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="t-1")
    set_llm(monkeypatch, FakeLLM())

    assert run_task(document) == "completed"

    assert fresh(db, document).status is DocumentStatus.COMPLETED
    assert db.scalars(select(ExtractionResult)).one().total == Decimal("1100.0000")


def test_a_transient_failure_schedules_a_retry_with_backoff(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="t-1")
    set_llm(monkeypatch, FakeLLM(LLMUnavailableError("The API is unavailable.")))
    retries: list[dict[str, Any]] = []

    def fake_retry(**kwargs: Any) -> Retry:
        retries.append(kwargs)
        return Retry("scheduled")

    monkeypatch.setattr(tasks.process_document_task, "retry", fake_retry)

    with pytest.raises(Retry):
        run_task(document, retries=1)

    assert retries[0]["countdown"] == 20
    assert retries[0]["max_retries"] == 2
    refreshed = fresh(db, document)
    assert refreshed.status is DocumentStatus.PROCESSING
    assert ProcessingEvent.RETRY_SCHEDULED in events(db, document)


def test_retries_exhausted_fails_with_the_reason(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="t-1")
    set_llm(monkeypatch, FakeLLM(LLMUnavailableError("The API is unavailable.")))

    assert run_task(document, retries=2) == "failed"

    refreshed = fresh(db, document)
    assert refreshed.status is DocumentStatus.FAILED
    assert refreshed.failure_reason == "The API is unavailable."
    assert refreshed.is_retryable is True


def test_a_permanent_failure_does_not_retry(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="t-1")
    set_llm(monkeypatch, FakeLLM(LLMRejectedError("The API rejected the request.")))

    def no_retry(**_kwargs: Any) -> None:
        raise AssertionError("a permanent failure must not retry")

    monkeypatch.setattr(tasks.process_document_task, "retry", no_retry)

    assert run_task(document) == "failed"

    assert fresh(db, document).failure_reason == "The API rejected the request."


def test_a_corrupted_file_fails_with_its_reason(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.7\nnot really a pdf")
    document = make_document(db, path, task_id="t-1")
    set_llm(monkeypatch, FakeLLM())

    assert run_task(document) == "failed"

    refreshed = fresh(db, document)
    assert refreshed.status is DocumentStatus.FAILED
    assert refreshed.failure_reason
    assert str(tmp_path) not in refreshed.failure_reason


def test_a_crash_reports_a_safe_message(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="t-1")

    def crash(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("password=hunter2 at /internal/path")

    monkeypatch.setattr(tasks, "process_document", crash)

    assert run_task(document) == "failed"

    refreshed = fresh(db, document)
    assert refreshed.failure_reason == tasks.UNEXPECTED_FAILURE
    assert "hunter2" not in refreshed.failure_reason


def test_running_out_of_time_fails_retryably(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="t-1")

    def too_slow(*_a: Any, **_k: Any) -> None:
        raise SoftTimeLimitExceeded

    monkeypatch.setattr(tasks, "process_document", too_slow)

    assert run_task(document) == "failed"

    refreshed = fresh(db, document)
    assert refreshed.failure_reason == tasks.TIMED_OUT
    assert refreshed.is_retryable is True


def test_a_stale_delivery_changes_nothing(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="current")
    llm = FakeLLM()
    set_llm(monkeypatch, llm)
    task = tasks.process_document_task
    task.push_request(id="stale", retries=0, called_directly=False)
    try:
        assert task.run(str(document.id)) == "skipped"
    finally:
        task.pop_request()

    assert llm.calls == 0
    assert fresh(db, document).status is DocumentStatus.QUEUED


def test_a_missing_key_only_matters_for_documents_that_need_the_llm(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = make_document(db, make_pdf(tmp_path / "a.pdf"), task_id="t-1")
    monkeypatch.setattr(tasks, "_llm_client_or_none", lambda: None)

    assert run_task(document) == "failed"

    assert "LLM_API_KEY" in fresh(db, document).failure_reason


def test_llm_client_factory_returns_none_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_key() -> None:
        raise LLMNotConfiguredError("LLM_API_KEY is not set.")

    monkeypatch.setattr(tasks, "_cached_llm_client", no_key)

    assert tasks._llm_client_or_none() is None


def test_recovery_hands_only_stalled_documents_to_a_new_task(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_pdf(tmp_path / "a.pdf")
    old = datetime.now(UTC) - timedelta(minutes=10)
    stalled = make_document(
        db, path, status=DocumentStatus.PROCESSING, updated_at=old, task_id="dead-task"
    )
    active = make_document(db, path, status=DocumentStatus.PROCESSING, task_id="live-task")
    sent: list[str] = []
    monkeypatch.setattr(
        "app.pipeline.service.celery_app.send_task",
        lambda _name, **kwargs: sent.append(kwargs["args"][0]),
    )

    assert tasks.recover_stalled_documents() == 1

    assert sent == [str(stalled.id)]
    refreshed = fresh(db, stalled)
    assert refreshed.status is DocumentStatus.QUEUED
    assert refreshed.task_id != "dead-task"
    assert fresh(db, active).task_id == "live-task"
    assert ProcessingEvent.REPROCESS_REQUESTED in events(db, stalled)


def test_after_recovery_the_dead_tasks_redelivery_is_ignored(
    db: Session, tmp_path: Path, wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime.now(UTC) - timedelta(minutes=10)
    document = make_document(
        db,
        make_pdf(tmp_path / "a.pdf"),
        status=DocumentStatus.PROCESSING,
        updated_at=old,
        task_id="dead-task",
    )
    monkeypatch.setattr("app.pipeline.service.celery_app.send_task", lambda *_a, **_k: None)
    tasks.recover_stalled_documents()
    llm = FakeLLM()
    set_llm(monkeypatch, llm)
    task = tasks.process_document_task
    task.push_request(id="dead-task", retries=0, called_directly=False)
    try:
        assert task.run(str(document.id)) == "skipped"
    finally:
        task.pop_request()

    assert llm.calls == 0
