"""Real files through every step of Phase 2, with only the LLM's answers scripted."""

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pymupdf
import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.enums import ExtractionMethod, FileType, Severity
from app.db.models import Document
from app.extraction.extractor import extract_invoices
from app.extraction.llm_client import LLMResponse
from app.extraction.persistence import invoice_from_result, save_invoice
from app.processing.extract import extract_pages
from app.validation.cross_document import check_against_stored
from app.validation.issues import ValidationCode, ValidationIssue, needs_review
from app.validation.rules import validate_invoice

pytestmark = pytest.mark.db

TODAY = date(2026, 6, 1)


def field(value: Any) -> dict[str, Any]:
    return {"value": value, "confidence": 0.95 if value else 0.0, "source_snippet": None}


ANSWER = {
    "invoice_number": field("INV-500"),
    "invoice_date": field("2026-05-01"),
    "supplier_name": field("Northwind Traders"),
    "customer_name": field("Contoso Retail"),
    "subtotal": field("1,000.00"),
    "tax": field("100.00"),
    "total": field("1,100.00"),
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


class ScriptedLLM:
    model = "scripted"

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> LLMResponse:
        return LLMResponse(data=ANSWER)


def make_pdf(path: Path, total: str = "1,100.00") -> Path:
    lines = [
        "Invoice Number: INV-500",
        "Invoice Date: 2026-05-01",
        "Supplier: Northwind Traders",
        "Bill To: Contoso Retail",
        "Consulting services 2 500.00 1,000.00",
        "Subtotal 1,000.00",
        "Tax 100.00",
        f"Total GBP {total}",
    ]
    with pymupdf.open() as document:
        page = document.new_page()
        for index, line in enumerate(lines):
            page.insert_text((50, 60 + index * 18), line, fontsize=11)
        document.save(path)
    return path


def make_xlsx(path: Path, total: float = 1100.0) -> Path:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(
        [
            "Invoice No",
            "Invoice Date",
            "Supplier",
            "Customer",
            "Subtotal",
            "Tax",
            "Total",
            "Currency",
        ]
    )
    worksheet.append(
        [
            "inv 500",
            date(2026, 5, 1),
            "NORTHWIND TRADERS LTD",
            "Contoso Retail",
            1000.0,
            100.0,
            total,
            "GBP",
        ]
    )
    workbook.save(path)
    return path


def process(
    db: Session, path: Path, file_type: FileType, settings: Settings
) -> tuple[Any, list[ValidationIssue]]:
    """What the background pipeline will do, step by step."""
    document = Document(
        original_filename=path.name,
        storage_path=str(path),
        sha256=uuid.uuid4().hex,
        file_type=file_type,
        mime_type="x",
        size_bytes=1,
    )
    db.add(document)
    db.flush()

    run = extract_invoices(extract_pages(path, file_type, settings), ScriptedLLM(), settings)
    assert len(run.invoices) == 1
    invoice = run.invoices[0]
    saved = save_invoice(db, document, invoice)
    db.commit()
    issues = [
        *validate_invoice(invoice, today=TODAY),
        *check_against_stored(db, document.id, file_type, invoice, exclude_result_id=saved.id),
    ]
    return saved, issues


def blocking(issues: list[ValidationIssue]) -> list[ValidationCode]:
    return [issue.code for issue in issues if issue.severity is not Severity.INFO]


def test_a_clean_pdf_passes_every_check(db: Session, tmp_path: Path, settings: Settings) -> None:
    saved, issues = process(db, make_pdf(tmp_path / "a.pdf"), FileType.PDF, settings)

    assert blocking(issues) == []
    assert not needs_review(issues)
    assert saved.method is ExtractionMethod.LLM
    assert saved.total == Decimal("1100.0000")
    assert saved.line_items[0].description == "Consulting services"
    assert saved.field_metadata["total"]["source_page"] == 1


def test_a_clean_spreadsheet_passes_without_the_llm(
    db: Session, tmp_path: Path, settings: Settings
) -> None:
    saved, issues = process(db, make_xlsx(tmp_path / "a.xlsx"), FileType.XLSX, settings)

    assert saved.method is ExtractionMethod.RULE_BASED
    assert saved.invoice_date == date(2026, 5, 1)
    assert blocking(issues) == []


def test_the_same_invoice_uploaded_twice_is_flagged_as_a_duplicate(
    db: Session, tmp_path: Path, settings: Settings
) -> None:
    process(db, make_pdf(tmp_path / "first.pdf"), FileType.PDF, settings)

    _, issues = process(db, make_pdf(tmp_path / "second.pdf"), FileType.PDF, settings)

    assert blocking(issues) == [ValidationCode.DUPLICATE_INVOICE]
    assert needs_review(issues)


def test_pdf_and_excel_copies_that_agree_are_duplicates_across_formats(
    db: Session, tmp_path: Path, settings: Settings
) -> None:
    process(db, make_pdf(tmp_path / "a.pdf"), FileType.PDF, settings)

    _, issues = process(db, make_xlsx(tmp_path / "a.xlsx"), FileType.XLSX, settings)

    assert blocking(issues) == [ValidationCode.DUPLICATE_INVOICE]


def test_pdf_and_excel_copies_that_disagree_are_flagged_with_both_values(
    db: Session, tmp_path: Path, settings: Settings
) -> None:
    pdf_saved, _ = process(db, make_pdf(tmp_path / "a.pdf"), FileType.PDF, settings)

    _, issues = process(db, make_xlsx(tmp_path / "a.xlsx", total=1150.0), FileType.XLSX, settings)

    mismatches = [issue for issue in issues if issue.code is ValidationCode.PDF_EXCEL_MISMATCH]
    assert [issue.field for issue in mismatches] == ["total"]
    assert mismatches[0].actual_value == "1150.00"
    assert mismatches[0].expected_value == "1100.00"
    assert mismatches[0].related_document_id == pdf_saved.document_id
    # Its own arithmetic is wrong too, and both problems are reported.
    assert ValidationCode.TOTAL_MISMATCH in {issue.code for issue in issues}


def test_a_stored_invoice_validates_the_same_as_a_fresh_one(
    db: Session, tmp_path: Path, settings: Settings
) -> None:
    saved, fresh_issues = process(db, make_pdf(tmp_path / "a.pdf"), FileType.PDF, settings)
    db.expire_all()

    restored = invoice_from_result(saved)

    assert blocking(validate_invoice(restored, today=TODAY)) == blocking(fresh_issues)
