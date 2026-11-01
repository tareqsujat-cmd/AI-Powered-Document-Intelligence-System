import hashlib
import io
import os
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import UploadFile
from sqlalchemy import Engine, event, func, select, update
from sqlalchemy.orm import Session

from app.api.documents import _store_upload
from app.core.config import Settings
from app.db.enums import (
    DocumentStatus,
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
    ExtractedText,
    ExtractionResult,
    ProcessingHistory,
    ValidationError,
)
from app.pipeline.service import QUEUE_UNAVAILABLE
from app.processing.errors import FileTooLargeError
from tests.api_helpers import (
    API,
    CSV_BYTES,
    EXECUTABLE_BYTES,
    Api,
    add_result,
    encrypted_xlsx_bytes,
    line,
    make_document,
    make_invoice,
    pdf_bytes,
    png_bytes,
    xlsx_bytes,
)

pytestmark = pytest.mark.db
pytest_plugins = ["tests.api_helpers"]

MIB = 1024 * 1024
SUMMARY_KEYS = {
    "id",
    "filename",
    "file_type",
    "size_bytes",
    "page_count",
    "status",
    "stage",
    "progress",
    "needs_review",
    "failure_reason",
    "is_retryable",
    "attempt_count",
    "created_at",
    "updated_at",
    "processing_started_at",
    "processing_finished_at",
    "invoice_count",
}


def documents_in(db: Session) -> list[Document]:
    db.expire_all()
    return list(db.scalars(select(Document)))


def history_of(db: Session, document_id: Any) -> list[ProcessingHistory]:
    return list(
        db.scalars(
            select(ProcessingHistory)
            .where(ProcessingHistory.document_id == document_id)
            .order_by(ProcessingHistory.id)
        )
    )


def add_page(
    db: Session,
    document: Document,
    number: int = 1,
    content: str | None = "Page text",
    **fields: Any,
) -> DocumentPage:
    values: dict[str, Any] = {"source": TextSource.TEXT_LAYER, "processing_ms": 12}
    page = DocumentPage(document_id=document.id, page_number=number, **{**values, **fields})
    db.add(page)
    db.flush()
    if content is not None:
        db.add(ExtractedText(document_id=document.id, page_id=page.id, content=content))
    db.commit()
    return page


def set_idle_for(db: Session, document: Document, delta: timedelta) -> None:
    db.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(updated_at=datetime.now(UTC) - delta)
    )
    db.commit()


# Upload


def test_upload_stores_the_file_records_it_and_queues_it(api: Api, db: Session) -> None:
    content = pdf_bytes()

    response = api.upload(("invoice.pdf", content))

    assert response.status_code == 202
    [outcome] = response.json()["results"]
    assert outcome["filename"] == "invoice.pdf"
    assert outcome["accepted"] is True
    assert outcome["error"] is None
    summary = outcome["document"]
    assert set(summary) == SUMMARY_KEYS
    assert summary["status"] == "queued"
    assert summary["stage"] == "upload"
    assert summary["file_type"] == "pdf"
    assert summary["size_bytes"] == len(content)
    assert summary["invoice_count"] == 0

    [document] = documents_in(db)
    assert str(document.id) == summary["id"]
    assert document.sha256 == hashlib.sha256(content).hexdigest()
    assert document.mime_type == "application/pdf"
    assert Path(document.storage_path).read_bytes() == content
    assert Path(document.storage_path).parent == api.settings.storage_dir
    assert api.queue.calls == [
        {
            "name": "documents.process",
            "args": [summary["id"]],
            "task_id": document.task_id,
            "retry": False,
        }
    ]
    [entry] = history_of(db, document.id)
    assert entry.event is ProcessingEvent.UPLOADED


@pytest.mark.parametrize(
    ("name", "make_content", "file_type", "mime"),
    [
        ("a.pdf", pdf_bytes, "pdf", "application/pdf"),
        ("b.png", png_bytes, "png", "image/png"),
        ("c.csv", lambda: CSV_BYTES, "csv", "text/csv"),
        (
            "d.xlsx",
            xlsx_bytes,
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ],
)
def test_upload_identifies_each_supported_type(
    api: Api,
    db: Session,
    name: str,
    make_content: Callable[[], bytes],
    file_type: str,
    mime: str,
) -> None:
    [outcome] = api.upload((name, make_content())).json()["results"]

    assert outcome["document"]["file_type"] == file_type
    [document] = documents_in(db)
    assert document.mime_type == mime
    assert Path(document.storage_path).suffix == f".{name.rsplit('.')[-1]}"


def test_mixed_batch_gives_each_file_its_own_outcome(api: Api, db: Session) -> None:
    response = api.upload(
        ("good.pdf", pdf_bytes()),
        ("bad.exe", EXECUTABLE_BYTES),
        ("disguised.pdf", EXECUTABLE_BYTES),
        ("empty.pdf", b""),
        ("data.csv", CSV_BYTES),
    )

    assert response.status_code == 202
    results = response.json()["results"]
    assert [(r["filename"], r["accepted"]) for r in results] == [
        ("good.pdf", True),
        ("bad.exe", False),
        ("disguised.pdf", False),
        ("empty.pdf", True),
        ("data.csv", True),
    ]
    assert [r["error"]["code"] if r["error"] else None for r in results] == [
        None,
        "unsupported_file",
        "unsupported_file",
        "empty_file",
        None,
    ]
    assert results[1]["document"] is None
    assert results[3]["document"]["status"] == "failed"
    assert {document.original_filename for document in documents_in(db)} == {
        "good.pdf",
        "empty.pdf",
        "data.csv",
    }
    assert len(api.stored_files()) == 3
    assert len(api.queue.calls) == 2


def test_disguised_executable_leaves_no_file_and_no_record(api: Api, db: Session) -> None:
    [outcome] = api.upload(("invoice.pdf", EXECUTABLE_BYTES)).json()["results"]

    assert outcome["accepted"] is False
    assert outcome["error"]["code"] == "unsupported_file"
    assert api.stored_files() == []
    assert documents_in(db) == []
    assert api.queue.calls == []


def test_oversized_file_is_rejected_and_deleted(make_api: Callable[..., Api], db: Session) -> None:
    api = make_api(max_upload_mb=1)
    content = b"%PDF-1.7\n" + b" " * MIB

    [outcome] = api.upload(("big.pdf", content)).json()["results"]

    assert outcome["accepted"] is False
    assert outcome["error"]["code"] == "file_too_large"
    assert "1 MB" in outcome["error"]["reason"]
    assert api.stored_files() == []
    assert documents_in(db) == []


def test_file_exactly_at_the_limit_is_accepted(make_api: Callable[..., Api]) -> None:
    api = make_api(max_upload_mb=1)
    header = b"%PDF-1.7\n"

    [outcome] = api.upload(("big.pdf", header + b" " * (MIB - len(header)))).json()["results"]

    assert outcome["accepted"] is True
    assert outcome["document"]["size_bytes"] == MIB


def test_store_upload_stops_reading_as_soon_as_the_limit_passes(tmp_path: Path) -> None:
    class EndlessFile(io.RawIOBase):
        reads = 0

        def read(self, size: int = -1) -> bytes:
            self.reads += 1
            return b"x" * size

    source = EndlessFile()
    settings = Settings(_env_file=None, storage_dir=tmp_path / "s", max_upload_mb=2)

    with pytest.raises(FileTooLargeError):
        _store_upload(UploadFile(source, filename="a.pdf"), "a.pdf", settings)

    assert source.reads == 3
    assert list(settings.storage_dir.iterdir()) == []


def test_stored_name_takes_only_an_allowed_extension_from_the_client(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, storage_dir=tmp_path / "s")

    path, _ = _store_upload(UploadFile(io.BytesIO(b"x"), filename="x.php"), "x.php", settings)

    assert path.suffix == ""
    uuid.UUID(path.name)


def test_storage_failure_is_reported_without_failing_the_batch(
    api: Api, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_store = _store_upload
    outcomes = iter([OSError("/secret/path: no space left on device"), None])

    def flaky_store(*args: Any) -> tuple[Path, str]:
        error = next(outcomes)
        if error:
            raise error
        return real_store(*args)

    monkeypatch.setattr("app.api.documents._store_upload", flaky_store)

    results = api.upload(("a.pdf", pdf_bytes()), ("b.pdf", pdf_bytes())).json()["results"]

    assert results[0]["accepted"] is False
    assert results[0]["error"]["code"] == "storage_error"
    assert "secret" not in results[0]["error"]["reason"]
    assert results[1]["accepted"] is True
    assert len(documents_in(db)) == 1


def test_a_failed_database_write_removes_the_stored_file(
    api: Api, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Too long for the column, so the database refuses the row.
    monkeypatch.setattr("app.processing.file_validation._sha256", lambda path: "f" * 65)

    response = api.upload(("a.pdf", pdf_bytes()))

    assert response.status_code == 500
    assert api.stored_files() == []
    assert documents_in(db) == []


@pytest.mark.parametrize(
    ("name", "make_content", "code", "file_type", "mime"),
    [
        ("empty.pdf", lambda: b"", "empty_file", FileType.PDF, "application/pdf"),
        ("blank.csv", lambda: b"  \n \n", "empty_file", FileType.CSV, "text/csv"),
        (
            "broken.xlsx",
            lambda: b"PK\x03\x04 this is not really a zip archive",
            "corrupted_file",
            FileType.XLSX,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (
            "broken.xls",
            lambda: b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\xff" * 2048,
            "corrupted_file",
            FileType.XLS,
            "application/vnd.ms-excel",
        ),
        (
            "locked.xlsx",
            encrypted_xlsx_bytes,
            "encrypted_file",
            FileType.XLSX,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ],
)
def test_unreadable_files_are_recorded_as_failed_and_retryable(
    api: Api,
    db: Session,
    name: str,
    make_content: Callable[[], bytes],
    code: str,
    file_type: FileType,
    mime: str,
) -> None:
    content = make_content()

    response = api.upload((name, content))

    assert response.status_code == 202
    [outcome] = response.json()["results"]
    assert outcome["accepted"] is True
    assert outcome["error"]["code"] == code
    assert outcome["document"]["status"] == "failed"
    assert outcome["document"]["failure_reason"] == outcome["error"]["reason"]
    assert outcome["document"]["is_retryable"] is True
    assert outcome["document"]["file_type"] == file_type.value

    [document] = documents_in(db)
    assert document.status is DocumentStatus.FAILED
    assert document.stage is ProcessingStage.FILE_VALIDATION
    assert document.file_type is file_type
    assert document.mime_type == mime
    assert document.sha256 == hashlib.sha256(content).hexdigest()
    assert document.size_bytes == len(content)
    assert Path(document.storage_path).read_bytes() == content
    [entry] = history_of(db, document.id)
    assert entry.event is ProcessingEvent.STAGE_FAILED
    assert entry.stage is ProcessingStage.FILE_VALIDATION
    assert entry.message == document.failure_reason
    assert api.queue.calls == []


@pytest.mark.parametrize(
    ("hostile_name", "safe_name"),
    [
        ("../../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\windows\\evil.pdf", "evil.pdf"),
        ("/absolute/path/report.pdf", "report.pdf"),
    ],
)
def test_path_traversal_filename_cannot_escape_storage(
    api: Api, db: Session, tmp_path: Path, hostile_name: str, safe_name: str
) -> None:
    [outcome] = api.upload((hostile_name, pdf_bytes())).json()["results"]

    assert outcome["filename"] == safe_name
    assert outcome["document"]["filename"] == safe_name
    [document] = documents_in(db)
    assert document.original_filename == safe_name
    stored = Path(document.storage_path)
    assert stored.parent == api.settings.storage_dir
    uuid.UUID(stored.stem)
    assert stored.suffix == ".pdf"
    everything = {path.name for path in tmp_path.rglob("*")}
    assert safe_name not in everything


def test_uppercase_extension_is_stored_in_lowercase(api: Api) -> None:
    api.upload(("SCAN.PDF", pdf_bytes()))

    [stored] = api.stored_files()
    assert stored.suffix == ".pdf"


def test_unsupported_extension_never_reaches_the_stored_name(api: Api) -> None:
    [outcome] = api.upload(("x.php", pdf_bytes())).json()["results"]

    assert outcome["accepted"] is False
    assert outcome["error"]["code"] == "unsupported_file"
    assert api.stored_files() == []


def test_queue_down_at_upload_keeps_the_document_as_failed(api: Api, db: Session) -> None:
    api.queue.fail = True

    response = api.upload(("a.pdf", pdf_bytes()))

    assert response.status_code == 202
    [outcome] = response.json()["results"]
    assert outcome["accepted"] is True
    assert outcome["error"] == {"code": "queue_unavailable", "reason": QUEUE_UNAVAILABLE}
    assert outcome["document"]["status"] == "failed"
    assert outcome["document"]["is_retryable"] is True
    assert "secret-host" not in response.text
    [document] = documents_in(db)
    assert document.status is DocumentStatus.FAILED
    assert document.failure_reason == QUEUE_UNAVAILABLE
    assert len(api.stored_files()) == 1


def test_upload_without_files_is_422(api: Api) -> None:
    assert api.client.post(f"{API}/documents").status_code == 422
    assert api.client.post(f"{API}/documents", data={"files": "text"}).status_code == 422
    assert api.client.post(f"{API}/documents", files={"other": ("a.pdf", b"x")}).status_code == 422


def test_too_many_files_is_422_and_stores_nothing(
    make_api: Callable[..., Api], db: Session
) -> None:
    api = make_api(max_files_per_upload=2)
    files = [(f"{index}.pdf", pdf_bytes()) for index in range(3)]

    response = api.upload(*files)

    assert response.status_code == 422
    assert "2 files" in response.json()["detail"]
    assert api.stored_files() == []
    assert documents_in(db) == []


def test_files_at_the_batch_limit_are_accepted(make_api: Callable[..., Api]) -> None:
    api = make_api(max_files_per_upload=2)

    response = api.upload(("0.pdf", pdf_bytes()), ("1.pdf", pdf_bytes()))

    assert response.status_code == 202
    assert len(response.json()["results"]) == 2


# List


def names(api: Api, query: str = "") -> tuple[set[str], int]:
    response = api.client.get(f"{API}/documents?{query}")
    assert response.status_code == 200, query
    body = response.json()
    return {item["filename"] for item in body["items"]}, body["total"]


def seed_for_filters(db: Session) -> None:
    a = make_document(db, original_filename="alpha.pdf", needs_review=True)
    add_result(
        db,
        a,
        make_invoice(
            number="INV-100", supplier="Northwind Ltd", invoice_date=date(2026, 1, 10), total="100"
        ),
    )
    b = make_document(
        db, original_filename="Beta_report.csv", file_type=FileType.CSV, mime_type="text/csv"
    )
    add_result(
        db,
        b,
        make_invoice(
            number="INV-200", supplier="Contoso Inc", invoice_date=date(2026, 2, 20), total="200.50"
        ),
    )
    make_document(
        db,
        original_filename="gamma.png",
        file_type=FileType.PNG,
        mime_type="image/png",
        status=DocumentStatus.FAILED,
        failure_reason="Bad file.",
    )
    make_document(db, original_filename="delta.pdf", status=DocumentStatus.QUEUED)
    # An older version of an invoice must not match on values that have since been replaced.
    e = make_document(db, original_filename="epsilon.pdf")
    add_result(
        db,
        e,
        make_invoice(
            number="OLD-9", supplier="Ghost Corp", invoice_date=date(2020, 1, 1), total="999"
        ),
    )
    add_result(
        db,
        e,
        make_invoice(
            number="NEW-9", supplier="Real Ltd", invoice_date=date(2026, 3, 1), total="50"
        ),
    )


def test_list_filters(api: Api, db: Session) -> None:
    seed_for_filters(db)
    everything = {"alpha.pdf", "Beta_report.csv", "gamma.png", "delta.pdf", "epsilon.pdf"}
    expectations: list[tuple[str, set[str]]] = [
        ("", everything),
        ("status=failed", {"gamma.png"}),
        ("status=queued", {"delta.pdf"}),
        ("status=processing", set()),
        ("needs_review=true", {"alpha.pdf"}),
        ("needs_review=false", everything - {"alpha.pdf"}),
        ("file_type=csv", {"Beta_report.csv"}),
        ("file_type=pdf", {"alpha.pdf", "delta.pdf", "epsilon.pdf"}),
        ("filename=ALPHA", {"alpha.pdf"}),
        ("filename=.pdf", {"alpha.pdf", "delta.pdf", "epsilon.pdf"}),
        ("filename=nothing", set()),
        ("supplier=northwind", {"alpha.pdf"}),
        ("supplier=CONTOSO", {"Beta_report.csv"}),
        ("supplier=ltd", {"alpha.pdf", "epsilon.pdf"}),
        ("supplier=ghost", set()),
        ("invoice_number=inv 100", {"alpha.pdf"}),
        ("invoice_number=INV-0100", set()),
        ("invoice_number=OLD-9", set()),
        ("invoice_number=new9", {"epsilon.pdf"}),
        ("invoice_number=---", set()),
        ("date_from=2026-02-20", {"Beta_report.csv", "epsilon.pdf"}),
        ("date_from=2026-02-21", {"epsilon.pdf"}),
        ("date_to=2026-01-10", {"alpha.pdf"}),
        ("date_to=2026-01-09", set()),
        ("date_from=2026-01-11&date_to=2026-02-28", {"Beta_report.csv"}),
        ("total_min=200.50", {"Beta_report.csv"}),
        ("total_min=200.51", set()),
        ("total_max=50", {"epsilon.pdf"}),
        ("total_max=49.99", set()),
        ("total_min=60&total_max=150", {"alpha.pdf"}),
        ("total_min=900", set()),
        ("status=completed&file_type=pdf", {"alpha.pdf", "epsilon.pdf"}),
        ("status=completed&file_type=pdf&needs_review=true", {"alpha.pdf"}),
        ("file_type=csv&supplier=contoso&total_min=100&date_to=2026-12-31", {"Beta_report.csv"}),
        ("file_type=pdf&supplier=contoso", set()),
        ("filename=alpha&invoice_number=INV-200", set()),
    ]
    for query, expected in expectations:
        assert names(api, query) == (expected, len(expected)), query


def test_filename_filter_treats_wildcards_as_ordinary_characters(api: Api, db: Session) -> None:
    make_document(db, original_filename="100%_final.pdf")
    make_document(db, original_filename="plain.pdf")
    make_document(db, original_filename="under_score.pdf")

    assert names(api, "filename=%25")[0] == {"100%_final.pdf"}
    assert names(api, "filename=_")[0] == {"100%_final.pdf", "under_score.pdf"}
    assert names(api, "filename=/")[0] == set()
    assert names(api, "filename=l%25")[0] == set()


def test_supplier_filter_treats_wildcards_as_ordinary_characters(api: Api, db: Session) -> None:
    add_result(db, make_document(db, original_filename="a.pdf"), make_invoice(supplier="A%B Ltd"))
    add_result(db, make_document(db, original_filename="b.pdf"), make_invoice(supplier="AxB Ltd"))

    assert names(api, "supplier=a%25b")[0] == {"a.pdf"}
    assert names(api, "supplier=a_b")[0] == set()


def test_document_with_many_matching_invoices_appears_once(api: Api, db: Session) -> None:
    document = make_document(db, original_filename="many.pdf")
    for index in range(3):
        add_result(
            db, document, make_invoice(number=f"INV-{index}", invoice_index=index, total="500")
        )
    add_result(db, make_document(db, original_filename="none.pdf"), make_invoice(total="1"))

    body = api.client.get(f"{API}/documents", params={"supplier": "north", "total_min": 100}).json()

    assert body["total"] == 1
    assert [item["filename"] for item in body["items"]] == ["many.pdf"]
    assert body["items"][0]["invoice_count"] == 3


def test_invoice_filters_must_all_hold_for_one_invoice(api: Api, db: Session) -> None:
    document = make_document(db, original_filename="two.pdf")
    add_result(db, document, make_invoice(supplier="Northwind", total="10", invoice_index=0))
    add_result(db, document, make_invoice(supplier="Contoso", total="999", invoice_index=1))

    assert names(api, "supplier=contoso&total_min=500")[1] == 1
    assert names(api, "supplier=northwind&total_min=500")[1] == 0


def test_invoice_count_ignores_old_versions_and_other_documents(api: Api, db: Session) -> None:
    document = make_document(db, original_filename="a.pdf")
    add_result(db, document)
    add_result(db, document)
    add_result(db, make_document(db, original_filename="b.pdf"))
    make_document(db, original_filename="c.pdf")

    items = api.client.get(f"{API}/documents").json()["items"]

    assert {item["filename"]: item["invoice_count"] for item in items} == {
        "a.pdf": 1,
        "b.pdf": 1,
        "c.pdf": 0,
    }


def test_list_pagination_and_totals(api: Api, db: Session) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(5):
        make_document(db, original_filename=f"{index}.pdf", created_at=base + timedelta(days=index))

    def page(query: str) -> tuple[list[str], dict[str, int]]:
        body = api.client.get(f"{API}/documents?{query}").json()
        return [item["filename"] for item in body["items"]], {
            key: body[key] for key in ("total", "limit", "offset")
        }

    assert page("limit=2") == (["4.pdf", "3.pdf"], {"total": 5, "limit": 2, "offset": 0})
    assert page("limit=2&offset=2") == (["2.pdf", "1.pdf"], {"total": 5, "limit": 2, "offset": 2})
    assert page("limit=2&offset=4") == (["0.pdf"], {"total": 5, "limit": 2, "offset": 4})
    assert page("limit=2&offset=5") == ([], {"total": 5, "limit": 2, "offset": 5})
    assert page("") == (
        ["4.pdf", "3.pdf", "2.pdf", "1.pdf", "0.pdf"],
        {"total": 5, "limit": 50, "offset": 0},
    )
    assert page("limit=200")[1]["limit"] == 200


def test_list_breaks_ties_by_id(api: Api, db: Session) -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    ids = sorted(
        (
            make_document(db, original_filename=f"{index}.pdf", created_at=moment).id
            for index in range(4)
        ),
        reverse=True,
    )

    items = api.client.get(f"{API}/documents").json()["items"]

    assert [item["id"] for item in items] == [str(value) for value in ids]


@pytest.mark.parametrize(
    "query",
    [
        "status=unknown",
        "file_type=exe",
        "needs_review=maybe",
        "date_from=15-03-2026",
        "date_to=notadate",
        "total_min=abc",
        "total_min=NaN",
        "total_max=Infinity",
        "total_min=1e999999",
        "limit=0",
        "limit=201",
        "offset=-1",
        "offset=99999999999999999999",
        "filename=a%00b",
        "supplier=a%00b",
        "invoice_number=a%00b",
    ],
)
def test_invalid_list_filters_are_422(api: Api, query: str) -> None:
    assert api.client.get(f"{API}/documents?{query}").status_code == 422


@pytest.mark.parametrize(
    "attempt",
    [
        "' OR '1'='1",
        "'; DROP TABLE documents; --",
        '" OR ""="',
        "%' UNION SELECT storage_path FROM documents --",
        "\\",
    ],
)
def test_sql_injection_attempts_in_filters_are_harmless(
    api: Api, db: Session, attempt: str
) -> None:
    make_document(db, original_filename="a.pdf")

    for name in ("filename", "supplier", "invoice_number"):
        response = api.client.get(f"{API}/documents", params={name: attempt})
        assert response.status_code == 200
        assert response.json()["total"] == 0, name

    assert names(api)[1] == 1


def test_sql_injection_in_an_uploaded_filename_is_stored_as_text(api: Api, db: Session) -> None:
    hostile = "x'; DROP TABLE documents; --.pdf"

    [outcome] = api.upload((hostile, pdf_bytes())).json()["results"]

    assert outcome["accepted"] is True
    assert outcome["document"]["filename"] == hostile
    assert db.scalar(select(func.count()).select_from(Document)) == 1
    assert names(api, f"filename={quote('DROP TABLE')}")[1] == 1


def test_listing_does_not_query_per_document(
    api: Api, db: Session, migrated_engine: Engine
) -> None:
    statements: list[str] = []

    def record(_c: Any, _cur: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    make_document(db, original_filename="first.pdf")
    event.listen(migrated_engine, "before_cursor_execute", record)
    api.client.get(f"{API}/documents")
    with_one = len(statements)
    event.remove(migrated_engine, "before_cursor_execute", record)

    for index in range(10):
        add_result(db, make_document(db, original_filename=f"{index}.pdf"))
    statements.clear()
    event.listen(migrated_engine, "before_cursor_execute", record)
    response = api.client.get(f"{API}/documents")
    event.remove(migrated_engine, "before_cursor_execute", record)

    assert response.json()["total"] == 11
    assert len(statements) == with_one


def test_responses_never_expose_internal_fields(api: Api, db: Session) -> None:
    document = make_document(db, storage_path="/secret/location.pdf", task_id="secret-task")
    add_result(db, document)

    for path in ("", f"/{document.id}", f"/{document.id}/status", f"/{document.id}/results"):
        response = api.client.get(f"{API}/documents{path}")
        assert response.status_code == 200
        for hidden in ("storage_path", "secret", "task_id", "raw_response", "raw model output"):
            assert hidden not in response.text, (path, hidden)


# Detail and status


def test_detail_has_the_summary_and_counts(api: Api, db: Session) -> None:
    document = make_document(db, original_filename="a.pdf", page_count=5)
    old = add_result(db, document, make_invoice(total="999"))
    db.add(
        ValidationError(
            document_id=document.id,
            extraction_result_id=old.id,
            code="old_issue",
            severity=Severity.ERROR,
            message="From an old version.",
        )
    )
    current = add_result(db, document)
    for code in ("one", "two"):
        db.add(
            ValidationError(
                document_id=document.id,
                extraction_result_id=current.id,
                code=code,
                severity=Severity.WARNING,
                message=code,
            )
        )
    db.commit()
    add_page(db, document, 1)
    add_page(db, document, 2)
    other = make_document(db, original_filename="b.pdf")
    add_page(db, other, 1)

    response = api.client.get(f"{API}/documents/{document.id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == SUMMARY_KEYS | {"stored_page_count", "validation_error_count"}
    assert body["id"] == str(document.id)
    assert body["filename"] == "a.pdf"
    assert body["page_count"] == 5
    assert body["invoice_count"] == 1
    assert body["stored_page_count"] == 2
    assert body["validation_error_count"] == 2


def test_missing_document_is_404_with_a_plain_message(api: Api) -> None:
    missing = uuid.uuid4()

    for method, path in (
        ("GET", ""),
        ("GET", "/status"),
        ("GET", "/results"),
        ("GET", "/history"),
        ("GET", "/file"),
        ("GET", "/pages/1"),
        ("POST", "/reprocess"),
    ):
        response = api.client.request(method, f"{API}/documents/{missing}{path}")
        assert response.status_code == 404, path
        assert response.json()["detail"] in {"Document not found.", "Page not found."}


def test_malformed_id_is_422(api: Api) -> None:
    for method, path in (
        ("GET", ""),
        ("GET", "/status"),
        ("GET", "/results"),
        ("GET", "/history"),
        ("GET", "/file"),
        ("GET", "/pages/1"),
        ("POST", "/reprocess"),
    ):
        response = api.client.request(method, f"{API}/documents/not-a-uuid{path}")
        assert response.status_code == 422, path


def test_status_returns_only_the_polling_fields(api: Api, db: Session) -> None:
    document = make_document(
        db,
        status=DocumentStatus.PROCESSING,
        stage=ProcessingStage.TEXT_EXTRACTION,
        progress=42,
        needs_review=True,
    )

    response = api.client.get(f"{API}/documents/{document.id}/status")

    assert response.status_code == 200
    body = response.json()
    updated_at = body.pop("updated_at")
    assert updated_at
    assert body == {
        "id": str(document.id),
        "status": "processing",
        "stage": "text_extraction",
        "progress": 42,
        "needs_review": True,
        "failure_reason": None,
        "is_retryable": True,
    }


# Results


def test_results_return_fields_line_items_and_issues(api: Api, db: Session) -> None:
    document = make_document(db)
    invoice = make_invoice(
        total="12345678901234.5678",
        page_start=2,
        page_end=3,
        warnings=["Could not read the footer."],
        line_items=[
            line(1, "99.5", quantity="2", unit_price="49.75"),
            line(2, "0.5", source_page=3),
        ],
    )
    result = add_result(db, document, invoice)
    db.add(
        ValidationError(
            document_id=document.id,
            extraction_result_id=result.id,
            related_document_id=document.id,
            code="total_mismatch",
            severity=Severity.ERROR,
            field="total",
            message="Total differs.",
            expected_value="110.00",
            actual_value="12345678901234.5678",
        )
    )
    db.add(
        ValidationError(
            document_id=document.id,
            extraction_result_id=result.id,
            code="no_line_items",
            severity=Severity.INFO,
            message="Info.",
        )
    )
    db.commit()

    response = api.client.get(f"{API}/documents/{document.id}/results")

    assert response.status_code == 200
    [body] = response.json()
    assert body["id"] == str(result.id)
    assert body["invoice_index"] == 0
    assert body["version"] == 1
    assert (body["page_start"], body["page_end"]) == (2, 3)
    assert body["method"] == "llm"
    assert body["overall_confidence"] == pytest.approx(0.9)
    assert body["warnings"] == ["Could not read the footer."]
    assert body["llm_model"] == "test-model"
    assert body["prompt_version"] == "7"
    assert "raw_response" not in body

    assert set(body["fields"]) == {
        "invoice_number",
        "invoice_date",
        "supplier_name",
        "customer_name",
        "subtotal",
        "tax",
        "total",
        "currency",
    }
    assert body["fields"]["invoice_number"] == {
        "value": "INV-1",
        "confidence": 0.9,
        "reason": None,
        "source_snippet": "snippet INV-1",
        "source_page": 1,
    }
    assert body["fields"]["invoice_date"]["value"] == "2026-03-15"
    assert body["fields"]["subtotal"]["value"] == "100.00"
    # A JSON number could not hold this exactly.
    assert body["fields"]["total"]["value"] == "12345678901234.5678"

    assert [item["position"] for item in body["line_items"]] == [1, 2]
    first, second = body["line_items"]
    assert first["description"] == "Item 1"
    assert first["quantity"] == "2.00"
    assert first["unit_price"] == "49.75"
    assert first["amount"] == "99.50"
    assert first["confidence"] == 0.9
    assert first["source_snippet"] == "Item 1 line"
    assert second["source_page"] == 3
    assert second["quantity"] is None

    assert [issue["code"] for issue in body["validation_errors"]] == [
        "total_mismatch",
        "no_line_items",
    ]
    issue = body["validation_errors"][0]
    assert set(issue) == {
        "id",
        "code",
        "severity",
        "field",
        "message",
        "expected_value",
        "actual_value",
        "related_document_id",
        "is_resolved",
    }
    assert issue["severity"] == "error"
    assert issue["field"] == "total"
    assert issue["expected_value"] == "110.00"
    assert issue["related_document_id"] == str(document.id)
    assert issue["is_resolved"] is False


def test_unknown_field_is_null_with_its_reason(api: Api, db: Session) -> None:
    document = make_document(db)
    add_result(db, document, make_invoice(tax=None))

    [body] = api.client.get(f"{API}/documents/{document.id}/results").json()

    assert body["fields"]["tax"] == {
        "value": None,
        "confidence": 0.0,
        "reason": "Not found in the document.",
        "source_snippet": None,
        "source_page": None,
    }


def test_results_return_only_current_invoices_in_order(api: Api, db: Session) -> None:
    document = make_document(db)
    add_result(db, document, make_invoice(number="B", invoice_index=1))
    add_result(db, document, make_invoice(number="OLD", invoice_index=0))
    add_result(db, document, make_invoice(number="A", invoice_index=0))
    add_result(db, make_document(db), make_invoice(number="OTHER"))

    body = api.client.get(f"{API}/documents/{document.id}/results").json()

    assert [(r["invoice_index"], r["fields"]["invoice_number"]["value"]) for r in body] == [
        (0, "A"),
        (1, "B"),
    ]
    assert body[0]["version"] == 2


def test_document_without_results_returns_an_empty_list(api: Api, db: Session) -> None:
    document = make_document(db)

    response = api.client.get(f"{API}/documents/{document.id}/results")

    assert response.status_code == 200
    assert response.json() == []


# Pages


def test_page_returns_its_text_and_metadata(api: Api, db: Session) -> None:
    document = make_document(db)
    add_page(
        db,
        document,
        3,
        "Invoice text",
        sheet_name="Sheet1",
        source=TextSource.OCR,
        ocr_confidence=0.75,
        warnings=["Blurry."],
        table_data={"headers": ["secret-table"]},
    )

    response = api.client.get(f"{API}/documents/{document.id}/pages/3")

    assert response.status_code == 200
    assert response.json() == {
        "page_number": 3,
        "sheet_name": "Sheet1",
        "source": "ocr",
        "status": "ok",
        "ocr_confidence": 0.75,
        "processing_ms": 12,
        "error": None,
        "warnings": ["Blurry."],
        "text": "Invoice text",
    }


def test_failed_page_has_no_text(api: Api, db: Session) -> None:
    document = make_document(db)
    add_page(db, document, 1, None, source=None, status=PageStatus.FAILED, error="OCR timed out.")

    body = api.client.get(f"{API}/documents/{document.id}/pages/1").json()

    assert body["text"] is None
    assert body["source"] is None
    assert body["status"] == "failed"
    assert body["error"] == "OCR timed out."


def test_absent_page_is_404(api: Api, db: Session) -> None:
    document = make_document(db)
    other = make_document(db)
    add_page(db, other, 1)

    response = api.client.get(f"{API}/documents/{document.id}/pages/1")

    assert response.status_code == 404
    assert response.json()["detail"] == "Page not found."


@pytest.mark.parametrize("number", ["0", "-1", "abc", "99999999999999999999"])
def test_invalid_page_number_is_422(api: Api, db: Session, number: str) -> None:
    document = make_document(db)

    assert api.client.get(f"{API}/documents/{document.id}/pages/{number}").status_code == 422


# History


def test_history_is_in_order_and_includes_before_and_after(api: Api, db: Session) -> None:
    document = make_document(db)
    db.add_all(
        [
            ProcessingHistory(
                document_id=document.id,
                event=ProcessingEvent.STAGE_COMPLETED,
                stage=ProcessingStage.TEXT_EXTRACTION,
                attempt=1,
                duration_ms=250,
            ),
            ProcessingHistory(
                document_id=document.id,
                event=ProcessingEvent.MANUAL_CORRECTION,
                before={"total": "1.00"},
                after={"total": "2.00"},
                actor="alice",
                message="Corrected.",
            ),
        ]
    )
    db.commit()

    response = api.client.get(f"{API}/documents/{document.id}/history")

    assert response.status_code == 200
    first, second = response.json()
    assert first["event"] == "stage_completed"
    assert first["stage"] == "text_extraction"
    assert first["attempt"] == 1
    assert first["duration_ms"] == 250
    assert first["before"] is None
    assert first["actor"] == "system"
    assert second["event"] == "manual_correction"
    assert second["before"] == {"total": "1.00"}
    assert second["after"] == {"total": "2.00"}
    assert second["actor"] == "alice"
    assert second["message"] == "Corrected."
    assert first["id"] < second["id"]


def test_history_is_limited_to_the_newest_entries(api: Api, db: Session) -> None:
    document = make_document(db)
    db.add_all(
        ProcessingHistory(
            document_id=document.id,
            event=ProcessingEvent.STAGE_STARTED,
            message=str(index),
        )
        for index in range(1005)
    )
    db.commit()

    entries = api.client.get(f"{API}/documents/{document.id}/history").json()

    assert len(entries) == 1000
    assert [entries[0]["message"], entries[-1]["message"]] == ["5", "1004"]


# File


def stored_document(api: Api, db: Session, content: bytes = b"content", **fields: Any) -> Document:
    api.settings.storage_dir.mkdir(parents=True, exist_ok=True)
    path = api.settings.storage_dir / f"{uuid.uuid4()}.bin"
    path.write_bytes(content)
    return make_document(db, storage_path=str(path), **fields)


def test_file_serves_the_original_with_protective_headers(api: Api, db: Session) -> None:
    content = pdf_bytes()
    document = stored_document(api, db, content, original_filename="invoice.pdf")

    response = api.client.get(f"{API}/documents/{document.id}/file")

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["content-disposition"] == 'inline; filename="invoice.pdf"'
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("file_type", "mime", "disposition"),
    [
        (FileType.PDF, "application/pdf", "inline"),
        (FileType.JPEG, "image/jpeg", "inline"),
        (FileType.PNG, "image/png", "inline"),
        (FileType.TIFF, "image/tiff", "attachment"),
        (FileType.CSV, "text/csv", "attachment"),
        (FileType.XLS, "application/vnd.ms-excel", "attachment"),
        (
            FileType.XLSX,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "attachment",
        ),
    ],
)
def test_only_safe_previews_are_served_inline(
    api: Api, db: Session, file_type: FileType, mime: str, disposition: str
) -> None:
    document = stored_document(
        api, db, original_filename="a.dat", file_type=file_type, mime_type=mime
    )

    response = api.client.get(f"{API}/documents/{document.id}/file")

    assert response.headers["content-disposition"] == f'{disposition}; filename="a.dat"'
    assert response.headers["content-type"].split(";")[0] == mime


def test_file_encodes_a_non_ascii_filename(api: Api, db: Session) -> None:
    document = stored_document(api, db, original_filename="Rechnung März 発票.pdf")

    response = api.client.get(f"{API}/documents/{document.id}/file")

    assert response.status_code == 200
    expected = quote("Rechnung März 発票.pdf")
    assert response.headers["content-disposition"] == f"inline; filename*=utf-8''{expected}"


def test_uploaded_non_ascii_filename_round_trips_through_the_file_route(api: Api) -> None:
    content = pdf_bytes()
    [outcome] = api.upload(("Rechnung März.pdf", content)).json()["results"]
    assert outcome["filename"] == "Rechnung März.pdf"

    response = api.client.get(f"{API}/documents/{outcome['document']['id']}/file")

    assert response.content == content
    assert quote("Rechnung März.pdf") in response.headers["content-disposition"]


def test_file_header_cannot_be_injected_through_a_filename(api: Api, db: Session) -> None:
    document = stored_document(api, db, original_filename='a"\r\nX-Evil: yes.pdf')

    response = api.client.get(f"{API}/documents/{document.id}/file")

    assert "x-evil" not in response.headers
    assert "\n" not in response.headers["content-disposition"]


def test_file_outside_storage_is_not_served(api: Api, db: Session, tmp_path: Path) -> None:
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"private")
    document = make_document(db, storage_path=str(outside))

    response = api.client.get(f"{API}/documents/{document.id}/file")

    assert response.status_code == 404
    assert response.json() == {"detail": "The file is no longer available."}


def test_stored_path_climbing_out_of_storage_is_not_served(
    api: Api, db: Session, tmp_path: Path
) -> None:
    (tmp_path / "outside.pdf").write_bytes(b"private")
    api.settings.storage_dir.mkdir(parents=True)
    climbing = api.settings.storage_dir / ".." / "outside.pdf"
    document = make_document(db, storage_path=str(climbing))

    assert api.client.get(f"{API}/documents/{document.id}/file").status_code == 404


def test_symlink_out_of_storage_is_not_served(api: Api, db: Session, tmp_path: Path) -> None:
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"private")
    api.settings.storage_dir.mkdir(parents=True)
    link = api.settings.storage_dir / "link.pdf"
    os.symlink(outside, link)
    document = make_document(db, storage_path=str(link))

    assert api.client.get(f"{API}/documents/{document.id}/file").status_code == 404


def test_missing_file_is_404_without_revealing_the_path(api: Api, db: Session) -> None:
    path = api.settings.storage_dir / "gone.pdf"
    document = make_document(db, storage_path=str(path))

    response = api.client.get(f"{API}/documents/{document.id}/file")

    assert response.status_code == 404
    assert response.json() == {"detail": "The file is no longer available."}


def test_directory_in_storage_is_not_served(api: Api, db: Session) -> None:
    api.settings.storage_dir.mkdir(parents=True)
    document = make_document(db, storage_path=str(api.settings.storage_dir))

    assert api.client.get(f"{API}/documents/{document.id}/file").status_code == 404


# Reprocess


@pytest.mark.parametrize("status", [DocumentStatus.QUEUED, DocumentStatus.PROCESSING])
def test_reprocess_is_409_while_busy(api: Api, db: Session, status: DocumentStatus) -> None:
    document = make_document(db, status=status)

    response = api.client.post(f"{API}/documents/{document.id}/reprocess")

    assert response.status_code == 409
    assert response.json()["detail"] == "The document is already being processed."
    assert api.queue.calls == []
    assert history_of(db, document.id) == []


def test_reprocess_of_a_stalled_document_is_allowed(api: Api, db: Session) -> None:
    document = make_document(db, status=DocumentStatus.PROCESSING)
    set_idle_for(db, document, timedelta(seconds=api.settings.stalled_after_seconds + 60))

    response = api.client.post(f"{API}/documents/{document.id}/reprocess")

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert api.queue.document_ids == [str(document.id)]


def test_document_processing_within_the_stall_limit_is_still_busy(api: Api, db: Session) -> None:
    document = make_document(db, status=DocumentStatus.PROCESSING)
    set_idle_for(db, document, timedelta(seconds=api.settings.stalled_after_seconds - 60))

    assert api.client.post(f"{API}/documents/{document.id}/reprocess").status_code == 409


def test_reprocess_starts_over_by_default(api: Api, db: Session) -> None:
    document = make_document(
        db,
        status=DocumentStatus.FAILED,
        failure_reason="Broke.",
        page_count=2,
        needs_review=True,
        progress=80,
        text_extracted_at=datetime.now(UTC),
        task_id="old-task",
    )
    add_page(db, document)
    result = add_result(db, document)

    response = api.client.post(f"{API}/documents/{document.id}/reprocess")

    assert response.status_code == 202
    body = response.json()
    assert body["id"] == str(document.id)
    assert body["status"] == "queued"
    assert body["stage"] == "upload"
    assert body["progress"] == 0
    assert body["failure_reason"] is None
    assert body["needs_review"] is False

    db.expire_all()
    fresh = db.get(Document, document.id)
    assert fresh.page_count is None
    assert fresh.text_extracted_at is None
    assert fresh.task_id != "old-task"
    assert api.queue.calls[0]["task_id"] == fresh.task_id
    assert db.scalar(select(func.count()).select_from(DocumentPage)) == 0
    assert db.get(ExtractionResult, result.id).is_current is False
    [entry] = history_of(db, document.id)
    assert entry.event is ProcessingEvent.REPROCESS_REQUESTED
    assert entry.message == "Starting over."
    assert api.client.get(f"{API}/documents/{document.id}/results").json() == []


def test_reprocess_with_resume_keeps_the_work_already_done(api: Api, db: Session) -> None:
    extracted_at = datetime.now(UTC)
    document = make_document(
        db,
        status=DocumentStatus.FAILED,
        failure_reason="Broke.",
        page_count=2,
        progress=60,
        text_extracted_at=extracted_at,
    )
    add_page(db, document)
    add_result(db, document)

    response = api.client.post(f"{API}/documents/{document.id}/reprocess", params={"resume": True})

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["progress"] == 60
    db.expire_all()
    fresh = db.get(Document, document.id)
    assert fresh.page_count == 2
    assert fresh.text_extracted_at == extracted_at
    assert db.scalar(select(func.count()).select_from(DocumentPage)) == 1
    assert len(api.client.get(f"{API}/documents/{document.id}/results").json()) == 1
    assert history_of(db, document.id)[-1].message == "Resuming from the last completed work."


def test_completed_document_can_be_reprocessed(api: Api, db: Session) -> None:
    document = make_document(db)

    response = api.client.post(f"{API}/documents/{document.id}/reprocess")

    assert response.status_code == 202
    assert api.queue.document_ids == [str(document.id)]


@pytest.mark.parametrize("resume", [True, False])
def test_reprocess_with_the_queue_down_is_503(api: Api, db: Session, resume: bool) -> None:
    document = make_document(db, status=DocumentStatus.FAILED, failure_reason="Broke.")
    api.queue.fail = True

    response = api.client.post(
        f"{API}/documents/{document.id}/reprocess", params={"resume": resume}
    )

    assert response.status_code == 503
    assert response.json() == {"detail": QUEUE_UNAVAILABLE}
    db.expire_all()
    fresh = db.get(Document, document.id)
    assert fresh.status is DocumentStatus.FAILED
    assert fresh.failure_reason == QUEUE_UNAVAILABLE
    assert fresh.is_retryable is True


def test_simultaneous_reprocess_requests_queue_the_document_once(api: Api, db: Session) -> None:
    document = make_document(db, status=DocumentStatus.FAILED, failure_reason="Broke.")
    url = f"{API}/documents/{document.id}/reprocess"

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = sorted(pool.map(lambda _: api.client.post(url).status_code, range(6)))

    assert codes == [202, 409, 409, 409, 409, 409]
    assert len(api.queue.calls) == 1


def test_reprocess_failed_queues_every_retryable_failure(api: Api, db: Session) -> None:
    retryable = [
        make_document(db, status=DocumentStatus.FAILED, failure_reason="Broke.") for _ in range(3)
    ]
    add_page(db, retryable[0])
    add_result(db, retryable[0])
    make_document(db, status=DocumentStatus.FAILED, failure_reason="Permanent.", is_retryable=False)
    make_document(db, status=DocumentStatus.COMPLETED)
    make_document(db, status=DocumentStatus.QUEUED)

    response = api.client.post(f"{API}/documents/reprocess-failed")

    assert response.status_code == 202
    assert response.json() == {"queued": 3, "failed_to_queue": 0}
    assert set(api.queue.document_ids) == {str(document.id) for document in retryable}
    db.expire_all()
    assert all(db.get(Document, d.id).status is DocumentStatus.QUEUED for d in retryable)
    # Each one resumes, so earlier pages and results survive.
    assert db.scalar(select(func.count()).select_from(DocumentPage)) == 1
    assert len(api.client.get(f"{API}/documents/{retryable[0].id}/results").json()) == 1
    assert history_of(db, retryable[0].id)[-1].event is ProcessingEvent.REPROCESS_REQUESTED


def test_reprocess_failed_with_nothing_to_do(api: Api, db: Session) -> None:
    make_document(db)

    response = api.client.post(f"{API}/documents/reprocess-failed")

    assert response.status_code == 202
    assert response.json() == {"queued": 0, "failed_to_queue": 0}


def test_reprocess_failed_stops_when_the_queue_goes_down(api: Api, db: Session) -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    documents = [
        make_document(
            db,
            status=DocumentStatus.FAILED,
            failure_reason=f"Reason {index}.",
            created_at=moment + timedelta(days=index),
        )
        for index in range(4)
    ]
    api.queue.fail_after = 1

    response = api.client.post(f"{API}/documents/reprocess-failed")

    assert response.status_code == 202
    assert response.json() == {"queued": 1, "failed_to_queue": 3}
    db.expire_all()
    states = [db.get(Document, d.id) for d in documents]
    assert states[0].status is DocumentStatus.QUEUED
    assert states[1].status is DocumentStatus.FAILED
    assert states[1].failure_reason == QUEUE_UNAVAILABLE
    # Not attempted, so each keeps the real reason it failed.
    assert [s.failure_reason for s in states[2:]] == ["Reason 2.", "Reason 3."]
    assert all(s.status is DocumentStatus.FAILED for s in states[2:])


def test_upload_failure_can_be_retried_by_reprocess_failed(api: Api, db: Session) -> None:
    [outcome] = api.upload(("empty.pdf", b"")).json()["results"]

    response = api.client.post(f"{API}/documents/reprocess-failed")

    assert response.json() == {"queued": 1, "failed_to_queue": 0}
    assert api.queue.document_ids == [outcome["document"]["id"]]
