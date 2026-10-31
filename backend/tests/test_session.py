import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import session as session_module
from app.db.enums import FileType
from app.db.models import Document

pytestmark = pytest.mark.db


def _document() -> Document:
    return Document(
        original_filename="a.pdf",
        storage_path="/data/uploads/a.pdf",
        sha256="a" * 64,
        file_type=FileType.PDF,
        mime_type="application/pdf",
        size_bytes=1,
    )


def _count(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(Document)) or 0


@pytest.fixture
def factory(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> sessionmaker[Session]:
    session_factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    monkeypatch.setattr(session_module, "get_session_factory", lambda: session_factory)
    return session_factory


def test_session_scope_commits_on_success(
    factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    with session_module.session_scope() as session:
        session.add(_document())

    assert _count(migrated_engine) == 1


def test_session_scope_rolls_back_on_error(
    factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    with pytest.raises(RuntimeError), session_module.session_scope() as session:
        session.add(_document())
        session.flush()
        raise RuntimeError("task failed")

    assert _count(migrated_engine) == 0


def test_get_db_yields_a_working_session_and_closes_it(factory: sessionmaker[Session]) -> None:
    dependency = session_module.get_db()
    session = next(dependency)

    assert session.scalar(select(func.count()).select_from(Document)) == 0

    dependency.close()
    assert not session.in_transaction()
