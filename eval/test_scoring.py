"""A mistake in the scorer would be a false number in the report, so it gets its own tests."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from scoring import (
    IssueScore,
    Score,
    actual_issue_codes,
    classify_field,
    match_invoices,
    match_line_items,
    values_match,
)


def result(number="A-1", total="100.00", lines=None, **fields) -> dict:
    values = {
        "invoice_number": number,
        "invoice_date": "2026-03-15",
        "supplier_name": "Northwind Ltd",
        "customer_name": "Contoso",
        "subtotal": "90.00",
        "tax": "10.00",
        "total": total,
        "currency": "USD",
        **fields,
    }
    return {
        "fields": {name: {"value": value} for name, value in values.items()},
        "line_items": lines or [],
        "validation_errors": [],
    }


def label(number="A-1", total="100.00", lines=None, **fields) -> dict:
    values = {
        "invoice_number": number,
        "invoice_date": "2026-03-15",
        "supplier_name": "Northwind Ltd",
        "customer_name": "Contoso",
        "subtotal": "90.00",
        "tax": "10.00",
        "total": total,
        "currency": "USD",
        **fields,
    }
    return {"fields": values, "line_items": lines or []}


@pytest.mark.parametrize(
    ("name", "expected", "actual", "same"),
    [
        ("total", "1100.00", "1100", True),
        ("total", "1100.00", "1100.01", True),
        ("total", "1100.00", "1100.02", False),
        ("total", "1100.00", "1110.00", False),
        ("total", "1100.00", "not a number", False),
        ("invoice_number", "INV-0042", "inv 0042", True),
        ("invoice_number", "INV-0042", "INV-0043", False),
        ("supplier_name", "Northwind Ltd.", "NORTHWIND LTD", True),
        ("supplier_name", "Müller GmbH", "Muller GmbH", True),
        ("supplier_name", "Northwind", "Southwind", False),
        ("currency", "USD", "usd", True),
        ("currency", "USD", "EUR", False),
        ("invoice_date", "2026-03-15", "2026-03-15", True),
        ("invoice_date", "2026-03-15", "2026-03-16", False),
        ("quantity", "2", "2.00", True),
        ("description", "Widget, large", "widget large", True),
    ],
)
def test_values_match(name: str, expected: str, actual: str, same: bool) -> None:
    assert values_match(name, expected, actual) is same


@pytest.mark.parametrize(
    ("expected", "actual", "outcome"),
    [
        ("100.00", "100.00", "correct"),
        ("100.00", "200.00", "wrong"),
        ("100.00", None, "missing"),
        (None, "100.00", "invented"),
        (None, None, "correct_null"),
    ],
)
def test_classify_field(expected, actual, outcome: str) -> None:
    assert classify_field("total", expected, actual) == outcome


def test_a_perfect_invoice_scores_full_marks() -> None:
    score = Score()

    score.add_invoice("a.pdf", label(), result())

    assert score.field_accuracy == 1.0
    assert score.value_accuracy == 1.0
    assert score.invoices_all_fields_right == 1
    assert score.errors == []


def test_an_invented_value_is_counted_and_lowers_accuracy() -> None:
    score = Score()

    score.add_invoice("a.pdf", label(tax=None), result(tax="10.00"))

    assert score.fields["invented"] == 1
    assert score.field_accuracy == pytest.approx(7 / 8)
    assert score.invoices_all_fields_right == 0
    assert score.errors[0]["outcome"] == "invented"
    assert score.errors[0]["field"] == "tax"


def test_saying_null_for_something_absent_is_correct() -> None:
    score = Score()

    score.add_invoice("a.pdf", label(tax=None), result(tax=None))

    assert score.fields["correct_null"] == 1
    assert score.field_accuracy == 1.0
    assert score.invoices_all_fields_right == 1
    # The harder number only counts values that exist.
    assert score.value_accuracy == 1.0


def test_a_missed_invoice_counts_every_value_as_missing() -> None:
    score = Score()

    score.add_invoice("a.pdf", label(), None)

    assert score.fields["missing"] == 8
    assert score.value_accuracy == 0.0
    assert score.invoices_found == 0
    assert score.invoices_all_fields_right == 0


def test_null_answers_do_not_inflate_the_value_accuracy() -> None:
    score = Score()
    score.add_invoice(
        "a.pdf",
        label(tax=None, currency=None),
        result(tax=None, currency=None, total="1.00"),
    )

    # Of 6 real values, 5 are right. Counting the 2 correct nulls would make it look better.
    assert score.value_accuracy == pytest.approx(5 / 6)
    assert score.field_accuracy == pytest.approx(7 / 8)


def test_invoices_are_paired_by_number_regardless_of_order() -> None:
    pairs, extra = match_invoices(
        [label("A-1"), label("A-2")],
        [result("A-2", total="2"), result("A-1", total="1")],
    )

    assert pairs[0][1]["fields"]["total"]["value"] == "1"
    assert pairs[1][1]["fields"]["total"]["value"] == "2"
    assert extra == []


def test_a_misread_number_is_still_paired_so_its_fields_get_scored() -> None:
    pairs, extra = match_invoices([label("A-1")], [result("A-7")])

    assert pairs[0][1] is not None
    assert extra == []


def test_extra_invoices_are_reported() -> None:
    pairs, extra = match_invoices([label("A-1")], [result("A-1"), result("B-9")])

    assert len(pairs) == 1
    assert len(extra) == 1


def test_a_missing_invoice_pairs_with_nothing() -> None:
    pairs, _ = match_invoices([label("A-1"), label("A-2")], [result("A-1")])

    assert pairs[1][1] is None


def row(description="Widget", quantity="2", unit_price="5.00", amount="10.00") -> dict:
    return {
        "description": description,
        "quantity": quantity,
        "unit_price": unit_price,
        "amount": amount,
    }


def test_line_items_pair_by_similarity_not_position() -> None:
    pairs = match_line_items(
        [row("Widget", amount="10.00"), row("Gadget", amount="20.00")],
        [row("Gadget", amount="20.00"), row("Widget", amount="10.00")],
    )

    assert pairs[0][1]["description"] == "Widget"
    assert pairs[1][1]["description"] == "Gadget"


def test_a_found_row_is_used_only_once() -> None:
    pairs = match_line_items([row("Widget"), row("Widget")], [row("Widget")])

    assert pairs[0][1] is not None
    assert pairs[1][1] is None


def test_nothing_similar_is_no_match() -> None:
    pairs = match_line_items([row("Widget", amount="10")], [row("Zzzzzz", amount="99")])

    assert pairs[0][1] is None


def test_line_item_scores() -> None:
    score = Score()
    lines = [row("Widget"), row("Gadget", amount="20.00")]
    found = [
        row("Widget"),
        row("Gadget", amount="21.00"),
        row("Ghost row", amount="1.00"),
    ]

    score.add_invoice("a.pdf", label(lines=lines), result(lines=found))

    assert score.expected_rows == 2
    assert score.found_rows == 3
    assert score.exact_rows == 1
    assert score.line_recall == 0.5
    assert score.line_precision == pytest.approx(1 / 3)
    # 7 of 8 labeled cells are right, and only the second row's amount differs.
    assert score.cell_accuracy == pytest.approx(7 / 8)


def test_a_row_not_found_has_all_its_cells_wrong() -> None:
    score = Score()

    score.add_invoice("a.pdf", label(lines=[row()]), result(lines=[]))

    assert score.exact_rows == 0
    assert score.cells["wrong"] == 4


def test_no_rows_at_all_gives_no_ratio_rather_than_a_fake_one() -> None:
    score = Score()

    score.add_invoice("a.pdf", label(), result())

    assert score.line_precision is None
    assert score.line_recall is None
    assert score.cell_accuracy is None


def test_issue_precision_and_recall() -> None:
    score = IssueScore()

    score.add(
        "a",
        {"total_mismatch", "duplicate_invoice"},
        {"total_mismatch", "low_confidence"},
        True,
        True,
    )

    assert score.true_positives["total_mismatch"] == 1
    assert score.false_positives["low_confidence"] == 1
    assert score.false_negatives["duplicate_invoice"] == 1
    assert score.precision == 0.5
    assert score.recall == 0.5
    assert {mistake["kind"] for mistake in score.mistakes} == {"false alarm", "missed"}


def test_a_clean_document_with_no_flags_counts_only_as_a_true_negative() -> None:
    score = IssueScore()

    score.add("a", set(), set(), False, False)

    assert score.precision is None
    assert score.recall is None
    assert score.review_true_negative == 1


def test_review_flag_confusion() -> None:
    score = IssueScore()
    score.add("a", {"x"}, {"x"}, True, True)
    score.add("b", set(), {"y"}, False, True)
    score.add("c", {"z"}, set(), True, False)

    assert score.review_precision == 0.5
    assert score.review_recall == 0.5


def test_actual_issue_codes_ignore_notes_but_keep_verifications() -> None:
    results = [
        {
            "validation_errors": [
                {"code": "no_line_items", "severity": "info"},
                {"code": "cross_document_verified", "severity": "info"},
                {"code": "total_mismatch", "severity": "error"},
                {"code": "some_other_note", "severity": "info"},
            ]
        }
    ]

    assert actual_issue_codes(results) == {"cross_document_verified", "total_mismatch"}
