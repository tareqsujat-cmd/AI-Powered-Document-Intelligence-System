import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import DocumentStatus, ProcessingEvent
from app.db.models import Document, ExtractionResult, LineItem, ProcessingHistory
from app.extraction.normalize import duplicate_key
from app.pipeline.review import refresh_needs_review
from tests.api_helpers import API, Api, add_result, line, make_document, make_invoice

pytestmark = pytest.mark.db
pytest_plugins = ["tests.api_helpers"]


def seed(db: Session, **invoice_fields: Any) -> tuple[Document, ExtractionResult]:
    """A processed document whose stored issues and review flag match its invoice."""
    document = make_document(db)
    result = add_result(db, document, make_invoice(**invoice_fields))
    refresh_needs_review(db, document)
    db.commit()
    return document, result


def patch(api: Api, document: Document, result: ExtractionResult, body: dict[str, Any]) -> Any:
    return api.client.patch(f"{API}/documents/{document.id}/results/{result.id}", json=body)


def corrections(db: Session, document: Document) -> list[ProcessingHistory]:
    return list(
        db.scalars(
            select(ProcessingHistory)
            .where(
                ProcessingHistory.document_id == document.id,
                ProcessingHistory.event == ProcessingEvent.MANUAL_CORRECTION,
            )
            .order_by(ProcessingHistory.id)
        )
    )


def stored(db: Session, result: ExtractionResult) -> ExtractionResult:
    db.expire_all()
    fresh = db.get(ExtractionResult, result.id)
    assert fresh is not None
    return fresh


def is_flagged(db: Session, document: Document) -> bool:
    db.expire_all()
    fresh = db.get(Document, document.id)
    assert fresh is not None
    return fresh.needs_review


def codes(body: dict[str, Any]) -> set[str]:
    return {issue["code"] for issue in body["validation_errors"]}


def test_correcting_a_wrong_total_removes_the_issue_and_clears_review(
    api: Api, db: Session
) -> None:
    document, result = seed(db, total="999.00")
    assert is_flagged(db, document) is True

    response = patch(api, document, result, {"total": "110.00"})

    assert response.status_code == 200
    body = response.json()
    assert body["fields"]["total"]["value"] == "110.00"
    assert body["validation_errors"] == []
    assert is_flagged(db, document) is False
    assert stored(db, result).total == Decimal("110.00")


def test_a_correction_that_stays_wrong_keeps_the_document_flagged(api: Api, db: Session) -> None:
    document, result = seed(db, total="999.00")

    body = patch(api, document, result, {"total": "500.00"}).json()

    [issue] = [i for i in body["validation_errors"] if i["code"] == "total_mismatch"]
    assert issue["expected_value"] == "110.00"
    assert issue["actual_value"] == "500.00"
    assert is_flagged(db, document) is True


def test_a_correction_can_introduce_a_new_issue(api: Api, db: Session) -> None:
    document, result = seed(db)
    assert is_flagged(db, document) is False

    body = patch(api, document, result, {"total": "111.00"}).json()

    assert "total_mismatch" in codes(body)
    assert is_flagged(db, document) is True


def test_correcting_one_invoice_keeps_flag_while_another_is_wrong(api: Api, db: Session) -> None:
    document = make_document(db)
    first = add_result(db, document, make_invoice(number="A", total="999", invoice_index=0))
    add_result(db, document, make_invoice(number="B", total="888", invoice_index=1))
    refresh_needs_review(db, document)
    db.commit()

    patch(api, document, first, {"total": "110.00"})

    assert is_flagged(db, document) is True


def test_history_records_before_after_and_actor_for_changed_fields_only(
    api: Api, db: Session
) -> None:
    document, result = seed(db, total="999.50")

    patch(
        api,
        document,
        result,
        {
            "total": "110.00",
            "invoice_date": "2026-04-01",
            "supplier_name": "Northwind Ltd",  # The same value it already has.
            "actor": "alice",
        },
    )

    [entry] = corrections(db, document)
    assert entry.before == {"total": "999.50", "invoice_date": "2026-03-15"}
    assert entry.after == {"total": "110.00", "invoice_date": "2026-04-01"}
    assert entry.actor == "alice"
    assert entry.attempt == 0


def test_correction_shows_up_in_the_history_route(api: Api, db: Session) -> None:
    document, result = seed(db, total="999.00")

    patch(api, document, result, {"total": "110.00"})

    [entry] = api.client.get(f"{API}/documents/{document.id}/history").json()
    assert entry["event"] == "manual_correction"
    assert entry["actor"] == "reviewer"
    assert entry["before"] == {"total": "999.00"}
    assert entry["after"] == {"total": "110.00"}


def test_corrected_field_gets_full_confidence_and_loses_its_old_evidence(
    api: Api, db: Session
) -> None:
    document, result = seed(db, confidence=0.3)

    body = patch(api, document, result, {"supplier_name": "Fixed Ltd", "actor": "alice"}).json()

    assert body["fields"]["supplier_name"] == {
        "value": "Fixed Ltd",
        "confidence": 1.0,
        "reason": "Corrected by alice.",
        "source_snippet": None,
        "source_page": None,
    }
    # Fields left alone keep their evidence.
    assert body["fields"]["total"]["confidence"] == 0.3
    assert body["fields"]["total"]["source_snippet"] == "snippet 110.00"
    fresh = stored(db, result)
    assert fresh.field_metadata["supplier_name"]["reason"] == "Corrected by alice."
    assert fresh.field_metadata["total"]["confidence"] == 0.3


def test_correcting_a_low_confidence_field_removes_its_issue(api: Api, db: Session) -> None:
    document, result = seed(db, confidence=0.3)
    before = api.client.get(f"{API}/documents/{document.id}/results").json()[0]
    assert {i["field"] for i in before["validation_errors"]} >= {"supplier_name", "total"}

    body = patch(api, document, result, {"supplier_name": "Fixed Ltd"}).json()

    low = {i["field"] for i in body["validation_errors"] if i["code"] == "low_confidence"}
    assert "supplier_name" not in low
    assert "total" in low


def test_explicit_null_clears_a_field_and_an_absent_field_is_untouched(
    api: Api, db: Session
) -> None:
    document, result = seed(db)

    response = patch(api, document, result, {"customer_name": None, "actor": "bob"})

    assert response.status_code == 200
    body = response.json()
    assert body["fields"]["customer_name"] == {
        "value": None,
        "confidence": 0.0,
        "reason": "Cleared by bob.",
        "source_snippet": None,
        "source_page": None,
    }
    assert body["fields"]["supplier_name"]["value"] == "Northwind Ltd"
    assert body["fields"]["total"]["value"] == "110.00"
    assert "missing_field" in codes(body)
    fresh = stored(db, result)
    assert fresh.customer_name is None
    assert fresh.supplier_name == "Northwind Ltd"
    [entry] = corrections(db, document)
    assert entry.before == {"customer_name": "Contoso Retail"}
    assert entry.after == {"customer_name": None}
    assert is_flagged(db, document) is True


@pytest.mark.parametrize(
    "body",
    [
        {"total": "110.00"},
        {"total": "110.0000"},
        {"total": 110},
        {"invoice_date": "2026-03-15", "supplier_name": "Northwind Ltd"},
        {"line_items": [{"description": "Item 1", "amount": "100"}]},
        {"tax": "10", "actor": "alice"},
    ],
)
def test_no_op_changes_nothing_and_writes_no_history(
    api: Api, db: Session, body: dict[str, Any]
) -> None:
    document, result = seed(db)
    before = stored(db, result)
    updated_at, metadata = before.updated_at, dict(before.field_metadata)
    line_ids = [item.id for item in before.line_items]

    response = patch(api, document, result, body)

    assert response.status_code == 200
    assert response.json()["fields"]["total"]["value"] == "110.00"
    assert corrections(db, document) == []
    after = stored(db, result)
    assert after.updated_at == updated_at
    assert after.field_metadata == metadata
    assert [item.id for item in after.line_items] == line_ids


def test_clearing_an_already_empty_field_is_a_no_op(api: Api, db: Session) -> None:
    document, result = seed(db, customer=None)
    reason = stored(db, result).field_metadata["customer_name"]["reason"]

    response = patch(api, document, result, {"customer_name": None})

    assert response.status_code == 200
    assert corrections(db, document) == []
    assert stored(db, result).field_metadata["customer_name"]["reason"] == reason


def test_stale_version_is_409(api: Api, db: Session) -> None:
    document = make_document(db)
    old = add_result(db, document)
    add_result(db, document)

    response = patch(api, document, old, {"total": "1.00"})

    assert response.status_code == 409
    assert "newer version" in response.json()["detail"]
    assert stored(db, old).total == Decimal("110.00")


def test_result_of_another_document_is_404(api: Api, db: Session) -> None:
    document = make_document(db)
    other, result = seed(db)

    response = patch(api, document, result, {"total": "1.00"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Result not found."}
    assert stored(db, result).total == Decimal("110.00")
    assert corrections(db, other) == []


def test_missing_document_or_result_is_404(api: Api, db: Session) -> None:
    document, result = seed(db)

    missing_result = api.client.patch(
        f"{API}/documents/{document.id}/results/{uuid.uuid4()}", json={"total": "1"}
    )
    missing_document = api.client.patch(
        f"{API}/documents/{uuid.uuid4()}/results/{result.id}", json={"total": "1"}
    )

    assert missing_result.status_code == 404
    assert missing_result.json() == {"detail": "Result not found."}
    assert missing_document.status_code == 404
    assert missing_document.json() == {"detail": "Document not found."}


def test_malformed_ids_are_422(api: Api, db: Session) -> None:
    document, result = seed(db)

    for path in (f"not-a-uuid/results/{result.id}", f"{document.id}/results/not-a-uuid"):
        assert api.client.patch(f"{API}/documents/{path}", json={"total": "1"}).status_code == 422


@pytest.mark.parametrize("status", [DocumentStatus.QUEUED, DocumentStatus.PROCESSING])
def test_busy_document_is_409(api: Api, db: Session, status: DocumentStatus) -> None:
    document, result = seed(db)
    document.status = status
    db.commit()

    response = patch(api, document, result, {"total": "1.00"})

    assert response.status_code == 409
    assert "being processed" in response.json()["detail"]
    assert stored(db, result).total == Decimal("110.00")


def test_failed_document_can_be_corrected(api: Api, db: Session) -> None:
    document, result = seed(db)
    document.status = DocumentStatus.FAILED
    document.failure_reason = "Indexing failed."
    db.commit()

    assert patch(api, document, result, {"total": "111.00"}).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"actor": "alice"},
        {"total": "abc"},
        {"total": "NaN"},
        {"total": "Infinity"},
        {"total": "-Infinity"},
        {"total": True},
        {"total": "1.23456"},
        {"total": "123456789012345"},
        {"total": "1e20"},
        {"subtotal": []},
        {"tax": {"value": 1}},
        {"invoice_date": "15/03/2026"},
        {"invoice_date": "2026-02-30"},
        {"invoice_date": 1773532800},
        {"currency": "usd"},
        {"currency": "US"},
        {"currency": "USDX"},
        {"currency": "U$D"},
        {"invoice_number": "x" * 129},
        {"invoice_number": ""},
        {"invoice_number": "   "},
        {"invoice_number": 123},
        {"invoice_number": "a\u0000b"},
        {"supplier_name": "x" * 513},
        {"customer_name": "x" * 513},
        {"supplier_name": "a\u0000b"},
        {"total": "1.00", "unknown_field": "x"},
        {"total": "1.00", "raw_response": {}},
        {"total": "1.00", "id": str(uuid.uuid4())},
        {"line_items": None},
        {"line_items": "none"},
        {"line_items": [{"description": "x", "confidence": 0.5}]},
        {"line_items": [{"amount": "NaN"}]},
        {"line_items": [{"quantity": "1.23456"}]},
        {"line_items": [{"description": "x" * 4001}]},
        {"total": "1.00", "actor": "x" * 129},
        {"total": "1.00", "actor": ""},
        {"total": "1.00", "actor": None},
    ],
)
def test_invalid_values_are_422_and_change_nothing(
    api: Api, db: Session, body: dict[str, Any]
) -> None:
    document, result = seed(db)

    response = patch(api, document, result, body)

    assert response.status_code == 422
    assert stored(db, result).total == Decimal("110.00")
    assert corrections(db, document) == []


@pytest.mark.parametrize(
    "content",
    [b'{"supplier_name": "bad \\ud800 text"}', b'{"bad \\ud800 key": "x"}'],
)
def test_unpaired_surrogate_is_422_not_a_crash(api: Api, db: Session, content: bytes) -> None:
    document, result = seed(db)

    response = api.client.patch(
        f"{API}/documents/{document.id}/results/{result.id}",
        content=content,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422


def test_validation_errors_name_the_field_without_echoing_the_input(api: Api, db: Session) -> None:
    document, result = seed(db)

    response = patch(api, document, result, {"total": "not-a-number-secret"})

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert set(error) == {"loc", "msg", "type"}
    assert "total" in error["loc"]
    assert "not-a-number-secret" not in response.text
    assert response.headers["content-type"] == "application/json"


def test_largest_allowed_values_are_stored_exactly(api: Api, db: Session) -> None:
    document, result = seed(db)

    response = patch(
        api,
        document,
        result,
        {
            "total": "99999999999999.9999",
            "invoice_number": "N" * 128,
            "supplier_name": "S" * 512,
            "customer_name": "C" * 512,
            "currency": "EUR",
            "actor": "a" * 128,
        },
    )

    assert response.status_code == 200
    assert response.json()["fields"]["total"]["value"] == "99999999999999.9999"
    fresh = stored(db, result)
    assert fresh.total == Decimal("99999999999999.9999")
    assert len(fresh.invoice_number) == 128
    assert fresh.currency == "EUR"


def test_money_accepts_a_json_number_without_float_noise(api: Api, db: Session) -> None:
    document, result = seed(db)

    patch(api, document, result, {"subtotal": 0.1})

    assert stored(db, result).subtotal == Decimal("0.1000")


def test_whitespace_around_text_is_removed(api: Api, db: Session) -> None:
    document, result = seed(db)

    patch(api, document, result, {"supplier_name": "  Fixed Ltd  "})

    assert stored(db, result).supplier_name == "Fixed Ltd"


def test_line_items_are_replaced_with_full_confidence(api: Api, db: Session) -> None:
    document, result = seed(db, line_items=[line(1, "60.00"), line(2, "40.00")])
    old_ids = {item.id for item in stored(db, result).line_items}

    response = patch(
        api,
        document,
        result,
        {
            "line_items": [
                {"description": "New A", "quantity": "2", "unit_price": "10", "amount": "20"},
                {"description": "New B", "amount": "50.00"},
                {"description": "New C", "amount": "30.00"},
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [(i["position"], i["description"], i["amount"]) for i in body["line_items"]] == [
        (1, "New A", "20.00"),
        (2, "New B", "50.00"),
        (3, "New C", "30.00"),
    ]
    first = body["line_items"][0]
    assert first["confidence"] == 1.0
    assert first["source_snippet"] is None
    assert first["source_page"] is None
    assert first["quantity"] == "2.00"
    assert body["validation_errors"] == []

    db.expire_all()
    rows = list(db.scalars(select(LineItem).where(LineItem.extraction_result_id == result.id)))
    assert len(rows) == 3
    assert not old_ids & {row.id for row in rows}

    [entry] = corrections(db, document)
    assert [item["amount"] for item in entry.before["line_items"]] == ["60.00", "40.00"]
    assert [item["amount"] for item in entry.after["line_items"]] == ["20.00", "50.00", "30.00"]


def test_empty_line_items_clears_them(api: Api, db: Session) -> None:
    document, result = seed(db)

    body = patch(api, document, result, {"line_items": []}).json()

    assert body["line_items"] == []
    assert "no_line_items" in codes(body)
    # No line items is informational, so this alone does not call for a review.
    assert is_flagged(db, document) is False


def test_line_items_that_stop_adding_up_are_flagged(api: Api, db: Session) -> None:
    document, result = seed(db)

    body = patch(api, document, result, {"line_items": [{"amount": "70.00"}]}).json()

    assert "line_items_subtotal_mismatch" in codes(body)
    assert is_flagged(db, document) is True


def test_correcting_line_items_clears_a_subtotal_mismatch(api: Api, db: Session) -> None:
    document, result = seed(db, line_items=[line(1, "70.00")])
    assert is_flagged(db, document) is True

    body = patch(api, document, result, {"line_items": [{"amount": "100.00"}]}).json()

    assert body["validation_errors"] == []
    assert is_flagged(db, document) is False


def test_correcting_only_line_items_keeps_invoice_field_evidence(api: Api, db: Session) -> None:
    document, result = seed(db)

    body = patch(api, document, result, {"line_items": [{"amount": "100.00"}]}).json()

    assert body["fields"]["total"]["confidence"] == 0.9
    assert body["fields"]["total"]["source_page"] == 1


def test_keys_and_overall_confidence_are_recomputed(api: Api, db: Session) -> None:
    document, result = seed(db, number="INV-OLD", confidence=0.5)

    body = patch(api, document, result, {"invoice_number": "INV-NEW 7"}).json()

    fresh = stored(db, result)
    assert fresh.invoice_key == "invnew7"
    assert fresh.duplicate_key == duplicate_key(
        "Northwind Ltd", "INV-NEW 7", date(2026, 3, 15), Decimal("110.00")
    )
    # Seven fields at 0.5 and one at 1.0.
    assert body["overall_confidence"] == pytest.approx((7 * 0.5 + 1.0) / 8)
    assert fresh.overall_confidence == pytest.approx((7 * 0.5 + 1.0) / 8)

    found = api.client.get(f"{API}/documents", params={"invoice_number": "inv new 7"}).json()
    gone = api.client.get(f"{API}/documents", params={"invoice_number": "INV-OLD"}).json()
    assert found["total"] == 1
    assert gone["total"] == 0


def test_clearing_the_number_clears_both_keys(api: Api, db: Session) -> None:
    document, result = seed(db)
    assert stored(db, result).duplicate_key is not None

    patch(api, document, result, {"invoice_number": None})

    fresh = stored(db, result)
    assert fresh.invoice_key is None
    assert fresh.duplicate_key is None


def test_overall_confidence_counts_only_fields_with_values(api: Api, db: Session) -> None:
    document, result = seed(db, confidence=0.5)
    every_field: dict[str, Any] = dict.fromkeys(
        (
            "invoice_number",
            "invoice_date",
            "supplier_name",
            "customer_name",
            "subtotal",
            "tax",
            "total",
            "currency",
        )
    )

    body = patch(api, document, result, every_field).json()

    assert body["overall_confidence"] is None
    assert stored(db, result).overall_confidence is None


def test_correction_detects_a_new_duplicate_and_then_removes_it(api: Api, db: Session) -> None:
    first, _ = seed(db, number="INV-1")
    second, result = seed(db, number="INV-2")
    assert is_flagged(db, second) is False

    made_duplicate = patch(api, second, result, {"invoice_number": "inv 1"}).json()

    [issue] = [i for i in made_duplicate["validation_errors"] if i["code"] == "duplicate_invoice"]
    assert issue["related_document_id"] == str(first.id)
    assert is_flagged(db, second) is True

    fixed = patch(api, second, result, {"invoice_number": "INV-2"}).json()

    assert "duplicate_invoice" not in codes(fixed)
    assert is_flagged(db, second) is False


def test_correcting_a_document_refreshes_those_that_cite_it(api: Api, db: Session) -> None:
    first, first_result = seed(db, number="INV-1")
    # The second sees the first as a duplicate when it is processed.
    second, _ = seed(db, number="INV-1")
    assert is_flagged(db, second) is True

    patch(api, first, first_result, {"invoice_number": "INV-9"})

    assert is_flagged(db, second) is False
    [body] = api.client.get(f"{API}/documents/{second.id}/results").json()
    assert body["validation_errors"] == []


def test_a_busy_document_that_cites_this_one_is_left_to_its_worker(api: Api, db: Session) -> None:
    first, first_result = seed(db, number="INV-1")
    second, _ = seed(db, number="INV-1")
    second.status = DocumentStatus.PROCESSING
    db.commit()

    patch(api, first, first_result, {"invoice_number": "INV-9"})

    assert is_flagged(db, second) is True


def test_extraction_warning_keeps_the_document_flagged_after_correction(
    api: Api, db: Session
) -> None:
    """Corrections do not touch extraction warnings. See the API notes: only reprocessing can."""
    document, result = seed(db, total="999.00", warnings=["Page 3 could not be read."])

    body = patch(api, document, result, {"total": "110.00"}).json()

    assert codes(body) == {"extraction_warning"}
    assert body["warnings"] == ["Page 3 could not be read."]
    assert is_flagged(db, document) is True


def test_response_matches_the_results_route(api: Api, db: Session) -> None:
    document, result = seed(db, total="999.00", customer=None)

    patched = patch(api, document, result, {"total": "1.00", "currency": "EUR"}).json()

    [listed] = api.client.get(f"{API}/documents/{document.id}/results").json()
    assert patched == listed


def test_sql_injection_in_a_correction_is_stored_as_text(api: Api, db: Session) -> None:
    document, result = seed(db)
    hostile = "x'; DROP TABLE documents; --"

    response = patch(api, document, result, {"supplier_name": hostile})

    assert response.status_code == 200
    assert stored(db, result).supplier_name == hostile
    assert api.client.get(f"{API}/documents").json()["total"] == 1


def test_filling_in_a_missing_field_removes_its_issue(api: Api, db: Session) -> None:
    document, result = seed(db, currency=None)
    assert is_flagged(db, document) is True

    body = patch(api, document, result, {"currency": "EUR"}).json()

    assert body["fields"]["currency"]["value"] == "EUR"
    assert body["validation_errors"] == []
    assert is_flagged(db, document) is False
