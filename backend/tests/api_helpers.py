"""Shared setup for API tests: a real database, with a fake queue and a fake Redis.

Test modules load the fixtures here with `pytest_plugins = ["tests.api_helpers"]`. They also import
from it first, too late for assertion rewriting. This file has no assertions, so it opts out with
PYTEST_DONT_REWRITE, which avoids a warning.
"""

import io
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import openpyxl
import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from msoffcrypto.format.ooxml import OOXMLFile
from PIL import Image
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.rate_limit import get_rate_limit_redis
from app.core.config import Settings, get_settings
from app.db.enums import DocumentStatus, ExtractionMethod, FileType, ProcessingStage
from app.db.models import Document, ExtractionResult
from app.db.session import get_db
from app.extraction.persistence import save_invoice
from app.extraction.schemas import ExtractedField, InvoiceData, LineItemData
from app.main import create_app
from app.worker.celery_app import celery_app

API = "/api/v1"


class FakeQueue:
    """Stands in for celery_app.send_task, so that no test can reach a real broker."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = False
        self.fail_after: int | None = None

    def __call__(self, name: str, *, args: list[str], task_id: str, retry: bool) -> None:
        if self.fail or (self.fail_after is not None and len(self.calls) >= self.fail_after):
            raise ConnectionError("redis://secret-host:6379 refused the connection")
        self.calls.append({"name": name, "args": args, "task_id": task_id, "retry": retry})

    @property
    def document_ids(self) -> list[str]:
        return [call["args"][0] for call in self.calls]


class _FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.results: list[int | bool] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def incr(self, key: str) -> None:
        self.redis.counts[key] = self.redis.counts.get(key, 0) + 1
        self.results.append(self.redis.counts[key])

    def expire(self, key: str, seconds: int) -> None:
        self.redis.expiries[key] = seconds
        self.results.append(True)

    def execute(self) -> list[int | bool]:
        return self.results


@dataclass
class FakeRedis:
    counts: dict[str, int] = field(default_factory=dict)
    expiries: dict[str, int] = field(default_factory=dict)

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


class ExhaustedRedis(FakeRedis):
    """Every client is already far over every limit."""

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        pipeline = _FakePipeline(self)
        pipeline.results.append(10**9)
        return pipeline


class UnreachableRedis:
    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        raise RedisConnectionError("redis://secret-host:6379 is unreachable")


@dataclass
class Api:
    client: TestClient
    app: FastAPI
    settings: Settings
    queue: FakeQueue
    redis: FakeRedis

    def upload(self, *files: tuple[str, bytes]) -> Any:
        parts = [("files", (name, content)) for name, content in files]
        return self.client.post(f"{API}/documents", files=parts)

    def stored_files(self) -> list[Path]:
        if not self.settings.storage_dir.exists():
            return []
        return sorted(self.settings.storage_dir.iterdir())


@pytest.fixture
def make_api(
    migrated_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Api]]:
    """Builds an API on a real, migrated database, with any settings a test wants to change."""
    queue = FakeQueue()
    monkeypatch.setattr(celery_app, "send_task", queue)
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    clients: list[TestClient] = []

    def override_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    def build(**overrides: Any) -> Api:
        values: dict[str, Any] = {
            "storage_dir": tmp_path / "uploads",
            "cors_origins": ["http://allowed.test"],
        }
        settings = Settings(_env_file=None, **{**values, **overrides})
        app = create_app(settings)
        redis = FakeRedis()
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_rate_limit_redis] = lambda: redis
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return Api(client=client, app=app, settings=settings, queue=queue, redis=redis)

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def api(make_api: Callable[..., Api]) -> Api:
    return make_api()


def make_document(db: Session, **overrides: Any) -> Document:
    values: dict[str, Any] = {
        "original_filename": "invoice.pdf",
        "storage_path": f"/data/uploads/{uuid.uuid4()}.pdf",
        "sha256": "a" * 64,
        "file_type": FileType.PDF,
        "mime_type": "application/pdf",
        "size_bytes": 1000,
        "status": DocumentStatus.COMPLETED,
        "stage": ProcessingStage.FINISHED,
        "progress": 100,
    }
    document = Document(**{**values, **overrides})
    db.add(document)
    db.commit()
    return document


def line(position: int = 1, amount: str | None = "100.00", **overrides: Any) -> LineItemData:
    values: dict[str, Any] = {
        "position": position,
        "description": f"Item {position}",
        "amount": None if amount is None else Decimal(amount),
        "confidence": 0.9,
        "source_snippet": f"Item {position} line",
        "source_page": 1,
    }
    return LineItemData(**{**values, **overrides})


def make_invoice(
    *,
    number: str | None = "INV-1",
    supplier: str | None = "Northwind Ltd",
    customer: str | None = "Contoso Retail",
    invoice_date: date | None = date(2026, 3, 15),
    subtotal: str | None = "100.00",
    tax: str | None = "10.00",
    total: str | None = "110.00",
    currency: str | None = "USD",
    confidence: float = 0.9,
    **overrides: Any,
) -> InvoiceData:
    """A clean invoice unless told otherwise: it passes every validation rule."""

    def found(value: Any) -> dict[str, Any]:
        if value is None:
            return {"reason": "Not found in the document."}
        return {
            "value": value,
            "confidence": confidence,
            "source_snippet": f"snippet {value}",
            "source_page": 1,
        }

    def money(value: str | None) -> Decimal | None:
        return None if value is None else Decimal(value)

    values: dict[str, Any] = {
        "invoice_number": ExtractedField[str](**found(number)),
        "invoice_date": ExtractedField[date](**found(invoice_date)),
        "supplier_name": ExtractedField[str](**found(supplier)),
        "customer_name": ExtractedField[str](**found(customer)),
        "subtotal": ExtractedField[Decimal](**found(money(subtotal))),
        "tax": ExtractedField[Decimal](**found(money(tax))),
        "total": ExtractedField[Decimal](**found(money(total))),
        "currency": ExtractedField[str](**found(currency)),
        "line_items": [line()],
        "method": ExtractionMethod.LLM,
        "llm_model": "test-model",
        "prompt_version": "7",
        "raw_response": {"secret": "raw model output"},
    }
    return InvoiceData(**{**values, **overrides})


def add_result(
    db: Session, document: Document, invoice: InvoiceData | None = None
) -> ExtractionResult:
    result = save_invoice(db, document, invoice or make_invoice())
    db.commit()
    return result


def pdf_bytes(text: str = "Invoice INV-1") -> bytes:
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 60), text)
        return document.tobytes()


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (20, 20), color=255).save(buffer, format="PNG")
    return buffer.getvalue()


def xlsx_bytes() -> bytes:
    buffer = io.BytesIO()
    workbook = openpyxl.Workbook()
    workbook.active.append(["Description", "Amount"])
    workbook.active.append(["Widget", 100])
    workbook.save(buffer)
    return buffer.getvalue()


def encrypted_xlsx_bytes() -> bytes:
    encrypted = io.BytesIO()
    OOXMLFile(io.BytesIO(xlsx_bytes())).encrypt("secret", encrypted)
    return encrypted.getvalue()


CSV_BYTES = b"Description,Amount\nWidget,100\n"
# A Windows executable header. Its name is the disguise.
EXECUTABLE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" * 20
