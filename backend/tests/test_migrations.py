import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.db.base import Base
from tests.conftest import alembic_config

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
    "documents",
    "document_pages",
    "extracted_text",
    "extraction_results",
    "line_items",
    "validation_errors",
    "processing_history",
    "embeddings",
}


def test_metadata_defines_every_required_table() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_upgrade_creates_all_tables_and_pgvector(empty_database_url: str) -> None:
    command.upgrade(alembic_config(empty_database_url), "head")

    engine = create_engine(empty_database_url)
    try:
        assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
            )
    finally:
        engine.dispose()


def test_models_and_migrations_do_not_drift(empty_database_url: str) -> None:
    config = alembic_config(empty_database_url)
    command.upgrade(config, "head")

    command.check(config)  # raises if autogenerate would produce any change


def test_downgrade_then_upgrade_round_trip(empty_database_url: str) -> None:
    config = alembic_config(empty_database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(empty_database_url)
    try:
        assert not EXPECTED_TABLES & set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_expected_search_indexes_exist(empty_database_url: str) -> None:
    command.upgrade(alembic_config(empty_database_url), "head")

    engine = create_engine(empty_database_url)
    try:
        with engine.connect() as connection:
            methods = dict(
                connection.execute(
                    text(
                        "SELECT c.relname, am.amname FROM pg_class c "
                        "JOIN pg_am am ON am.oid = c.relam WHERE c.relkind = 'i'"
                    )
                ).all()
            )
    finally:
        engine.dispose()

    assert methods["ix_embeddings_embedding_hnsw"] == "hnsw"
    assert methods["ix_extracted_text_content_tsv"] == "gin"
