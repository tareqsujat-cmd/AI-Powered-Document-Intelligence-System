import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session, defer, selectinload

from app.api.deps import SessionDep, SettingsDep, get_document_or_404, results_with_details
from app.api.rate_limit import limit_requests, limit_uploads
from app.api.schemas import (
    DocumentDetail,
    DocumentFilters,
    DocumentList,
    DocumentStatusBody,
    DocumentSummary,
    HistoryOut,
    PageOut,
    ReprocessFailedResponse,
    ResultOut,
    UploadError,
    UploadOutcome,
    UploadResponse,
)
from app.core.config import Settings
from app.db.enums import DocumentStatus, FileType, ProcessingEvent, ProcessingStage
from app.db.models import (
    Document,
    DocumentPage,
    ExtractionResult,
    ProcessingHistory,
    ValidationError,
)
from app.extraction.normalize import normalize_key
from app.pipeline.service import (
    enqueue_processing,
    is_busy,
    prepare_reprocess,
    record_event,
    retryable_failed_documents,
)
from app.processing.errors import FileTooLargeError, ProcessingError, UnsupportedFileError
from app.processing.file_validation import (
    EXTENSION_TYPES,
    MIME_TYPES,
    ValidatedFile,
    sanitize_filename,
    validate_file,
)

logger = logging.getLogger(__name__)

# Two routers, so that no route can be added without one of the two rate limits.
upload_router = APIRouter(tags=["documents"], dependencies=[Depends(limit_uploads)])
router = APIRouter(tags=["documents"], dependencies=[Depends(limit_requests)])

_CHUNK_BYTES = 1024 * 1024
_MAX_HISTORY_ENTRIES = 1000
_MAX_PAGE_NUMBER = 2**31 - 1
_MAX_REPORTED_FILENAME = 255
_INLINE_TYPES = frozenset({FileType.PDF, FileType.JPEG, FileType.PNG})
_API_ACTOR = "api"


def _extension(filename: str) -> str:
    """A suffix for the stored file, only ever one from the allowed list."""
    suffix = PurePosixPath(filename).suffix.lower()
    return suffix if suffix in EXTENSION_TYPES else ""


def _type_from_extension(filename: str) -> FileType:
    """For a file whose content could not be read. validate_file has already checked the suffix."""
    return EXTENSION_TYPES[_extension(filename)]


def _store_upload(upload: UploadFile, filename: str, settings: Settings) -> tuple[Path, str]:
    """Copy an upload to storage, returning its path and SHA-256. Leaves nothing on any failure."""
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    path = settings.storage_dir / f"{uuid.uuid4()}{_extension(filename)}"
    digest = hashlib.sha256()
    written = 0
    try:
        with path.open("xb") as target:
            while chunk := upload.file.read(_CHUNK_BYTES):
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise FileTooLargeError(
                        f"The file exceeds the {settings.max_upload_mb} MB upload limit."
                    )
                digest.update(chunk)
                target.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path, digest.hexdigest()


def _save_new_document(session: Session, document: Document, event: ProcessingEvent) -> None:
    """Commit a new document and its first history entry, or delete its file if that fails."""
    try:
        session.add(document)
        session.flush()
        record_event(
            session,
            document,
            event,
            stage=document.stage,
            message=document.failure_reason,
            actor=_API_ACTOR,
        )
        session.commit()
    except Exception:
        session.rollback()
        Path(document.storage_path).unlink(missing_ok=True)
        raise


def _record_unreadable_file(
    session: Session, path: Path, filename: str, sha256: str, error: ProcessingError
) -> Document:
    """An empty, damaged or locked file stays on record as failed, so it can be seen and retried."""
    file_type = _type_from_extension(filename)
    document = Document(
        original_filename=filename,
        storage_path=str(path),
        sha256=sha256,
        file_type=file_type,
        mime_type=MIME_TYPES[file_type],
        size_bytes=path.stat().st_size,
        status=DocumentStatus.FAILED,
        stage=ProcessingStage.FILE_VALIDATION,
        failure_reason=error.reason,
        is_retryable=True,
        processing_finished_at=datetime.now(UTC),
    )
    _save_new_document(session, document, ProcessingEvent.STAGE_FAILED)
    return document


def _record_valid_file(session: Session, path: Path, validated: ValidatedFile) -> Document:
    document = Document(
        original_filename=validated.safe_filename,
        storage_path=str(path),
        sha256=validated.sha256,
        file_type=validated.file_type,
        mime_type=validated.mime_type,
        size_bytes=validated.size_bytes,
        status=DocumentStatus.QUEUED,
        stage=ProcessingStage.UPLOAD,
    )
    _save_new_document(session, document, ProcessingEvent.UPLOADED)
    return document


def _accept_upload(session: Session, upload: UploadFile, settings: Settings) -> UploadOutcome:
    filename = sanitize_filename(upload.filename or "")
    reported_name = filename[:_MAX_REPORTED_FILENAME]

    def rejected(code: str, reason: str) -> UploadOutcome:
        error = UploadError(code=code, reason=reason)
        return UploadOutcome(filename=reported_name, accepted=False, error=error)

    try:
        path, sha256 = _store_upload(upload, filename, settings)
    except FileTooLargeError as exc:
        return rejected(exc.code, exc.reason)
    except OSError:
        logger.exception("could not store an upload")
        return rejected("storage_error", "The file could not be stored. Try again shortly.")

    try:
        validated = validate_file(path, filename, settings)
    except (UnsupportedFileError, FileTooLargeError) as exc:
        path.unlink(missing_ok=True)
        return rejected(exc.code, exc.reason)
    except ProcessingError as exc:
        document = _record_unreadable_file(session, path, filename, sha256, exc)
        return UploadOutcome(
            filename=reported_name,
            accepted=True,
            document=DocumentSummary.of(document),
            error=UploadError(code=exc.code, reason=exc.reason),
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise

    document = _record_valid_file(session, path, validated)
    error = None
    if not enqueue_processing(session, document):
        error = UploadError(code="queue_unavailable", reason=document.failure_reason or "")
    return UploadOutcome(
        filename=reported_name,
        accepted=True,
        document=DocumentSummary.of(document),
        error=error,
    )


@upload_router.post(
    "/documents", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED
)
def upload_documents(
    files: Annotated[list[UploadFile], File()], session: SessionDep, settings: SettingsDep
) -> UploadResponse:
    """Each file gets its own outcome, so that one bad file does not fail the batch."""
    if len(files) > settings.max_files_per_upload:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"At most {settings.max_files_per_upload} files can be uploaded at once.",
        )
    return UploadResponse(results=[_accept_upload(session, file, settings) for file in files])


def _list_conditions(filters: DocumentFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if filters.status is not None:
        conditions.append(Document.status == filters.status)
    if filters.needs_review is not None:
        conditions.append(Document.needs_review.is_(filters.needs_review))
    if filters.file_type is not None:
        conditions.append(Document.file_type == filters.file_type)
    if filters.filename is not None:
        conditions.append(Document.original_filename.icontains(filters.filename, autoescape=True))

    invoice_conditions: list[ColumnElement[bool]] = []
    if filters.supplier is not None:
        invoice_conditions.append(
            ExtractionResult.supplier_name.icontains(filters.supplier, autoescape=True)
        )
    if filters.invoice_number is not None:
        invoice_conditions.append(
            ExtractionResult.invoice_key == normalize_key(filters.invoice_number)
        )
    if filters.date_from is not None:
        invoice_conditions.append(ExtractionResult.invoice_date >= filters.date_from)
    if filters.date_to is not None:
        invoice_conditions.append(ExtractionResult.invoice_date <= filters.date_to)
    if filters.total_min is not None:
        invoice_conditions.append(ExtractionResult.total >= filters.total_min)
    if filters.total_max is not None:
        invoice_conditions.append(ExtractionResult.total <= filters.total_max)
    if invoice_conditions:
        # EXISTS rather than a join: a file with many matching invoices is still one document. All
        # conditions must hold for one invoice, not for different invoices in the same file.
        conditions.append(
            select(ExtractionResult.id)
            .where(
                ExtractionResult.document_id == Document.id,
                ExtractionResult.is_current,
                *invoice_conditions,
            )
            .exists()
        )
    return conditions


def _invoice_counts(session: Session, document_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    rows = session.execute(
        select(ExtractionResult.document_id, func.count())
        .where(ExtractionResult.document_id.in_(document_ids), ExtractionResult.is_current)
        .group_by(ExtractionResult.document_id)
    )
    return dict(rows.all())


@router.get("/documents", response_model=DocumentList)
def list_documents(
    filters: Annotated[DocumentFilters, Query()], session: SessionDep
) -> DocumentList:
    conditions = _list_conditions(filters)
    total = session.scalar(select(func.count()).select_from(Document).where(*conditions)) or 0
    documents = list(
        session.scalars(
            select(Document)
            .where(*conditions)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(filters.limit)
            .offset(filters.offset)
        )
    )
    counts = _invoice_counts(session, [document.id for document in documents])
    return DocumentList(
        items=[DocumentSummary.of(document, counts.get(document.id, 0)) for document in documents],
        total=total,
        limit=filters.limit,
        offset=filters.offset,
    )


# Declared before the routes that take a document id.
@router.post(
    "/documents/reprocess-failed",
    response_model=ReprocessFailedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def reprocess_failed_documents(session: SessionDep) -> ReprocessFailedResponse:
    """Stops at the first queue failure: the queue is down, so trying the rest would only be slow,
    and would replace each document's real failure reason with one about the queue."""
    documents = retryable_failed_documents(session)
    queued = 0
    for document in documents:
        prepare_reprocess(session, document, resume=True, actor=_API_ACTOR)
        if not enqueue_processing(session, document):
            break
        queued += 1
    return ReprocessFailedResponse(queued=queued, failed_to_queue=len(documents) - queued)


@router.get("/documents/{document_id}", response_model=DocumentDetail)
def get_document(document_id: uuid.UUID, session: SessionDep) -> DocumentDetail:
    document = get_document_or_404(session, document_id)
    invoice_count = _invoice_counts(session, [document.id]).get(document.id, 0)
    stored_page_count = session.scalar(
        select(func.count())
        .select_from(DocumentPage)
        .where(DocumentPage.document_id == document.id)
    )
    validation_error_count = session.scalar(
        select(func.count())
        .select_from(ValidationError)
        .join(ExtractionResult, ValidationError.extraction_result_id == ExtractionResult.id)
        .where(ValidationError.document_id == document.id, ExtractionResult.is_current)
    )
    return DocumentDetail.of(
        document,
        invoice_count,
        stored_page_count=stored_page_count,
        validation_error_count=validation_error_count,
    )


@router.get("/documents/{document_id}/status", response_model=DocumentStatusBody)
def get_document_status(document_id: uuid.UUID, session: SessionDep) -> DocumentStatusBody:
    return DocumentStatusBody.of(get_document_or_404(session, document_id))


@router.get("/documents/{document_id}/results", response_model=list[ResultOut])
def get_document_results(document_id: uuid.UUID, session: SessionDep) -> list[ResultOut]:
    get_document_or_404(session, document_id)
    results = session.scalars(
        results_with_details()
        .where(ExtractionResult.document_id == document_id, ExtractionResult.is_current)
        .order_by(ExtractionResult.invoice_index)
    )
    return [ResultOut.of(result) for result in results]


@router.get("/documents/{document_id}/pages/{page_number}", response_model=PageOut)
def get_document_page(
    document_id: uuid.UUID,
    page_number: Annotated[int, PathParam(ge=1, le=_MAX_PAGE_NUMBER)],
    session: SessionDep,
) -> PageOut:
    page = session.scalar(
        select(DocumentPage)
        .options(defer(DocumentPage.table_data), selectinload(DocumentPage.text))
        .where(DocumentPage.document_id == document_id, DocumentPage.page_number == page_number)
    )
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found.")
    return PageOut.of(page)


@router.get("/documents/{document_id}/history", response_model=list[HistoryOut])
def get_document_history(document_id: uuid.UUID, session: SessionDep) -> list[HistoryOut]:
    get_document_or_404(session, document_id)
    newest = session.scalars(
        select(ProcessingHistory)
        .where(ProcessingHistory.document_id == document_id)
        .order_by(ProcessingHistory.id.desc())
        .limit(_MAX_HISTORY_ENTRIES)
    ).all()
    return [HistoryOut.of(entry) for entry in reversed(newest)]


@router.get("/documents/{document_id}/file", response_class=FileResponse)
def get_document_file(
    document_id: uuid.UUID, session: SessionDep, settings: SettingsDep
) -> FileResponse:
    document = get_document_or_404(session, document_id)
    path = Path(document.storage_path).resolve()
    if not path.is_relative_to(settings.storage_dir.resolve()) or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The file is no longer available.")
    return FileResponse(
        path,
        media_type=document.mime_type,
        filename=document.original_filename,
        content_disposition_type="inline" if document.file_type in _INLINE_TYPES else "attachment",
        # Sandboxed, so that a hostile file opened directly cannot run script in this origin.
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "no-store",
        },
    )


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=DocumentStatusBody,
    status_code=status.HTTP_202_ACCEPTED,
)
def reprocess_document(
    document_id: uuid.UUID, session: SessionDep, settings: SettingsDep, resume: bool = False
) -> DocumentStatusBody:
    # Locked, so that two simultaneous requests cannot both see the document as idle.
    document = get_document_or_404(session, document_id, lock=True)
    if is_busy(document, settings):
        raise HTTPException(status.HTTP_409_CONFLICT, "The document is already being processed.")
    prepare_reprocess(session, document, resume=resume, actor=_API_ACTOR)
    if not enqueue_processing(session, document):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            document.failure_reason or "The processing queue is unavailable.",
        )
    return DocumentStatusBody.of(document)
