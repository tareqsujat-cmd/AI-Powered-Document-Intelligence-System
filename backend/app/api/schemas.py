import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.db.enums import (
    DocumentStatus,
    ExtractionMethod,
    FileType,
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
from app.extraction.schemas import INVOICE_FIELDS
from app.validation.rules import format_amount

# Fits NUMERIC(18,4): 14 integer digits and 4 decimals.
_MONEY_DIGITS = 18
_MONEY_DECIMALS = 4
MAX_LIST_OFFSET = 1_000_000_000
MAX_LINE_ITEMS = 10_000
_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


def plain_value(value: object) -> str | None:
    """A stored value as text. Money stays exact, which a JSON number could not guarantee."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format_amount(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _storable_text(value: str) -> str:
    # Postgres text cannot hold a NUL character.
    if "\x00" in value:
        raise ValueError("Text must not contain a NUL character.")
    return value


class UploadError(BaseModel):
    code: str
    reason: str


class DocumentSummary(BaseModel):
    id: uuid.UUID
    filename: str
    file_type: FileType
    size_bytes: int
    page_count: int | None
    status: DocumentStatus
    stage: ProcessingStage
    progress: int
    needs_review: bool
    failure_reason: str | None
    is_retryable: bool
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    processing_started_at: datetime | None
    processing_finished_at: datetime | None
    invoice_count: int

    @classmethod
    def of(cls, document: Document, invoice_count: int = 0, **extra: Any) -> Self:
        return cls(
            id=document.id,
            filename=document.original_filename,
            file_type=document.file_type,
            size_bytes=document.size_bytes,
            page_count=document.page_count,
            status=document.status,
            stage=document.stage,
            progress=document.progress,
            needs_review=document.needs_review,
            failure_reason=document.failure_reason,
            is_retryable=document.is_retryable,
            attempt_count=document.attempt_count,
            created_at=document.created_at,
            updated_at=document.updated_at,
            processing_started_at=document.processing_started_at,
            processing_finished_at=document.processing_finished_at,
            invoice_count=invoice_count,
            **extra,
        )


class DocumentDetail(DocumentSummary):
    stored_page_count: int
    validation_error_count: int


class UploadOutcome(BaseModel):
    filename: str
    accepted: bool
    document: DocumentSummary | None = None
    error: UploadError | None = None


class UploadResponse(BaseModel):
    results: list[UploadOutcome]


_FilterText = Annotated[str, StringConstraints(max_length=512), AfterValidator(_storable_text)]
_FilterMoney = Annotated[
    Decimal, Field(max_digits=_MONEY_DIGITS, decimal_places=_MONEY_DECIMALS, allow_inf_nan=False)
]


class DocumentFilters(BaseModel):
    status: DocumentStatus | None = None
    needs_review: bool | None = None
    file_type: FileType | None = None
    filename: _FilterText | None = None
    supplier: _FilterText | None = None
    invoice_number: _FilterText | None = None
    date_from: date | None = None
    date_to: date | None = None
    total_min: _FilterMoney | None = None
    total_max: _FilterMoney | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=MAX_LIST_OFFSET)


class DocumentList(BaseModel):
    items: list[DocumentSummary]
    total: int
    limit: int
    offset: int


class DocumentStatusBody(BaseModel):
    id: uuid.UUID
    status: DocumentStatus
    stage: ProcessingStage
    progress: int
    needs_review: bool
    failure_reason: str | None
    is_retryable: bool
    updated_at: datetime

    @classmethod
    def of(cls, document: Document) -> "DocumentStatusBody":
        return cls(
            id=document.id,
            status=document.status,
            stage=document.stage,
            progress=document.progress,
            needs_review=document.needs_review,
            failure_reason=document.failure_reason,
            is_retryable=document.is_retryable,
            updated_at=document.updated_at,
        )


class ReprocessFailedResponse(BaseModel):
    queued: int
    failed_to_queue: int


class FieldOut(BaseModel):
    value: str | None
    confidence: float
    reason: str | None
    source_snippet: str | None
    source_page: int | None


class LineItemOut(BaseModel):
    id: uuid.UUID
    position: int
    description: str | None
    quantity: str | None
    unit_price: str | None
    amount: str | None
    confidence: float
    source_snippet: str | None
    source_page: int | None


class ValidationErrorOut(BaseModel):
    id: uuid.UUID
    code: str
    severity: Severity
    field: str | None
    message: str
    expected_value: str | None
    actual_value: str | None
    related_document_id: uuid.UUID | None
    is_resolved: bool

    @classmethod
    def of(cls, error: ValidationError) -> "ValidationErrorOut":
        return cls(
            id=error.id,
            code=error.code,
            severity=error.severity,
            field=error.field,
            message=error.message,
            expected_value=error.expected_value,
            actual_value=error.actual_value,
            related_document_id=error.related_document_id,
            is_resolved=error.is_resolved,
        )


class ResultOut(BaseModel):
    id: uuid.UUID
    invoice_index: int
    version: int
    page_start: int | None
    page_end: int | None
    method: ExtractionMethod
    overall_confidence: float | None
    warnings: list[str]
    llm_model: str | None
    prompt_version: str | None
    fields: dict[str, FieldOut]
    line_items: list[LineItemOut]
    validation_errors: list[ValidationErrorOut]

    @classmethod
    def of(cls, result: ExtractionResult) -> "ResultOut":
        fields = {}
        for name in INVOICE_FIELDS:
            evidence = result.field_metadata.get(name, {})
            fields[name] = FieldOut(
                value=plain_value(getattr(result, name)),
                confidence=evidence.get("confidence", 0.0),
                reason=evidence.get("reason"),
                source_snippet=evidence.get("source_snippet"),
                source_page=evidence.get("source_page"),
            )
        return cls(
            id=result.id,
            invoice_index=result.invoice_index,
            version=result.version,
            page_start=result.page_start,
            page_end=result.page_end,
            method=result.method,
            overall_confidence=result.overall_confidence,
            warnings=result.warnings,
            llm_model=result.llm_model,
            prompt_version=result.prompt_version,
            fields=fields,
            line_items=[
                LineItemOut(
                    id=item.id,
                    position=item.position,
                    description=item.description,
                    quantity=plain_value(item.quantity),
                    unit_price=plain_value(item.unit_price),
                    amount=plain_value(item.amount),
                    confidence=item.field_metadata.get("confidence", 0.0),
                    source_snippet=item.field_metadata.get("source_snippet"),
                    source_page=item.field_metadata.get("source_page"),
                )
                for item in result.line_items
            ],
            validation_errors=[
                ValidationErrorOut.of(error)
                for error in sorted(
                    result.validation_errors,
                    key=lambda error: (
                        _SEVERITY_ORDER[error.severity],
                        error.code,
                        error.field or "",
                    ),
                )
            ],
        )


class PageOut(BaseModel):
    page_number: int
    sheet_name: str | None
    source: TextSource | None
    status: PageStatus
    ocr_confidence: float | None
    processing_ms: int | None
    error: str | None
    warnings: list[str]
    text: str | None

    @classmethod
    def of(cls, page: DocumentPage) -> "PageOut":
        return cls(
            page_number=page.page_number,
            sheet_name=page.sheet_name,
            source=page.source,
            status=page.status,
            ocr_confidence=page.ocr_confidence,
            processing_ms=page.processing_ms,
            error=page.error,
            warnings=page.warnings,
            text=page.text.content if page.text else None,
        )


class HistoryOut(BaseModel):
    id: int
    event: ProcessingEvent
    stage: ProcessingStage | None
    attempt: int | None
    duration_ms: int | None
    message: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    actor: str
    created_at: datetime

    @classmethod
    def of(cls, entry: ProcessingHistory) -> "HistoryOut":
        return cls(
            id=entry.id,
            event=entry.event,
            stage=entry.stage,
            attempt=entry.attempt,
            duration_ms=entry.duration_ms,
            message=entry.message,
            before=entry.before,
            after=entry.after,
            actor=entry.actor,
            created_at=entry.created_at,
        )


def _date_text(value: object) -> object:
    # Pydantic would otherwise read a bare number as a Unix timestamp.
    if not isinstance(value, str):
        raise ValueError("Expected an ISO date string, such as 2026-03-15.")
    return value


_Money = Annotated[
    Decimal,
    Field(max_digits=_MONEY_DIGITS, decimal_places=_MONEY_DECIMALS, allow_inf_nan=False),
]
_InvoiceDate = Annotated[date, BeforeValidator(_date_text)]
_InvoiceNumber = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    AfterValidator(_storable_text),
]
_PartyName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
    AfterValidator(_storable_text),
]
_Description = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
    AfterValidator(_storable_text),
]
_Actor = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    AfterValidator(_storable_text),
]


class LineItemCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: _Description | None = None
    quantity: _Money | None = None
    unit_price: _Money | None = None
    amount: _Money | None = None


class CorrectionRequest(BaseModel):
    """Which fields are present matters: an absent field stays as it is, and null clears it."""

    model_config = ConfigDict(extra="forbid")

    invoice_number: _InvoiceNumber | None = None
    invoice_date: _InvoiceDate | None = None
    supplier_name: _PartyName | None = None
    customer_name: _PartyName | None = None
    subtotal: _Money | None = None
    tax: _Money | None = None
    total: _Money | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    line_items: list[LineItemCorrection] = Field(default_factory=list, max_length=MAX_LINE_ITEMS)
    actor: _Actor = "reviewer"

    @model_validator(mode="after")
    def _has_a_change(self) -> "CorrectionRequest":
        if not self.model_fields_set - {"actor"}:
            raise ValueError("Provide at least one field to correct.")
        return self


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_resolved: bool
    actor: _Actor = "reviewer"


class ReviewResponse(BaseModel):
    validation_error: ValidationErrorOut
    needs_review: bool


class ProcessingTimes(BaseModel):
    mean_seconds: float | None
    p95_seconds: float | None


class MetricsResponse(BaseModel):
    documents_total: int
    documents_by_status: dict[str, int]
    needs_review: int
    documents_processed: int
    pages_processed: int
    pages_by_source: dict[str, int]
    processing_time: ProcessingTimes
    mean_ms_per_page: float | None
    success_rate: float | None
    failure_rate: float | None
    validation_issues_by_code: dict[str, int]
    validation_issues_by_severity: dict[str, int]
    invoices_by_method: dict[str, int]
    mean_seconds_by_stage: dict[str, float]
