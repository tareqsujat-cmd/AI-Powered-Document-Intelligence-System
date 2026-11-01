import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.db.enums import FileType, Severity
from app.db.models import Document
from app.extraction.persistence import save_invoice
from app.extraction.schemas import ExtractedField, InvoiceData, LineItemData
from app.validation.cross_document import (
    MAX_RELATED_RESULTS,
    check_against_stored,
    compare_invoices,
)
from app.validation.issues import ValidationCode, ValidationIssue, needs_review

OTHER_ID = uuid.uuid4()
PDF_PAIR = (FileType.PDF, FileType.PDF)


def known(value: Any) -> ExtractedField[Any]:
    return ExtractedField[Any](value=value, confidence=0.95)


def amount(text: str) -> ExtractedField[Any]:
    return known(Decimal(text))


def unknown() -> ExtractedField[Any]:
    return ExtractedField[Any]()


def line(position: int, line_amount: str | None) -> LineItemData:
    return LineItemData(
        position=position,
        description=f"Item {position}",
        amount=None if line_amount is None else Decimal(line_amount),
        confidence=0.9,
    )


def make_invoice(**overrides: Any) -> InvoiceData:
    values: dict[str, Any] = {
        "invoice_number": known("INV-1"),
        "invoice_date": known(date(2026, 3, 15)),
        "supplier_name": known("Northwind Ltd"),
        "customer_name": known("Contoso GmbH"),
        "subtotal": amount("100.00"),
        "tax": amount("10.00"),
        "total": amount("110.00"),
        "currency": known("USD"),
        "line_items": [line(1, "60.00"), line(2, "40.00")],
    }
    return InvoiceData(**{**values, **overrides})


def compare(first: InvoiceData, second: InvoiceData) -> list[ValidationIssue]:
    return compare_invoices(first, second, kinds=PDF_PAIR, related_document_id=OTHER_ID)


def fields(issues: list[ValidationIssue]) -> list[str | None]:
    return [issue.field for issue in issues]


# compare_invoices


def test_identical_invoices_have_no_differences() -> None:
    assert compare(make_invoice(), make_invoice()) == []


@pytest.mark.parametrize(
    ("name", "mine", "theirs", "actual", "expected"),
    [
        ("subtotal", amount("100.00"), amount("90.00"), "100.00", "90.00"),
        ("tax", amount("10.00"), amount("12.50"), "10.00", "12.50"),
        ("total", amount("110.00"), amount("110.02"), "110.00", "110.02"),
        (
            "invoice_date",
            known(date(2026, 3, 15)),
            known(date(2026, 3, 16)),
            "2026-03-15",
            "2026-03-16",
        ),
        ("supplier_name", known("Northwind Ltd"), known("Southwind Ltd"), None, None),
        ("customer_name", known("Contoso GmbH"), known("Fabrikam GmbH"), None, None),
        ("currency", known("USD"), known("EUR"), "USD", "EUR"),
    ],
)
def test_each_differing_field_is_an_error_naming_both_values(
    name: str, mine: Any, theirs: Any, actual: str | None, expected: str | None
) -> None:
    issues = compare(make_invoice(**{name: mine}), make_invoice(**{name: theirs}))

    assert len(issues) == 1
    issue = issues[0]
    assert issue.code is ValidationCode.PDF_PDF_MISMATCH
    assert issue.severity is Severity.ERROR
    assert issue.field == name
    assert issue.actual_value == (actual or mine.value)
    assert issue.expected_value == (expected or theirs.value)
    assert issue.related_document_id == OTHER_ID
    assert issue.actual_value in issue.message and issue.expected_value in issue.message


@pytest.mark.parametrize("other_total", ["110.01", "109.99", "110.0000"])
def test_amounts_within_tolerance_are_equal(other_total: str) -> None:
    assert compare(make_invoice(), make_invoice(total=amount(other_total))) == []


@pytest.mark.parametrize("other_total", ["110.02", "109.98"])
def test_amounts_beyond_tolerance_differ(other_total: str) -> None:
    assert fields(compare(make_invoice(), make_invoice(total=amount(other_total)))) == ["total"]


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        ("Northwind Ltd", "NORTHWIND LIMITED"),
        ("Northwind, Inc.", "northwind"),
        ("Müller GmbH", "Muller"),
    ],
)
def test_party_names_are_compared_after_normalizing(mine: str, theirs: str) -> None:
    first = make_invoice(supplier_name=known(mine), customer_name=known(mine))
    second = make_invoice(supplier_name=known(theirs), customer_name=known(theirs))

    assert compare(first, second) == []


def test_currency_comparison_is_exact() -> None:
    assert fields(compare(make_invoice(currency=known("usd")), make_invoice())) == ["currency"]


@pytest.mark.parametrize(
    "name",
    [
        "invoice_date",
        "supplier_name",
        "customer_name",
        "subtotal",
        "tax",
        "total",
        "currency",
    ],
)
def test_unknown_versus_known_is_not_a_mismatch(name: str) -> None:
    with_gap = make_invoice(**{name: unknown()})

    assert compare(with_gap, make_invoice()) == []
    assert compare(make_invoice(), with_gap) == []


def test_name_made_only_of_a_legal_suffix_is_treated_as_unknown() -> None:
    assert compare(make_invoice(supplier_name=known("Ltd")), make_invoice()) == []


def test_zero_is_known_and_compared_with_a_nonzero_value() -> None:
    issues = compare(make_invoice(tax=amount("0")), make_invoice())

    assert [(i.field, i.actual_value, i.expected_value) for i in issues] == [
        ("tax", "0.00", "10.00")
    ]


@pytest.mark.parametrize(
    ("kinds", "code"),
    [
        ((FileType.PDF, FileType.PDF), ValidationCode.PDF_PDF_MISMATCH),
        ((FileType.PDF, FileType.XLSX), ValidationCode.PDF_EXCEL_MISMATCH),
        ((FileType.XLS, FileType.PDF), ValidationCode.PDF_EXCEL_MISMATCH),
        ((FileType.PDF, FileType.CSV), ValidationCode.PDF_EXCEL_MISMATCH),
        ((FileType.CSV, FileType.PDF), ValidationCode.PDF_EXCEL_MISMATCH),
        ((FileType.PDF, FileType.PNG), ValidationCode.CROSS_DOCUMENT_MISMATCH),
        ((FileType.JPEG, FileType.PDF), ValidationCode.CROSS_DOCUMENT_MISMATCH),
        ((FileType.TIFF, FileType.XLSX), ValidationCode.CROSS_DOCUMENT_MISMATCH),
        ((FileType.XLSX, FileType.CSV), ValidationCode.CROSS_DOCUMENT_MISMATCH),
        ((FileType.PNG, FileType.PNG), ValidationCode.CROSS_DOCUMENT_MISMATCH),
    ],
)
def test_code_depends_on_the_kinds_of_file(
    kinds: tuple[FileType, FileType], code: ValidationCode
) -> None:
    issues = compare_invoices(
        make_invoice(), make_invoice(total=amount("1.00")), kinds=kinds, related_document_id=None
    )

    assert [issue.code for issue in issues] == [code]
    assert kinds[0].value.upper() in issues[0].message
    assert kinds[1].value.upper() in issues[0].message


def test_different_line_counts_compare_counts_and_sums_but_not_positions() -> None:
    other = make_invoice(line_items=[line(1, "60.00"), line(2, "30.00"), line(3, "20.00")])

    issues = compare(make_invoice(), other)

    assert [(i.field, i.actual_value, i.expected_value) for i in issues] == [
        ("line_items", "2", "3"),
        ("line_items.sum", "100.00", "110.00"),
    ]


def test_equal_counts_report_the_amount_at_each_differing_position() -> None:
    other = make_invoice(line_items=[line(1, "60.00"), line(2, "45.00")])

    issues = compare(make_invoice(), other)

    assert [(i.field, i.actual_value, i.expected_value) for i in issues] == [
        ("line_items.sum", "100.00", "105.00"),
        ("line_items[2].amount", "40.00", "45.00"),
    ]


def test_positions_are_paired_in_order_not_in_list_order() -> None:
    reversed_lines = make_invoice(line_items=[line(2, "40.00"), line(1, "60.00")])

    assert compare(make_invoice(), reversed_lines) == []
    assert compare(reversed_lines, make_invoice()) == []


def test_line_amounts_within_tolerance_are_equal() -> None:
    other = make_invoice(line_items=[line(1, "60.01"), line(2, "40.00")])

    assert compare(make_invoice(), other) == []


def test_position_mismatch_can_exist_with_equal_sums() -> None:
    other = make_invoice(line_items=[line(1, "40.00"), line(2, "60.00")])

    assert fields(compare(make_invoice(), other)) == [
        "line_items[1].amount",
        "line_items[2].amount",
    ]


def test_incomplete_lines_skip_the_sum_but_compare_known_positions() -> None:
    other = make_invoice(line_items=[line(1, None), line(2, "45.00")])

    assert fields(compare(make_invoice(), other)) == ["line_items[2].amount"]


def test_lines_on_only_one_side_are_not_compared() -> None:
    assert compare(make_invoice(), make_invoice(line_items=[])) == []
    assert compare(make_invoice(line_items=[]), make_invoice()) == []


def test_comparing_does_not_change_either_invoice() -> None:
    first, second = make_invoice(), make_invoice(total=amount("1.00"), line_items=[line(9, "1")])
    before = (first.model_dump(), second.model_dump())

    compare(first, second)

    assert (first.model_dump(), second.model_dump()) == before


# check_against_stored


def make_document(db: Session, file_type: FileType = FileType.PDF) -> Document:
    document = Document(
        original_filename="a",
        storage_path=f"/data/{uuid.uuid4()}",
        sha256="a" * 64,
        file_type=file_type,
        mime_type="application/octet-stream",
        size_bytes=1,
    )
    db.add(document)
    db.flush()
    return document


def store(db: Session, invoice: InvoiceData, file_type: FileType = FileType.PDF) -> Document:
    document = make_document(db, file_type)
    save_invoice(db, document, invoice)
    return document


def check(
    db: Session,
    invoice: InvoiceData,
    file_type: FileType = FileType.PDF,
    document_id: uuid.UUID | None = None,
    exclude_result_id: uuid.UUID | None = None,
) -> list[ValidationIssue]:
    return check_against_stored(
        db,
        document_id or uuid.uuid4(),
        file_type,
        invoice,
        exclude_result_id=exclude_result_id,
    )


@contextmanager
def captured_statements(db: Session) -> Iterator[list[tuple[str, Any]]]:
    statements: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        statements.append((statement, parameters))

    connection = db.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(connection, "before_cursor_execute", capture)


@pytest.mark.db
def test_nothing_stored_means_no_issues(db: Session) -> None:
    assert check(db, make_invoice()) == []


@pytest.mark.db
def test_duplicate_across_two_documents_is_an_error(db: Session) -> None:
    stored = store(db, make_invoice())

    issues = check(db, make_invoice())

    assert [(i.code, i.severity, i.related_document_id, i.actual_value) for i in issues] == [
        (ValidationCode.DUPLICATE_INVOICE, Severity.ERROR, stored.id, "INV-1")
    ]
    assert "another document" in issues[0].message


@pytest.mark.db
@pytest.mark.parametrize(
    ("number", "supplier", "total"),
    [
        ("inv 1", "Northwind Ltd", "110.00"),
        ("INV-1", "Northwind Limited", "110.00"),
        ("inv-1", "NORTHWIND LIMITED.", "110.0000"),
        ("INV1", "northwind", "110"),
    ],
)
def test_formatting_variants_are_still_duplicates(
    db: Session, number: str, supplier: str, total: str
) -> None:
    stored = store(db, make_invoice())
    variant = make_invoice(
        invoice_number=known(number), supplier_name=known(supplier), total=amount(total)
    )

    issues = check(db, variant)

    assert [(i.code, i.related_document_id) for i in issues] == [
        (ValidationCode.DUPLICATE_INVOICE, stored.id)
    ]


@pytest.mark.db
def test_duplicate_is_not_also_compared_for_mismatches(db: Session) -> None:
    store(db, make_invoice(subtotal=amount("1.00"), tax=amount("2.00"), currency=known("EUR")))

    issues = check(db, make_invoice())

    assert [issue.code for issue in issues] == [ValidationCode.DUPLICATE_INVOICE]


@pytest.mark.db
def test_each_duplicate_is_reported(db: Session) -> None:
    first, second = store(db, make_invoice()), store(db, make_invoice())

    issues = check(db, make_invoice())

    assert {issue.related_document_id for issue in issues} == {first.id, second.id}
    assert {issue.code for issue in issues} == {ValidationCode.DUPLICATE_INVOICE}


@pytest.mark.db
def test_pdf_against_pdf_mismatch(db: Session) -> None:
    stored = store(db, make_invoice(total=amount("120.00")))

    issues = check(db, make_invoice())

    assert [
        (i.code, i.severity, i.field, i.actual_value, i.expected_value, i.related_document_id)
        for i in issues
    ] == [
        (
            ValidationCode.PDF_PDF_MISMATCH,
            Severity.ERROR,
            "total",
            "110.00",
            "120.00",
            stored.id,
        )
    ]


@pytest.mark.db
@pytest.mark.parametrize("stored_type", [FileType.XLSX, FileType.XLS, FileType.CSV])
def test_pdf_against_spreadsheet_mismatch(db: Session, stored_type: FileType) -> None:
    stored = store(db, make_invoice(total=amount("120.00")), stored_type)

    issues = check(db, make_invoice())

    assert [(i.code, i.field, i.related_document_id) for i in issues] == [
        (ValidationCode.PDF_EXCEL_MISMATCH, "total", stored.id)
    ]


@pytest.mark.db
@pytest.mark.parametrize("stored_type", [FileType.XLSX, FileType.XLS, FileType.CSV])
def test_a_spreadsheet_that_agrees_with_a_pdf_is_verified_rather_than_a_duplicate(
    db: Session, stored_type: FileType
) -> None:
    stored = store(db, make_invoice(), stored_type)

    issues = check(db, make_invoice())

    assert [(i.code, i.severity, i.related_document_id) for i in issues] == [
        (ValidationCode.CROSS_DOCUMENT_VERIFIED, Severity.INFO, stored.id)
    ]
    assert not needs_review(issues)
    assert stored_type.value.upper() in issues[0].message


@pytest.mark.db
def test_verification_works_in_both_directions(db: Session) -> None:
    store(db, make_invoice())

    issues = check(db, make_invoice(), FileType.XLSX)

    assert [issue.code for issue in issues] == [ValidationCode.CROSS_DOCUMENT_VERIFIED]


@pytest.mark.db
def test_a_pdf_and_spreadsheet_sharing_a_total_can_still_disagree_on_other_values(
    db: Session,
) -> None:
    stored = store(db, make_invoice(tax=amount("11.00"), subtotal=amount("99.00")), FileType.XLSX)

    issues = check(db, make_invoice())

    assert {(i.code, i.field, i.related_document_id) for i in issues} == {
        (ValidationCode.PDF_EXCEL_MISMATCH, "subtotal", stored.id),
        (ValidationCode.PDF_EXCEL_MISMATCH, "tax", stored.id),
    }
    assert needs_review(issues)


@pytest.mark.db
def test_a_second_spreadsheet_of_the_same_invoice_is_still_a_duplicate(db: Session) -> None:
    store(db, make_invoice(), FileType.XLSX)

    issues = check(db, make_invoice(), FileType.CSV)

    assert [issue.code for issue in issues] == [ValidationCode.DUPLICATE_INVOICE]


@pytest.mark.db
def test_spreadsheet_against_stored_pdf_mismatch(db: Session) -> None:
    store(db, make_invoice(total=amount("120.00")))

    issues = check(db, make_invoice(), FileType.CSV)

    assert [issue.code for issue in issues] == [ValidationCode.PDF_EXCEL_MISMATCH]
    assert "this CSV" in issues[0].message and "related PDF" in issues[0].message


@pytest.mark.db
def test_image_against_pdf_uses_the_generic_code(db: Session) -> None:
    store(db, make_invoice(total=amount("120.00")))

    issues = check(db, make_invoice(), FileType.PNG)

    assert [issue.code for issue in issues] == [ValidationCode.CROSS_DOCUMENT_MISMATCH]


@pytest.mark.db
def test_stored_line_items_are_compared(db: Session) -> None:
    store(
        db,
        make_invoice(total=amount("120.00"), line_items=[line(1, "60.00"), line(2, "50.00")]),
    )

    issues = check(db, make_invoice())

    assert [(i.field, i.actual_value, i.expected_value) for i in issues] == [
        ("total", "110.00", "120.00"),
        ("line_items.sum", "100.00", "110.00"),
        ("line_items[2].amount", "40.00", "50.00"),
    ]


@pytest.mark.db
def test_same_number_from_a_different_supplier_is_ignored(db: Session) -> None:
    store(
        db,
        make_invoice(
            invoice_number=known("1001"),
            supplier_name=known("Southwind Ltd"),
            total=amount("999.00"),
        ),
    )

    assert check(db, make_invoice(invoice_number=known("1001"))) == []


@pytest.mark.db
def test_same_number_with_differently_written_supplier_is_compared(db: Session) -> None:
    store(
        db,
        make_invoice(
            invoice_number=known("1001"),
            supplier_name=known("NORTHWIND LIMITED"),
            total=amount("999.00"),
        ),
    )

    issues = check(db, make_invoice(invoice_number=known("1001")))

    assert [(i.code, i.field) for i in issues] == [(ValidationCode.PDF_PDF_MISMATCH, "total")]


@pytest.mark.db
@pytest.mark.parametrize("which", ["stored", "new"])
def test_unknown_supplier_on_one_side_is_still_compared(db: Session, which: str) -> None:
    stored_supplier = unknown() if which == "stored" else known("Northwind Ltd")
    new_supplier = unknown() if which == "new" else known("Northwind Ltd")
    store(db, make_invoice(supplier_name=stored_supplier, total=amount("999.00")))

    issues = check(db, make_invoice(supplier_name=new_supplier))

    assert [(i.code, i.field) for i in issues] == [(ValidationCode.PDF_PDF_MISMATCH, "total")]


@pytest.mark.db
def test_unknown_versus_known_in_storage_is_not_flagged(db: Session) -> None:
    store(
        db,
        make_invoice(
            subtotal=unknown(),
            tax=unknown(),
            total=unknown(),
            currency=unknown(),
            invoice_date=unknown(),
            customer_name=unknown(),
            line_items=[],
        ),
    )

    assert check(db, make_invoice()) == []


@pytest.mark.db
def test_unknown_number_matches_nothing(db: Session) -> None:
    store(db, make_invoice())
    no_number = make_invoice(invoice_number=unknown())

    assert check(db, no_number) == []


@pytest.mark.db
def test_number_with_no_letters_or_digits_matches_nothing(db: Session) -> None:
    store(db, make_invoice(invoice_number=known("---"), total=amount("999.00")))

    assert check(db, make_invoice(invoice_number=known("###"))) == []


@pytest.mark.db
def test_unknown_total_cannot_be_a_duplicate_but_is_still_related(db: Session) -> None:
    store(db, make_invoice(subtotal=amount("90.00")))

    issues = check(db, make_invoice(total=unknown()))

    assert [(i.code, i.field) for i in issues] == [(ValidationCode.PDF_PDF_MISMATCH, "subtotal")]


@pytest.mark.db
def test_superseded_versions_are_ignored(db: Session) -> None:
    document = make_document(db)
    save_invoice(db, document, make_invoice())
    save_invoice(
        db, document, make_invoice(invoice_number=known("OTHER-9"), total=amount("500.00"))
    )

    assert check(db, make_invoice()) == []


@pytest.mark.db
def test_superseded_version_with_conflicting_values_is_ignored(db: Session) -> None:
    document = make_document(db)
    save_invoice(db, document, make_invoice(total=amount("999.00")))
    current = save_invoice(db, document, make_invoice(total=amount("120.00")))

    issues = check(db, make_invoice())

    assert [(i.field, i.expected_value, i.related_document_id) for i in issues] == [
        ("total", "120.00", current.document_id)
    ]


@pytest.mark.db
def test_own_row_is_ignored_when_excluded_by_id(db: Session) -> None:
    document = make_document(db)
    invoice = make_invoice()
    saved = save_invoice(db, document, invoice)

    issues = check(db, invoice, document_id=document.id, exclude_result_id=saved.id)

    assert issues == []


@pytest.mark.db
def test_own_row_is_ignored_without_an_id_too(db: Session) -> None:
    document = make_document(db)
    invoice = make_invoice()
    save_invoice(db, document, invoice)

    assert check(db, invoice, document_id=document.id) == []


@pytest.mark.db
def test_excluding_a_result_id_leaves_others_in_place(db: Session) -> None:
    other = store(db, make_invoice())
    document = make_document(db)
    invoice = make_invoice()
    saved = save_invoice(db, document, invoice)

    issues = check(db, invoice, document_id=document.id, exclude_result_id=saved.id)

    assert [issue.related_document_id for issue in issues] == [other.id]


@pytest.mark.db
def test_exclude_id_alone_excludes_the_row_even_at_another_index(db: Session) -> None:
    document = make_document(db)
    saved = save_invoice(db, document, make_invoice(invoice_index=4))

    issues = check(
        db, make_invoice(invoice_index=0), document_id=document.id, exclude_result_id=saved.id
    )

    assert issues == []


@pytest.mark.db
def test_other_versions_of_this_invoice_are_ignored(db: Session) -> None:
    document = make_document(db)
    save_invoice(db, document, make_invoice())
    current_invoice = make_invoice()
    saved = save_invoice(db, document, current_invoice)
    # Older versions have the same key but are not current; only this row is, and it is excluded.

    assert check(db, current_invoice, document_id=document.id, exclude_result_id=saved.id) == []


@pytest.mark.db
def test_duplicate_inside_one_document_is_reported(db: Session) -> None:
    document = make_document(db)
    save_invoice(db, document, make_invoice(invoice_index=0))
    repeat = make_invoice(invoice_index=1)
    saved = save_invoice(db, document, repeat)

    issues = check(db, repeat, document_id=document.id, exclude_result_id=saved.id)

    assert [(i.code, i.severity, i.related_document_id) for i in issues] == [
        (ValidationCode.DUPLICATE_INVOICE, Severity.ERROR, document.id)
    ]
    assert "this document" in issues[0].message


@pytest.mark.db
def test_conflict_inside_one_document_is_not_a_cross_document_mismatch(db: Session) -> None:
    document = make_document(db)
    save_invoice(db, document, make_invoice(invoice_index=0, total=amount("999.00")))
    other_index = make_invoice(invoice_index=1)
    saved = save_invoice(db, document, other_index)

    assert check(db, other_index, document_id=document.id, exclude_result_id=saved.id) == []


@pytest.mark.db
def test_deleted_related_document_causes_no_failure(db: Session) -> None:
    gone = store(db, make_invoice())
    conflicting = store(db, make_invoice(total=amount("999.00")))
    db.commit()

    db.delete(gone)
    db.delete(conflicting)
    db.commit()

    assert check(db, make_invoice()) == []


def age_results(db: Session, documents: list[Document]) -> None:
    """Give each document's results a distinct creation time, the last document being newest."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for offset, document in enumerate(documents):
        for result in document.extraction_results:
            result.created_at = start + timedelta(minutes=offset)
    db.flush()


@pytest.mark.db
def test_related_rows_are_limited_to_the_most_recent(db: Session) -> None:
    documents = [
        store(db, make_invoice(total=amount("999.00"))) for _ in range(MAX_RELATED_RESULTS + 3)
    ]
    age_results(db, documents)
    db.expire_all()

    with captured_statements(db) as statements:
        issues = check(db, make_invoice())

    assert len(issues) == MAX_RELATED_RESULTS
    assert {issue.related_document_id for issue in issues} == {
        document.id for document in documents[3:]
    }
    assert sum("LIMIT" in statement for statement, _ in statements) == 2


@pytest.mark.db
def test_duplicates_are_limited_to_the_most_recent(db: Session) -> None:
    documents = [store(db, make_invoice()) for _ in range(MAX_RELATED_RESULTS + 2)]
    age_results(db, documents)
    db.expire_all()

    issues = check(db, make_invoice())

    assert len(issues) == MAX_RELATED_RESULTS
    assert {issue.code for issue in issues} == {ValidationCode.DUPLICATE_INVOICE}
    assert {issue.related_document_id for issue in issues} == {
        document.id for document in documents[2:]
    }


@pytest.mark.db
def test_many_related_results_do_not_add_queries(db: Session) -> None:
    store(db, make_invoice(total=amount("999.00")))
    db.commit()
    db.expunge_all()
    with captured_statements(db) as one_related:
        assert len(check(db, make_invoice())) == 1

    for _ in range(4):
        store(db, make_invoice(total=amount("999.00")))
    db.commit()
    db.expunge_all()
    with captured_statements(db) as five_related:
        assert len(check(db, make_invoice())) == 5

    assert len(five_related) == len(one_related)


@pytest.mark.db
@pytest.mark.parametrize(
    ("column", "index"),
    [
        ("duplicate_key =", "ix_extraction_results_duplicate_key"),
        ("invoice_key =", "ix_extraction_results_invoice_key"),
    ],
)
def test_lookups_use_the_partial_indexes(db: Session, column: str, index: str) -> None:
    store(db, make_invoice())
    with captured_statements(db) as statements:
        check(db, make_invoice())
    statement, parameters = next(item for item in statements if column in item[0])

    connection = db.connection()
    connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
    plan = connection.exec_driver_sql(f"EXPLAIN {statement}", parameters).scalars().all()

    assert index in "\n".join(plan)


@pytest.mark.db
def test_checking_does_not_write(db: Session) -> None:
    store(db, make_invoice(total=amount("120.00")))
    with captured_statements(db) as statements:
        check(db, make_invoice())

    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement, _ in statements)
