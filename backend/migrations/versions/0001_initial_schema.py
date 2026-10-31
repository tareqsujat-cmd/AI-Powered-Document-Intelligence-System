"""initial schema

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "file_type",
            sa.Enum(
                "pdf",
                "jpeg",
                "png",
                "tiff",
                "xlsx",
                "xls",
                "csv",
                name="file_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("mime_type", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "processing",
                "completed",
                "failed",
                name="document_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "stage",
            sa.Enum(
                "upload",
                "file_validation",
                "text_extraction",
                "llm_extraction",
                "validation",
                "indexing",
                "finished",
                name="processing_stage",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("progress", sa.SmallInteger(), nullable=False),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("is_retryable", sa.Boolean(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status <> 'failed' OR failure_reason IS NOT NULL",
            name=op.f("ck_documents_failed_has_reason"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_documents_attempts_non_negative")),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name=op.f("ck_documents_progress_range")),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_documents_size_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(
        "ix_documents_needs_review_created_at",
        "documents",
        [sa.literal_column("created_at DESC")],
        unique=False,
        postgresql_where=sa.text("needs_review"),
    )
    op.create_index("ix_documents_sha256", "documents", ["sha256"], unique=False)
    op.create_index(
        "ix_documents_status_created_at",
        "documents",
        ["status", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "document_pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("sheet_name", sa.String(length=255), nullable=True),
        sa.Column(
            "source",
            sa.Enum(
                "text_layer",
                "ocr",
                "spreadsheet",
                name="text_source",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "ok",
                "empty",
                "failed",
                name="page_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("processing_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ocr_confidence IS NULL OR ocr_confidence BETWEEN 0 AND 1",
            name=op.f("ck_document_pages_ocr_confidence_range"),
        ),
        sa.CheckConstraint("page_number >= 1", name=op.f("ck_document_pages_page_number_positive")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_pages_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_pages")),
        sa.UniqueConstraint(
            "document_id", "page_number", name=op.f("uq_document_pages_document_id_page_number")
        ),
    )
    op.create_table(
        "embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(dim=384), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_embeddings_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embeddings")),
        sa.UniqueConstraint(
            "document_id",
            "chunk_index",
            "model",
            name=op.f("uq_embeddings_document_id_chunk_index_model"),
        ),
    )
    op.create_index(
        "ix_embeddings_embedding_hnsw",
        "embeddings",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "extraction_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_index", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("invoice_number", sa.String(length=128), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("supplier_name", sa.String(length=512), nullable=True),
        sa.Column("customer_name", sa.String(length=512), nullable=True),
        sa.Column("subtotal", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("tax", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("total", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("field_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("overall_confidence", sa.Float(), nullable=True),
        sa.Column("duplicate_key", sa.String(length=64), nullable=True),
        sa.Column(
            "method",
            sa.Enum(
                "llm",
                "rule_based",
                name="extraction_method",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("llm_model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "overall_confidence IS NULL OR overall_confidence BETWEEN 0 AND 1",
            name=op.f("ck_extraction_results_overall_confidence_range"),
        ),
        sa.CheckConstraint(
            "page_end IS NULL OR page_start IS NULL OR page_end >= page_start",
            name=op.f("ck_extraction_results_page_range_ordered"),
        ),
        sa.CheckConstraint(
            "page_start IS NULL OR page_start >= 1",
            name=op.f("ck_extraction_results_page_start_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_extraction_results_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_results")),
        sa.UniqueConstraint(
            "document_id",
            "invoice_index",
            "version",
            name=op.f("uq_extraction_results_document_id_invoice_index_version"),
        ),
    )
    op.create_index(
        "ix_extraction_results_duplicate_key",
        "extraction_results",
        ["duplicate_key"],
        unique=False,
        postgresql_where=sa.text("is_current AND duplicate_key IS NOT NULL"),
    )
    op.create_index(
        "ix_extraction_results_invoice_date", "extraction_results", ["invoice_date"], unique=False
    )
    op.create_index(
        "ix_extraction_results_invoice_number",
        "extraction_results",
        ["invoice_number"],
        unique=False,
    )
    op.create_index(
        "ix_extraction_results_supplier_lower",
        "extraction_results",
        [sa.literal_column("lower(supplier_name)")],
        unique=False,
    )
    op.create_index(
        "uq_extraction_results_current_invoice",
        "extraction_results",
        ["document_id", "invoice_index"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "processing_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event",
            sa.Enum(
                "uploaded",
                "stage_started",
                "stage_completed",
                "stage_failed",
                "retry_scheduled",
                "reprocess_requested",
                "manual_correction",
                name="processing_event",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "stage",
            sa.Enum(
                "upload",
                "file_validation",
                "text_extraction",
                "llm_extraction",
                "validation",
                "indexing",
                "finished",
                name="history_processing_stage",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_processing_history_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_history")),
    )
    op.create_index(
        "ix_processing_history_document_id_id",
        "processing_history",
        ["document_id", "id"],
        unique=False,
    )
    op.create_table(
        "extracted_text",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "content_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', left(content, 200000))", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_extracted_text_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["document_pages.id"],
            name=op.f("fk_extracted_text_page_id_document_pages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extracted_text")),
        sa.UniqueConstraint("page_id", name=op.f("uq_extracted_text_page_id")),
    )
    op.create_index(
        "ix_extracted_text_content_tsv",
        "extracted_text",
        ["content_tsv"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_extracted_text_document_id", "extracted_text", ["document_id"], unique=False
    )
    op.create_table(
        "line_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extraction_result_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("field_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["extraction_result_id"],
            ["extraction_results.id"],
            name=op.f("fk_line_items_extraction_result_id_extraction_results"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_line_items")),
        sa.UniqueConstraint(
            "extraction_result_id",
            "position",
            name=op.f("uq_line_items_extraction_result_id_position"),
        ),
    )
    op.create_table(
        "validation_errors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_result_id", sa.Uuid(), nullable=False),
        sa.Column("related_document_id", sa.Uuid(), nullable=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column(
            "severity",
            sa.Enum(
                "info",
                "warning",
                "error",
                name="severity",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("field", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("expected_value", sa.Text(), nullable=True),
        sa.Column("actual_value", sa.Text(), nullable=True),
        sa.Column("is_resolved", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_validation_errors_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_result_id"],
            ["extraction_results.id"],
            name=op.f("fk_validation_errors_extraction_result_id_extraction_results"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["related_document_id"],
            ["documents.id"],
            name=op.f("fk_validation_errors_related_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_errors")),
    )
    op.create_index("ix_validation_errors_code", "validation_errors", ["code"], unique=False)
    op.create_index(
        "ix_validation_errors_document_id", "validation_errors", ["document_id"], unique=False
    )
    op.create_index(
        "ix_validation_errors_extraction_result_id",
        "validation_errors",
        ["extraction_result_id"],
        unique=False,
    )
    op.create_index(
        "ix_validation_errors_related_document_id",
        "validation_errors",
        ["related_document_id"],
        unique=False,
    )


def downgrade() -> None:
    # The vector extension is left in place: it is database-wide and may have other users.
    op.drop_index("ix_validation_errors_related_document_id", table_name="validation_errors")
    op.drop_index("ix_validation_errors_extraction_result_id", table_name="validation_errors")
    op.drop_index("ix_validation_errors_document_id", table_name="validation_errors")
    op.drop_index("ix_validation_errors_code", table_name="validation_errors")
    op.drop_table("validation_errors")
    op.drop_table("line_items")
    op.drop_index("ix_extracted_text_document_id", table_name="extracted_text")
    op.drop_index(
        "ix_extracted_text_content_tsv", table_name="extracted_text", postgresql_using="gin"
    )
    op.drop_table("extracted_text")
    op.drop_index("ix_processing_history_document_id_id", table_name="processing_history")
    op.drop_table("processing_history")
    op.drop_index(
        "uq_extraction_results_current_invoice",
        table_name="extraction_results",
        postgresql_where=sa.text("is_current"),
    )
    op.drop_index("ix_extraction_results_supplier_lower", table_name="extraction_results")
    op.drop_index("ix_extraction_results_invoice_number", table_name="extraction_results")
    op.drop_index("ix_extraction_results_invoice_date", table_name="extraction_results")
    op.drop_index(
        "ix_extraction_results_duplicate_key",
        table_name="extraction_results",
        postgresql_where=sa.text("is_current AND duplicate_key IS NOT NULL"),
    )
    op.drop_table("extraction_results")
    op.drop_index(
        "ix_embeddings_embedding_hnsw",
        table_name="embeddings",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_table("embeddings")
    op.drop_table("document_pages")
    op.drop_index("ix_documents_status_created_at", table_name="documents")
    op.drop_index("ix_documents_sha256", table_name="documents")
    op.drop_index(
        "ix_documents_needs_review_created_at",
        table_name="documents",
        postgresql_where=sa.text("needs_review"),
    )
    op.drop_table("documents")
