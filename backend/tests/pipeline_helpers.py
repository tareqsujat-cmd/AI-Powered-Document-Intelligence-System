"""Fakes and builders for tests that run documents through the pipeline."""

import hashlib
import math
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pymupdf
from sqlalchemy.orm import Session

from app.db.enums import FileType
from app.db.models import Document
from app.extraction.llm_client import LLMResponse
from app.processing.file_validation import MIME_TYPES


class FakeEmbedder:
    """A bag of hashed words: texts sharing words are similar, with no model to load."""

    model_name = "fake-embedder"

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * 384
        for word in re.findall(r"\w+", text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % 384] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def field(value: Any, confidence: float = 0.95) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": confidence if value is not None else 0.0,
        "reason": None if value is not None else "Not in the document.",
        "source_snippet": None,
    }


def invoice_answer(number: str = "INV-500", total: str = "1,100.00") -> dict[str, Any]:
    return {
        "invoice_number": field(number),
        "invoice_date": field("2026-05-01"),
        "supplier_name": field("Northwind Traders"),
        "customer_name": field("Contoso Retail"),
        "subtotal": field("1,000.00"),
        "tax": field("100.00"),
        "total": field(total),
        "currency": field("GBP"),
        "line_items": [
            {
                "description": "Consulting services",
                "quantity": "2",
                "unit_price": "500.00",
                "amount": "1,000.00",
                "confidence": 0.9,
            }
        ],
    }


class FakeLLM:
    """Answers from a script: a dict to return, or an exception to raise. The last one repeats."""

    model = "fake-llm"

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers) or [invoice_answer()]
        self.calls = 0

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> LLMResponse:
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return LLMResponse(data=answer)


def invoice_lines(number: str = "INV-500", total: str = "1,100.00") -> list[str]:
    return [
        f"Invoice Number: {number}",
        "Invoice Date: 2026-05-01",
        "Supplier: Northwind Traders",
        "Bill To: Contoso Retail",
        "Consulting services 2 500.00 1,000.00",
        "Subtotal 1,000.00",
        "Tax 100.00",
        f"Total GBP {total}",
    ]


def make_pdf(path: Path, pages: Sequence[Sequence[str]] | None = None) -> Path:
    with pymupdf.open() as document:
        for lines in pages if pages is not None else [invoice_lines()]:
            page = document.new_page()
            for index, line in enumerate(lines):
                page.insert_text((50, 60 + index * 18), line, fontsize=11)
        document.save(path)
    return path


def make_document(
    db: Session, path: Path, file_type: FileType = FileType.PDF, **overrides: Any
) -> Document:
    values: dict[str, Any] = {
        "original_filename": path.name,
        "storage_path": str(path),
        "sha256": uuid.uuid4().hex + uuid.uuid4().hex,
        "file_type": file_type,
        "mime_type": MIME_TYPES[file_type],
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }
    document = Document(**{**values, **overrides})
    db.add(document)
    db.commit()
    return document
