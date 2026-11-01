"""add invoice key and warnings

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "extraction_results", sa.Column("invoice_key", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "extraction_results",
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_extraction_results_invoice_key",
        "extraction_results",
        ["invoice_key"],
        unique=False,
        postgresql_where=sa.text("is_current AND invoice_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extraction_results_invoice_key",
        table_name="extraction_results",
        postgresql_where=sa.text("is_current AND invoice_key IS NOT NULL"),
    )
    op.drop_column("extraction_results", "warnings")
    op.drop_column("extraction_results", "invoice_key")
