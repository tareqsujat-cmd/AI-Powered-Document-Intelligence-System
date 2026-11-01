from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import Float, Select, cast, extract, func, select
from sqlalchemy.orm import Session

from app.api.deps import SessionDep
from app.api.rate_limit import limit_requests
from app.api.schemas import MetricsResponse, ProcessingTimes
from app.db.enums import (
    DocumentStatus,
    ExtractionMethod,
    ProcessingEvent,
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

router = APIRouter(tags=["metrics"], dependencies=[Depends(limit_requests)])


def _counts(session: Session, query: Select[Any, int]) -> dict[str, int]:
    return {str(key): count for key, count in session.execute(query)}


def _with_zeros(counts: dict[str, int], members: type[StrEnum]) -> dict[str, int]:
    return {member.value: counts.get(member.value, 0) for member in members}


def _current_issue_counts(session: Session, column: Any) -> dict[str, int]:
    return _counts(
        session,
        select(column, func.count())
        .select_from(ValidationError)
        .join(ExtractionResult, ValidationError.extraction_result_id == ExtractionResult.id)
        .where(ExtractionResult.is_current)
        .group_by(column),
    )


def _processing_times(session: Session) -> ProcessingTimes:
    seconds = cast(
        extract("epoch", Document.processing_finished_at - Document.processing_started_at), Float
    )
    mean, p95 = session.execute(
        select(func.avg(seconds), func.percentile_cont(0.95).within_group(seconds)).where(
            Document.status == DocumentStatus.COMPLETED
        )
    ).one()
    return ProcessingTimes(mean_seconds=mean, p95_seconds=p95)


@router.get("/metrics", response_model=MetricsResponse)
def get_metrics(session: SessionDep) -> MetricsResponse:
    by_status = _with_zeros(
        _counts(session, select(Document.status, func.count()).group_by(Document.status)),
        DocumentStatus,
    )
    completed, failed = by_status[DocumentStatus.COMPLETED], by_status[DocumentStatus.FAILED]
    finished = completed + failed

    pages_processed, mean_ms_per_page = session.execute(
        select(func.count(), cast(func.avg(DocumentPage.processing_ms), Float)).select_from(
            DocumentPage
        )
    ).one()
    pages_by_source = _counts(
        session,
        select(DocumentPage.source, func.count()).group_by(DocumentPage.source),
    )
    stage_rows = session.execute(
        select(ProcessingHistory.stage, cast(func.avg(ProcessingHistory.duration_ms), Float))
        .where(
            ProcessingHistory.event == ProcessingEvent.STAGE_COMPLETED,
            ProcessingHistory.stage.is_not(None),
            ProcessingHistory.duration_ms.is_not(None),
        )
        .group_by(ProcessingHistory.stage)
    )

    return MetricsResponse(
        documents_total=sum(by_status.values()),
        documents_by_status=by_status,
        needs_review=session.scalar(
            select(func.count()).select_from(Document).where(Document.needs_review)
        )
        or 0,
        documents_processed=completed,
        pages_processed=pages_processed,
        pages_by_source=_with_zeros(pages_by_source, TextSource),
        processing_time=_processing_times(session),
        mean_ms_per_page=mean_ms_per_page,
        success_rate=completed / finished if finished else None,
        failure_rate=failed / finished if finished else None,
        validation_issues_by_code=_current_issue_counts(session, ValidationError.code),
        validation_issues_by_severity=_with_zeros(
            _current_issue_counts(session, ValidationError.severity), Severity
        ),
        invoices_by_method=_with_zeros(
            _counts(
                session,
                select(ExtractionResult.method, func.count())
                .where(ExtractionResult.is_current)
                .group_by(ExtractionResult.method),
            ),
            ExtractionMethod,
        ),
        mean_seconds_by_stage={str(stage): mean_ms / 1000 for stage, mean_ms in stage_rows},
    )
