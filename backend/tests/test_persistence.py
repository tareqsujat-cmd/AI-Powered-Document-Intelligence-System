import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import ExtractionMethod, FileType
from app.db.models import Document, ExtractionResult
from app.extraction.normalize import duplicate_key
from app.extraction.persistence import invoice_from_result, save_invoice
from app.extraction.schemas import ExtractedField, InvoiceData, LineItemData

pytestmark = pytest.mark.db


def make_document(db: Session) -> Document:
    document = Document(
        original_filename="a.pdf",
        storage_path=f"/data/{uuid.uuid4()}",
        sha256="a" * 64,
        file_type=FileType.PDF,
        mime_type="application/pdf",
        size_bytes=1,
    )
    db.add(document)
    db.flush()
    return document


def make_invoice(**overrides: object) -> InvoiceData:
    values: dict[str, object] = {
        "invoice_number": ExtractedField[str](
            value="INV-1", confidence=0.9, source_snippet="Invoice INV-1", source_page=1
        ),
        "invoice_date": ExtractedField[date](value=date(2026, 3, 15), confidence=0.8),
        "supplier_name": ExtractedField[str](value="Northwind Ltd", confidence=0.7),
        "total": ExtractedField[Decimal](value=Decimal("110.50"), confidence=0.6),
        "subtotal": ExtractedField[Decimal](reason="Not on the invoice."),
        "line_items": [
            LineItemData(
                position=1,
                description="Widget",
                quantity=Decimal("2"),
                unit_price=Decimal("55.25"),
                amount=Decimal("110.50"),
                confidence=0.75,
                source_snippet="Widget 2 55.25 110.50",
                source_page=1,
            )
        ],
        "method": ExtractionMethod.LLM,
        "llm_model": "test-model",
        "prompt_version": "7",
    }
    return InvoiceData(**{**values, **overrides})


def test_round_trip_keeps_values_and_evidence(db: Session) -> None:
    document = make_document(db)
    saved = save_invoice(db, document, make_invoice())
    db.commit()
    db.expire_all()

    restored = invoice_from_result(db.get(ExtractionResult, saved.id))

    assert restored.invoice_number.value == "INV-1"
    assert restored.invoice_number.source_snippet == "Invoice INV-1"
    assert restored.invoice_number.source_page == 1
    assert restored.invoice_date.value == date(2026, 3, 15)
    assert restored.total.value == Decimal("110.50")
    assert restored.method is ExtractionMethod.LLM
    assert restored.prompt_version == "7"
    assert restored.line_items[0].description == "Widget"
    assert restored.line_items[0].amount == Decimal("110.50")
    assert restored.line_items[0].confidence == 0.75


def test_unknown_fields_stay_null_and_keep_their_reason(db: Session) -> None:
    document = make_document(db)
    saved = save_invoice(db, document, make_invoice())
    db.commit()
    db.expire_all()

    result = db.get(ExtractionResult, saved.id)
    restored = invoice_from_result(result)

    assert result.subtotal is None
    assert restored.subtotal.value is None
    assert restored.subtotal.reason == "Not on the invoice."
    assert restored.subtotal.confidence == 0.0


def test_stores_lookup_keys_and_overall_confidence(db: Session) -> None:
    document = make_document(db)
    saved = save_invoice(db, document, make_invoice())

    assert saved.invoice_key == "inv1"
    assert saved.duplicate_key == duplicate_key(
        "Northwind Ltd", "INV-1", date(2026, 3, 15), Decimal("110.50")
    )
    assert saved.overall_confidence == pytest.approx((0.9 + 0.8 + 0.7 + 0.6) / 4)


def test_keys_are_null_when_the_number_is_unknown(db: Session) -> None:
    document = make_document(db)
    saved = save_invoice(db, document, make_invoice(invoice_number=ExtractedField[str]()))

    assert saved.invoice_key is None
    assert saved.duplicate_key is None


def test_saving_again_creates_a_new_current_version_and_keeps_the_old(db: Session) -> None:
    document = make_document(db)
    first = save_invoice(db, document, make_invoice())
    second = save_invoice(db, document, make_invoice())
    db.commit()
    db.expire_all()

    assert (first.version, first.is_current) == (1, False)
    assert (second.version, second.is_current) == (2, True)


def test_invoices_in_one_document_are_versioned_separately(db: Session) -> None:
    document = make_document(db)
    save_invoice(db, document, make_invoice(invoice_index=0))
    save_invoice(db, document, make_invoice(invoice_index=1))
    save_invoice(db, document, make_invoice(invoice_index=1))
    db.commit()

    current = db.scalars(
        select(ExtractionResult)
        .where(ExtractionResult.is_current)
        .order_by(ExtractionResult.invoice_index)
    ).all()

    assert [(result.invoice_index, result.version) for result in current] == [(0, 1), (1, 2)]


def test_warnings_survive_storage(db: Session) -> None:
    document = make_document(db)
    saved = save_invoice(db, document, make_invoice(warnings=["A line item was discarded."]))
    db.commit()
    db.expire_all()

    assert invoice_from_result(db.get(ExtractionResult, saved.id)).warnings == [
        "A line item was discarded."
    ]


@pytest.mark.parametrize(
    ("name", "value"),
    [("invoice_number", "X" * 129), ("supplier_name", "X" * 513), ("currency", "EURO")],
)
def test_a_value_too_long_for_its_column_is_dropped_rather_than_failing_the_save(
    db: Session, name: str, value: str
) -> None:
    document = make_document(db)
    invoice = make_invoice(**{name: ExtractedField[str](value=value, confidence=0.9)})

    saved = save_invoice(db, document, invoice)
    db.commit()
    db.expire_all()

    restored = invoice_from_result(db.get(ExtractionResult, saved.id))
    assert restored.field(name).value is None
    assert restored.field(name).confidence == 0.0
    assert "characters" in restored.field(name).reason
    # The caller's object is not changed.
    assert invoice.field(name).value == value


def test_line_items_are_restored_in_order(db: Session) -> None:
    document = make_document(db)
    items = [LineItemData(position=position, description=f"{position}") for position in (2, 1, 3)]
    saved = save_invoice(db, document, make_invoice(line_items=items))
    db.commit()
    db.expire_all()

    restored = invoice_from_result(db.get(ExtractionResult, saved.id))

    assert [item.position for item in restored.line_items] == [1, 2, 3]
