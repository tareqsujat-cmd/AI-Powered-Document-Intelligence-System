import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
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

EMBEDDING_DIMENSIONS = 384  # all-MiniLM-L6-v2

# Generated tsvector columns fail the whole INSERT if the vector exceeds Postgres's 1 MB limit, so
# only a prefix of very long pages (large spreadsheet sheets) is indexed for keyword search.
TSVECTOR_MAX_CHARS = 200_000


def _enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """VARCHAR + CHECK rather than a native enum: adding a value needs no ALTER TYPE."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
    )


def _money() -> Numeric[Decimal]:
    return Numeric(18, 4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Document(Base):
    """One uploaded file and its position in the processing pipeline."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress_range"),
        CheckConstraint("attempt_count >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "status <> 'failed' OR failure_reason IS NOT NULL", name="failed_has_reason"
        ),
        Index("ix_documents_status_created_at", "status", text("created_at DESC")),
        Index(
            "ix_documents_needs_review_created_at",
            text("created_at DESC"),
            postgresql_where=text("needs_review"),
        ),
        Index("ix_documents_sha256", "sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64))
    file_type: Mapped[FileType] = mapped_column(_enum(FileType, "file_type"))
    mime_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[DocumentStatus] = mapped_column(
        _enum(DocumentStatus, "document_status"), default=DocumentStatus.QUEUED
    )
    stage: Mapped[ProcessingStage] = mapped_column(
        _enum(ProcessingStage, "processing_stage"), default=ProcessingStage.UPLOAD
    )
    progress: Mapped[int] = mapped_column(SmallInteger, default=0)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    is_retryable: Mapped[bool] = mapped_column(Boolean, default=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    task_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pages: Mapped[list["DocumentPage"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    extraction_results: Mapped[list["ExtractionResult"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    history: Mapped[list["ProcessingHistory"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentPage(Base):
    """One PDF page, TIFF frame, or spreadsheet sheet."""

    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number"),
        CheckConstraint("page_number >= 1", name="page_number_positive"),
        CheckConstraint(
            "ocr_confidence IS NULL OR ocr_confidence BETWEEN 0 AND 1",
            name="ocr_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    page_number: Mapped[int] = mapped_column(Integer)
    sheet_name: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[TextSource | None] = mapped_column(_enum(TextSource, "text_source"))
    status: Mapped[PageStatus] = mapped_column(
        _enum(PageStatus, "page_status"), default=PageStatus.OK
    )
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    processing_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    document: Mapped[Document] = relationship(back_populates="pages")
    text: Mapped["ExtractedText | None"] = relationship(
        back_populates="page", cascade="all, delete-orphan", passive_deletes=True
    )


class ExtractedText(Base):
    """Text of one page. Kept apart from document_pages so page metadata queries stay small."""

    __tablename__ = "extracted_text"
    __table_args__ = (
        UniqueConstraint("page_id"),
        Index("ix_extracted_text_document_id", "document_id"),
        Index("ix_extracted_text_content_tsv", "content_tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_pages.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    content_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(f"to_tsvector('english', left(content, {TSVECTOR_MAX_CHARS}))", persisted=True),
    )
    created_at: Mapped[datetime] = _created_at()

    page: Mapped[DocumentPage] = relationship(back_populates="text")


class ExtractionResult(Base):
    """One invoice found in a document, versioned across reprocessing.

    A long PDF can hold many invoices, so a document has many of these (invoice_index), and only
    the latest version of each is current. Unknown fields stay NULL; field_metadata explains why.

    field_metadata: {"<field>": {"confidence": 0.0-1.0, "reason": str | null,
                                 "source_snippet": str | null, "source_page": int | null}}
    """

    __tablename__ = "extraction_results"
    __table_args__ = (
        UniqueConstraint("document_id", "invoice_index", "version"),
        CheckConstraint(
            "overall_confidence IS NULL OR overall_confidence BETWEEN 0 AND 1",
            name="overall_confidence_range",
        ),
        CheckConstraint("page_start IS NULL OR page_start >= 1", name="page_start_positive"),
        CheckConstraint(
            "page_end IS NULL OR page_start IS NULL OR page_end >= page_start",
            name="page_range_ordered",
        ),
        Index(
            "uq_extraction_results_current_invoice",
            "document_id",
            "invoice_index",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_extraction_results_duplicate_key",
            "duplicate_key",
            postgresql_where=text("is_current AND duplicate_key IS NOT NULL"),
        ),
        Index(
            "ix_extraction_results_invoice_key",
            "invoice_key",
            postgresql_where=text("is_current AND invoice_key IS NOT NULL"),
        ),
        Index("ix_extraction_results_invoice_number", "invoice_number"),
        Index("ix_extraction_results_invoice_date", "invoice_date"),
        Index("ix_extraction_results_supplier_lower", text("lower(supplier_name)")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    invoice_index: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)

    invoice_number: Mapped[str | None] = mapped_column(String(128))
    # Letters and digits only, so cross-document checks match "INV-0042" with "inv 0042".
    invoice_key: Mapped[str | None] = mapped_column(String(128))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    supplier_name: Mapped[str | None] = mapped_column(String(512))
    customer_name: Mapped[str | None] = mapped_column(String(512))
    subtotal: Mapped[Decimal | None] = mapped_column(_money())
    tax: Mapped[Decimal | None] = mapped_column(_money())
    total: Mapped[Decimal | None] = mapped_column(_money())
    currency: Mapped[str | None] = mapped_column(String(3))

    field_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    overall_confidence: Mapped[float | None] = mapped_column(Float)
    # Hash of normalized supplier + number + date + total. Not unique: duplicates are flagged for
    # review, not rejected.
    duplicate_key: Mapped[str | None] = mapped_column(String(64))

    method: Mapped[ExtractionMethod] = mapped_column(_enum(ExtractionMethod, "extraction_method"))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    warnings: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="extraction_results")
    line_items: Mapped[list["LineItem"]] = relationship(
        back_populates="extraction_result",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="LineItem.position",
    )
    validation_errors: Mapped[list["ValidationError"]] = relationship(
        back_populates="extraction_result", cascade="all, delete-orphan", passive_deletes=True
    )


class LineItem(Base):
    __tablename__ = "line_items"
    __table_args__ = (UniqueConstraint("extraction_result_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    extraction_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_results.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(_money())
    unit_price: Mapped[Decimal | None] = mapped_column(_money())
    amount: Mapped[Decimal | None] = mapped_column(_money())
    field_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    extraction_result: Mapped[ExtractionResult] = relationship(back_populates="line_items")


class ValidationError(Base):
    """One failed rule. `related_document_id` points at the other file in cross-document checks."""

    __tablename__ = "validation_errors"
    __table_args__ = (
        Index("ix_validation_errors_document_id", "document_id"),
        Index("ix_validation_errors_extraction_result_id", "extraction_result_id"),
        Index("ix_validation_errors_related_document_id", "related_document_id"),
        Index("ix_validation_errors_code", "code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    extraction_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_results.id", ondelete="CASCADE")
    )
    related_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    code: Mapped[str] = mapped_column(String(64))
    severity: Mapped[Severity] = mapped_column(_enum(Severity, "severity"))
    field: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    expected_value: Mapped[str | None] = mapped_column(Text)
    actual_value: Mapped[str | None] = mapped_column(Text)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created_at()

    extraction_result: Mapped[ExtractionResult] = relationship(back_populates="validation_errors")


class ProcessingHistory(Base):
    """Append-only audit trail: pipeline stages, retries, and manual corrections (before/after)."""

    __tablename__ = "processing_history"
    __table_args__ = (Index("ix_processing_history_document_id_id", "document_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    event: Mapped[ProcessingEvent] = mapped_column(_enum(ProcessingEvent, "processing_event"))
    stage: Mapped[ProcessingStage | None] = mapped_column(
        _enum(ProcessingStage, "history_processing_stage")
    )
    attempt: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = _created_at()

    document: Mapped[Document] = relationship(back_populates="history")


class Embedding(Base):
    """One chunk of document text with its vector, for semantic search."""

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", "model"),
        Index(
            "ix_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    page_number: Mapped[int | None] = mapped_column(Integer)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    model: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _created_at()
