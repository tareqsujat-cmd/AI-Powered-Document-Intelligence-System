from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.db.enums import Severity
from app.extraction.schemas import ExtractedField, InvoiceData, LineItemData
from app.validation.issues import ValidationCode, ValidationIssue, needs_review
from app.validation.rules import (
    KNOWN_CURRENCIES,
    LOW_CONFIDENCE_THRESHOLD,
    format_amount,
    validate_invoice,
)

TODAY = date(2026, 6, 15)


def known(value: Any, confidence: float = 0.95) -> ExtractedField[Any]:
    return ExtractedField[Any](value=value, confidence=confidence)


def unknown(reason: str | None = None) -> ExtractedField[Any]:
    return ExtractedField[Any](reason=reason)


def amount(text: str) -> ExtractedField[Any]:
    return known(Decimal(text))


def line(
    position: int = 1,
    quantity: str | None = "2",
    unit_price: str | None = "50.00",
    line_amount: str | None = "100.00",
) -> LineItemData:
    return LineItemData(
        position=position,
        description="Widget",
        quantity=None if quantity is None else Decimal(quantity),
        unit_price=None if unit_price is None else Decimal(unit_price),
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
        "line_items": [line()],
    }
    return InvoiceData(**{**values, **overrides})


def validate(**overrides: Any) -> list[ValidationIssue]:
    return validate_invoice(make_invoice(**overrides), today=TODAY)


def with_code(issues: list[ValidationIssue], code: ValidationCode) -> list[ValidationIssue]:
    return [issue for issue in issues if issue.code is code]


def codes(issues: list[ValidationIssue]) -> list[ValidationCode]:
    return [issue.code for issue in issues]


def test_clean_invoice_has_no_issues() -> None:
    issues = validate()

    assert issues == []
    assert needs_review(issues) is False


def test_validation_codes_fit_the_database_column() -> None:
    assert all(len(code.value) <= 64 for code in ValidationCode)


def test_issue_is_immutable() -> None:
    issue = ValidationIssue(ValidationCode.ZERO_TOTAL, Severity.WARNING, "Total is zero.")

    with pytest.raises(AttributeError):
        issue.message = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("severities", "expected"),
    [
        ([], False),
        ([Severity.INFO], False),
        ([Severity.INFO, Severity.INFO], False),
        ([Severity.WARNING], True),
        ([Severity.ERROR], True),
        ([Severity.INFO, Severity.ERROR], True),
    ],
)
def test_needs_review_depends_on_severity(severities: list[Severity], expected: bool) -> None:
    issues = [ValidationIssue(ValidationCode.NO_LINE_ITEMS, s, "x") for s in severities]

    assert needs_review(issues) is expected
    assert needs_review(iter(issues)) is expected


# Missing fields


@pytest.mark.parametrize("name", ["invoice_number", "invoice_date", "supplier_name", "total"])
def test_missing_required_field_is_an_error(name: str) -> None:
    issues = validate(**{name: unknown()})

    missing = with_code(issues, ValidationCode.MISSING_REQUIRED_FIELD)
    assert [(issue.field, issue.severity) for issue in missing] == [(name, Severity.ERROR)]


@pytest.mark.parametrize("name", ["customer_name", "subtotal", "tax", "currency"])
def test_missing_recommended_field_is_a_warning(name: str) -> None:
    issues = validate(**{name: unknown()})

    missing = with_code(issues, ValidationCode.MISSING_FIELD)
    assert [(issue.field, issue.severity) for issue in missing] == [(name, Severity.WARNING)]
    assert with_code(issues, ValidationCode.MISSING_REQUIRED_FIELD) == []


def test_missing_field_message_includes_the_reason() -> None:
    issues = validate(supplier_name=unknown("Header was unreadable."))

    assert with_code(issues, ValidationCode.MISSING_REQUIRED_FIELD)[0].message == (
        "Supplier name is missing. Header was unreadable."
    )


def test_missing_field_without_a_reason_has_a_plain_message() -> None:
    issues = validate(supplier_name=unknown())

    assert with_code(issues, ValidationCode.MISSING_REQUIRED_FIELD)[0].message == (
        "Supplier name is missing."
    )


def test_blank_string_counts_as_missing_and_not_as_low_confidence() -> None:
    issues = validate(supplier_name=known("   ", confidence=0.1), currency=known("", 0.1))

    assert [issue.field for issue in with_code(issues, ValidationCode.MISSING_REQUIRED_FIELD)] == [
        "supplier_name"
    ]
    assert [issue.field for issue in with_code(issues, ValidationCode.MISSING_FIELD)] == [
        "currency"
    ]
    assert with_code(issues, ValidationCode.LOW_CONFIDENCE) == []
    assert with_code(issues, ValidationCode.INVALID_CURRENCY) == []


def test_all_fields_unknown_reports_every_field_and_no_arithmetic() -> None:
    invoice = InvoiceData()

    issues = validate_invoice(invoice, today=TODAY)

    assert sorted(codes(issues)) == sorted(
        [ValidationCode.MISSING_REQUIRED_FIELD] * 4
        + [ValidationCode.MISSING_FIELD] * 4
        + [ValidationCode.NO_LINE_ITEMS]
    )


# Numeric sanity


@pytest.mark.parametrize("name", ["subtotal", "tax", "total"])
def test_negative_amount_is_a_warning(name: str) -> None:
    issues = validate(**{name: amount("-5.00")})

    negative = with_code(issues, ValidationCode.NEGATIVE_AMOUNT)
    assert [(i.field, i.severity, i.actual_value) for i in negative] == [
        (name, Severity.WARNING, "-5.00")
    ]


def test_zero_amounts_are_not_negative() -> None:
    issues = validate(
        subtotal=amount("0"),
        tax=amount("0"),
        total=amount("0"),
        line_items=[line(line_amount="0", unit_price="0")],
    )

    assert with_code(issues, ValidationCode.NEGATIVE_AMOUNT) == []


def test_zero_total_is_a_warning() -> None:
    issues = validate(total=amount("0.00"), subtotal=amount("0.00"), tax=amount("0.00"))

    zero = with_code(issues, ValidationCode.ZERO_TOTAL)
    assert [(i.field, i.severity, i.actual_value) for i in zero] == [
        ("total", Severity.WARNING, "0.00")
    ]


def test_nonzero_and_unknown_totals_are_not_zero_total() -> None:
    assert with_code(validate(), ValidationCode.ZERO_TOTAL) == []
    assert with_code(validate(total=unknown()), ValidationCode.ZERO_TOTAL) == []


def test_tax_greater_than_subtotal_is_a_warning() -> None:
    issues = validate(subtotal=amount("10.00"), tax=amount("10.01"), total=amount("20.01"))

    excess = with_code(issues, ValidationCode.TAX_EXCEEDS_SUBTOTAL)
    assert [(i.field, i.severity, i.actual_value) for i in excess] == [
        ("tax", Severity.WARNING, "10.01")
    ]


def test_tax_equal_to_subtotal_is_not_flagged() -> None:
    issues = validate(subtotal=amount("10.00"), tax=amount("10.00"), total=amount("20.00"))

    assert with_code(issues, ValidationCode.TAX_EXCEEDS_SUBTOTAL) == []


def test_credit_note_is_not_reported_as_tax_exceeding_subtotal() -> None:
    issues = validate(
        subtotal=amount("-100.00"),
        tax=amount("-10.00"),
        total=amount("-110.00"),
        line_items=[line(quantity="-2", line_amount="-100.00")],
    )

    assert with_code(issues, ValidationCode.TAX_EXCEEDS_SUBTOTAL) == []
    assert with_code(issues, ValidationCode.TOTAL_MISMATCH) == []
    assert with_code(issues, ValidationCode.LINE_ITEMS_SUBTOTAL_MISMATCH) == []
    assert with_code(issues, ValidationCode.LINE_ITEM_AMOUNT_MISMATCH) == []
    assert [issue.field for issue in with_code(issues, ValidationCode.NEGATIVE_AMOUNT)] == [
        "subtotal",
        "tax",
        "total",
    ]
    assert needs_review(issues) is True


def test_line_item_negative_quantity_and_unit_price_are_warnings() -> None:
    issues = validate(line_items=[line(quantity="-2", unit_price="-50.00", line_amount="100.00")])

    negative = with_code(issues, ValidationCode.LINE_ITEM_NEGATIVE_VALUE)
    assert [(i.field, i.severity, i.actual_value) for i in negative] == [
        ("line_items[1].quantity", Severity.WARNING, "-2.00"),
        ("line_items[1].unit_price", Severity.WARNING, "-50.00"),
    ]


def test_positive_line_item_values_are_not_negative() -> None:
    assert with_code(validate(), ValidationCode.LINE_ITEM_NEGATIVE_VALUE) == []


def test_zero_quantity_with_an_amount_is_one_warning() -> None:
    issues = validate(line_items=[line(quantity="0")])

    assert [(i.code, i.severity, i.field) for i in issues] == [
        (ValidationCode.LINE_ITEM_ZERO_QUANTITY, Severity.WARNING, "line_items[1].quantity")
    ]


@pytest.mark.parametrize("line_amount", ["0.00", None])
def test_zero_quantity_without_an_amount_is_not_flagged(line_amount: str | None) -> None:
    issues = validate(line_items=[line(quantity="0", line_amount=line_amount)])

    assert with_code(issues, ValidationCode.LINE_ITEM_ZERO_QUANTITY) == []


# subtotal + tax = total


@pytest.mark.parametrize("total", ["110.00", "110.01", "109.99"])
def test_total_within_tolerance_passes(total: str) -> None:
    assert with_code(validate(total=amount(total)), ValidationCode.TOTAL_MISMATCH) == []


@pytest.mark.parametrize("total", ["110.02", "109.98", "110.0101"])
def test_total_beyond_tolerance_fails(total: str) -> None:
    mismatches = with_code(validate(total=amount(total)), ValidationCode.TOTAL_MISMATCH)

    assert len(mismatches) == 1
    assert mismatches[0].severity is Severity.ERROR
    assert mismatches[0].field == "total"
    assert mismatches[0].expected_value == "110.00"
    assert mismatches[0].actual_value == total
    assert "110.00" in mismatches[0].message and total in mismatches[0].message


def test_tolerance_is_exact_where_a_float_is_not() -> None:
    # In floats 0.1 + 0.2 - 0.29 is 0.01000000000000000x, just over the tolerance.
    issues = validate(
        subtotal=amount("0.1"), tax=amount("0.2"), total=amount("0.29"), line_items=[]
    )

    assert with_code(issues, ValidationCode.TOTAL_MISMATCH) == []
    assert with_code(issues, ValidationCode.TAX_EXCEEDS_SUBTOTAL) != []


def test_decimal_one_tenth_and_two_tenths_make_three_tenths() -> None:
    issues = validate(subtotal=amount("0.1"), tax=amount("0.2"), total=amount("0.3"), line_items=[])

    assert with_code(issues, ValidationCode.TOTAL_MISMATCH) == []


@pytest.mark.parametrize(
    ("total", "fails"),
    [
        ("12345678901234.5778", False),  # exactly 0.01 away
        ("12345678901234.5779", True),  # a float cannot tell this from exactly 0.01
        ("12345678901234.5578", False),
        ("12345678901234.5577", True),
    ],
)
def test_tolerance_holds_at_eighteen_digits(total: str, fails: bool) -> None:
    issues = validate(
        subtotal=amount("12345678901234.5678"),
        tax=amount("0"),
        total=amount(total),
        line_items=[],
    )

    assert bool(with_code(issues, ValidationCode.TOTAL_MISMATCH)) is fails


def test_tax_unknown_and_subtotal_differs_from_total_is_a_distinct_warning() -> None:
    issues = validate(tax=unknown())

    assert with_code(issues, ValidationCode.TOTAL_MISMATCH) == []
    unverified = with_code(issues, ValidationCode.TOTAL_UNVERIFIED)
    assert [(i.severity, i.field, i.actual_value, i.expected_value) for i in unverified] == [
        (Severity.WARNING, "total", "110.00", None)
    ]
    assert "could not be fully checked" in unverified[0].message


@pytest.mark.parametrize("total", ["100.00", "100.01", "99.99"])
def test_tax_unknown_and_subtotal_matches_total_is_not_unverified(total: str) -> None:
    issues = validate(tax=unknown(), total=amount(total))

    assert with_code(issues, ValidationCode.TOTAL_UNVERIFIED) == []
    assert with_code(issues, ValidationCode.TOTAL_MISMATCH) == []


@pytest.mark.parametrize(
    "unknown_fields",
    [
        ("subtotal",),
        ("total",),
        ("subtotal", "tax"),
        ("subtotal", "total"),
        ("tax", "total"),
        ("subtotal", "tax", "total"),
    ],
)
def test_unknown_values_never_produce_a_total_mismatch(unknown_fields: tuple[str, ...]) -> None:
    issues = validate(**{name: unknown() for name in unknown_fields})

    assert with_code(issues, ValidationCode.TOTAL_MISMATCH) == []
    assert with_code(issues, ValidationCode.TOTAL_UNVERIFIED) == []


def test_known_zero_tax_is_checked_rather_than_treated_as_unknown() -> None:
    issues = validate(tax=amount("0"), total=amount("110.00"))

    assert codes(with_code(issues, ValidationCode.TOTAL_MISMATCH)) == [
        ValidationCode.TOTAL_MISMATCH
    ]
    assert with_code(issues, ValidationCode.TOTAL_UNVERIFIED) == []


# sum(line items) = subtotal


@pytest.mark.parametrize("subtotal", ["300.00", "300.01", "299.99"])
def test_line_sum_within_tolerance_passes(subtotal: str) -> None:
    issues = validate(
        subtotal=amount(subtotal),
        tax=unknown(),
        total=amount(subtotal),
        line_items=[line(1, line_amount="100.00"), line(2, line_amount="200.00", quantity="4")],
    )

    assert with_code(issues, ValidationCode.LINE_ITEMS_SUBTOTAL_MISMATCH) == []


@pytest.mark.parametrize("subtotal", ["300.02", "299.98"])
def test_line_sum_beyond_tolerance_fails(subtotal: str) -> None:
    issues = validate(
        subtotal=amount(subtotal),
        tax=unknown(),
        total=amount(subtotal),
        line_items=[line(1, line_amount="100.00"), line(2, line_amount="200.00", quantity="4")],
    )

    mismatches = with_code(issues, ValidationCode.LINE_ITEMS_SUBTOTAL_MISMATCH)
    assert [(i.severity, i.field, i.expected_value, i.actual_value) for i in mismatches] == [
        (Severity.ERROR, "subtotal", "300.00", subtotal)
    ]


def test_line_sum_is_exact_across_many_small_amounts() -> None:
    items = [
        line(position, quantity="1", unit_price="0.10", line_amount="0.10")
        for position in range(1, 11)
    ]
    # Ten floats of 0.1 add to 0.9999999999999999, which puts a float difference over 0.01.
    issues = validate(
        subtotal=amount("1.01"), tax=amount("0"), total=amount("1.01"), line_items=items
    )

    assert with_code(issues, ValidationCode.LINE_ITEMS_SUBTOTAL_MISMATCH) == []


def test_some_unknown_line_amounts_are_incomplete_not_a_mismatch() -> None:
    issues = validate(
        line_items=[line(1), line(2, line_amount=None), line(3, line_amount=None)],
    )

    assert with_code(issues, ValidationCode.LINE_ITEMS_SUBTOTAL_MISMATCH) == []
    incomplete = with_code(issues, ValidationCode.LINE_ITEMS_INCOMPLETE)
    assert [(i.severity, i.field) for i in incomplete] == [(Severity.WARNING, "line_items")]
    assert "position 2, 3" in incomplete[0].message


def test_incomplete_lines_are_reported_without_a_subtotal_too() -> None:
    issues = validate(subtotal=unknown(), line_items=[line(line_amount=None)])

    assert codes(with_code(issues, ValidationCode.LINE_ITEMS_INCOMPLETE)) == [
        ValidationCode.LINE_ITEMS_INCOMPLETE
    ]
    assert with_code(issues, ValidationCode.LINE_ITEMS_TOTAL_MISMATCH) == []


def test_no_lines_produces_no_line_arithmetic() -> None:
    issues = validate(line_items=[])

    assert codes(issues) == [ValidationCode.NO_LINE_ITEMS]


def test_no_subtotal_compares_lines_with_total_as_a_warning() -> None:
    issues = validate(subtotal=unknown(), tax=unknown(), total=amount("120.00"))

    assert with_code(issues, ValidationCode.LINE_ITEMS_SUBTOTAL_MISMATCH) == []
    mismatches = with_code(issues, ValidationCode.LINE_ITEMS_TOTAL_MISMATCH)
    assert [(i.severity, i.field, i.expected_value, i.actual_value) for i in mismatches] == [
        (Severity.WARNING, "total", "100.00", "120.00")
    ]
    assert "tax" in mismatches[0].message


def test_no_subtotal_with_known_zero_tax_compares_lines_with_total() -> None:
    issues = validate(subtotal=unknown(), tax=amount("0"), total=amount("120.00"))

    assert codes(with_code(issues, ValidationCode.LINE_ITEMS_TOTAL_MISMATCH)) == [
        ValidationCode.LINE_ITEMS_TOTAL_MISMATCH
    ]


def test_no_subtotal_and_lines_matching_total_is_not_flagged() -> None:
    issues = validate(subtotal=unknown(), tax=unknown(), total=amount("100.01"))

    assert with_code(issues, ValidationCode.LINE_ITEMS_TOTAL_MISMATCH) == []


def test_no_subtotal_with_known_nonzero_tax_is_not_guessed_at() -> None:
    issues = validate(subtotal=unknown(), tax=amount("10.00"), total=amount("500.00"))

    assert with_code(issues, ValidationCode.LINE_ITEMS_TOTAL_MISMATCH) == []
    assert with_code(issues, ValidationCode.TOTAL_MISMATCH) == []


def test_no_subtotal_and_no_total_compares_nothing() -> None:
    issues = validate(subtotal=unknown(), tax=unknown(), total=unknown())

    assert with_code(issues, ValidationCode.LINE_ITEMS_TOTAL_MISMATCH) == []
    assert with_code(issues, ValidationCode.LINE_ITEMS_SUBTOTAL_MISMATCH) == []


# quantity x unit_price = amount


@pytest.mark.parametrize("line_amount", ["100.00", "100.01", "99.99"])
def test_line_amount_within_tolerance_passes(line_amount: str) -> None:
    issues = validate(
        subtotal=amount(line_amount),
        total=amount(line_amount),
        tax=amount("0"),
        line_items=[line(line_amount=line_amount)],
    )

    assert issues == []


@pytest.mark.parametrize("line_amount", ["100.02", "99.98"])
def test_line_amount_beyond_tolerance_fails(line_amount: str) -> None:
    issues = validate(
        subtotal=amount(line_amount),
        total=amount(line_amount),
        tax=amount("0"),
        line_items=[line(line_amount=line_amount)],
    )

    mismatches = with_code(issues, ValidationCode.LINE_ITEM_AMOUNT_MISMATCH)
    assert [(i.severity, i.field, i.expected_value, i.actual_value) for i in mismatches] == [
        (Severity.WARNING, "line_items[1].amount", "100.00", line_amount)
    ]
    assert "0.01 plus rounding of a four-decimal unit price" in mismatches[0].message


def test_rounded_four_decimal_unit_price_on_a_large_quantity_passes() -> None:
    # 3000 units at one third: printed as 0.3333, which multiplies out to 999.90 rather than 1000.
    issues = validate(
        line_items=[line(quantity="3000", unit_price="0.3333", line_amount="1000.00")]
    )

    assert with_code(issues, ValidationCode.LINE_ITEM_AMOUNT_MISMATCH) == []


def test_rounding_allowance_stops_at_what_rounding_can_explain() -> None:
    # 3000 x 0.00005 + 0.01 allows 0.16, and the amount here is 0.17 away from 999.90.
    within = line(quantity="3000", unit_price="0.3333", line_amount="1000.06")
    beyond = line(quantity="3000", unit_price="0.3333", line_amount="1000.07")

    assert with_code(validate(line_items=[within]), ValidationCode.LINE_ITEM_AMOUNT_MISMATCH) == []
    assert (
        len(with_code(validate(line_items=[beyond]), ValidationCode.LINE_ITEM_AMOUNT_MISMATCH)) == 1
    )


def test_negative_quantity_gets_the_same_rounding_allowance() -> None:
    issues = validate(
        line_items=[line(quantity="-3000", unit_price="0.3333", line_amount="-1000.00")]
    )

    assert with_code(issues, ValidationCode.LINE_ITEM_AMOUNT_MISMATCH) == []


@pytest.mark.parametrize(
    "item",
    [
        line(quantity=None),
        line(unit_price=None),
        line(line_amount=None),
    ],
)
def test_unknown_line_values_never_produce_an_amount_mismatch(item: LineItemData) -> None:
    assert with_code(validate(line_items=[item]), ValidationCode.LINE_ITEM_AMOUNT_MISMATCH) == []


def test_each_mismatched_line_is_named() -> None:
    issues = validate(
        line_items=[
            line(1, line_amount="50.00", quantity="1", unit_price="50.00"),
            line(2, line_amount="50.00", quantity="1", unit_price="60.00"),
            line(3, line_amount="0.00", quantity="1", unit_price="10.00"),
        ]
    )

    mismatches = with_code(issues, ValidationCode.LINE_ITEM_AMOUNT_MISMATCH)
    assert [issue.field for issue in mismatches] == ["line_items[2].amount", "line_items[3].amount"]


# Dates


@pytest.mark.parametrize(
    "invoice_date", [date(2026, 6, 16), date(2026, 6, 15), date(2016, 6, 15), date(2020, 1, 1)]
)
def test_dates_in_the_allowed_range_pass(invoice_date: date) -> None:
    assert validate(invoice_date=known(invoice_date)) == []


def test_date_more_than_one_day_ahead_is_a_warning() -> None:
    issues = validate(invoice_date=known(date(2026, 6, 17)))

    assert [(i.code, i.severity, i.field, i.actual_value) for i in issues] == [
        (ValidationCode.DATE_IN_FUTURE, Severity.WARNING, "invoice_date", "2026-06-17")
    ]


def test_date_older_than_ten_years_is_a_warning() -> None:
    issues = validate(invoice_date=known(date(2016, 6, 14)))

    assert [(i.code, i.severity, i.field, i.actual_value) for i in issues] == [
        (ValidationCode.DATE_TOO_OLD, Severity.WARNING, "invoice_date", "2016-06-14")
    ]


def test_ten_years_back_from_a_leap_day_does_not_crash() -> None:
    leap_day = date(2028, 2, 29)
    old = make_invoice(invoice_date=known(date(2018, 2, 27)))
    boundary = make_invoice(invoice_date=known(date(2018, 2, 28)))

    assert codes(validate_invoice(old, today=leap_day)) == [ValidationCode.DATE_TOO_OLD]
    assert validate_invoice(boundary, today=leap_day) == []


def test_today_defaults_to_the_real_date() -> None:
    real_today = date.today()
    current = make_invoice(invoice_date=known(real_today))
    future = make_invoice(invoice_date=known(real_today + timedelta(days=5)))

    assert validate_invoice(current) == []
    assert codes(validate_invoice(future)) == [ValidationCode.DATE_IN_FUTURE]


# Currency


@pytest.mark.parametrize("currency", sorted(KNOWN_CURRENCIES))
def test_known_currencies_pass(currency: str) -> None:
    assert validate(currency=known(currency)) == []


def test_required_currency_list_is_present() -> None:
    # fmt: off
    required = {
        "USD", "EUR", "GBP", "JPY", "CNY", "INR", "BDT", "AUD", "CAD", "CHF", "SGD", "AED", "SAR",
        "ZAR", "SEK", "NOK", "DKK", "NZD", "HKD", "KRW", "BRL", "MXN", "PKR", "LKR", "MYR", "THB",
        "IDR", "PHP", "VND", "TRY", "RUB", "PLN", "CZK", "HUF", "ILS", "EGP", "NGN", "KES",
    }
    # fmt: on

    assert required <= KNOWN_CURRENCIES


@pytest.mark.parametrize("currency", ["usd", "US", "USDX", "$", "XXX", "US1", "USD\n", "dollars"])
def test_bad_currency_is_a_warning(currency: str) -> None:
    issues = validate(currency=known(currency))

    assert [(i.code, i.severity, i.field, i.actual_value) for i in issues] == [
        (ValidationCode.INVALID_CURRENCY, Severity.WARNING, "currency", currency)
    ]


# Confidence


def test_found_field_below_threshold_is_flagged_with_confidence_and_reason() -> None:
    field = ExtractedField[Any](value="INV-1", confidence=0.59, reason="Smudged on the scan.")

    issues = validate(invoice_number=field)

    assert [(i.code, i.severity, i.field, i.actual_value) for i in issues] == [
        (ValidationCode.LOW_CONFIDENCE, Severity.WARNING, "invoice_number", "INV-1")
    ]
    assert "0.59" in issues[0].message
    assert "Smudged on the scan." in issues[0].message


def test_confidence_at_the_threshold_passes() -> None:
    assert validate(invoice_number=known("INV-1", LOW_CONFIDENCE_THRESHOLD)) == []


def test_threshold_is_the_documented_value() -> None:
    assert LOW_CONFIDENCE_THRESHOLD == 0.6


def test_null_field_is_missing_not_low_confidence() -> None:
    issues = validate(customer_name=unknown())

    assert codes(issues) == [ValidationCode.MISSING_FIELD]


def test_every_invoice_field_is_checked_for_confidence() -> None:
    issues = validate(
        invoice_number=known("INV-1", 0.1),
        invoice_date=known(date(2026, 3, 15), 0.1),
        supplier_name=known("Northwind", 0.1),
        customer_name=known("Contoso", 0.1),
        subtotal=known(Decimal("100.00"), 0.1),
        tax=known(Decimal("10.00"), 0.1),
        total=known(Decimal("110.00"), 0.1),
        currency=known("USD", 0.1),
    )

    assert [i.field for i in issues] == [
        "invoice_number",
        "invoice_date",
        "supplier_name",
        "customer_name",
        "subtotal",
        "tax",
        "total",
        "currency",
    ]
    assert set(codes(issues)) == {ValidationCode.LOW_CONFIDENCE}


# Extraction warnings and line item presence


def test_each_extraction_warning_is_reported() -> None:
    issues = validate(warnings=["Page 3 could not be read.", "Second warning."])

    assert [(i.code, i.severity, i.message) for i in issues] == [
        (ValidationCode.EXTRACTION_WARNING, Severity.WARNING, "Page 3 could not be read."),
        (ValidationCode.EXTRACTION_WARNING, Severity.WARNING, "Second warning."),
    ]


def test_no_line_items_alone_does_not_flag_for_review() -> None:
    issues = validate(line_items=[])

    assert [(i.code, i.severity) for i in issues] == [(ValidationCode.NO_LINE_ITEMS, Severity.INFO)]
    assert needs_review(issues) is False


# Whole invoices


def test_invoice_with_many_problems_reports_all_of_them() -> None:
    issues = validate(
        invoice_number=unknown("Torn corner."),
        invoice_date=known(date(2030, 1, 1)),
        customer_name=known("Contoso", 0.2),
        subtotal=amount("100.00"),
        tax=amount("-10.00"),
        total=amount("120.00"),
        currency=known("ZZZ"),
        line_items=[
            line(1, line_amount="40.00", quantity="-1", unit_price="50.00"),
            line(2, line_amount=None),
        ],
        warnings=["Possible second invoice on page 2."],
    )

    assert sorted(codes(issues)) == sorted(
        [
            ValidationCode.MISSING_REQUIRED_FIELD,
            ValidationCode.NEGATIVE_AMOUNT,
            ValidationCode.TOTAL_MISMATCH,
            ValidationCode.LINE_ITEMS_INCOMPLETE,
            ValidationCode.LINE_ITEM_NEGATIVE_VALUE,
            ValidationCode.LINE_ITEM_AMOUNT_MISMATCH,
            ValidationCode.DATE_IN_FUTURE,
            ValidationCode.INVALID_CURRENCY,
            ValidationCode.LOW_CONFIDENCE,
            ValidationCode.EXTRACTION_WARNING,
        ]
    )
    assert needs_review(issues) is True


def test_validation_does_not_change_the_invoice() -> None:
    invoice = make_invoice(
        total=amount("999.00"),
        customer_name=unknown("Not printed."),
        line_items=[line(2), line(1, line_amount=None)],
        warnings=["A warning."],
    )
    before = invoice.model_dump()

    validate_invoice(invoice, today=TODAY)

    assert invoice.model_dump() == before


def test_amounts_cannot_be_nan_or_infinite() -> None:
    # The comparisons in the rules rely on this: comparing a NaN Decimal raises.
    with pytest.raises(ValueError):
        LineItemData(position=1, amount=Decimal("NaN"))
    with pytest.raises(ValueError):
        ExtractedField[Decimal](value=Decimal("Infinity"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("110.5000", "110.50"),
        ("110.50", "110.50"),
        ("110", "110.00"),
        ("0", "0.00"),
        ("1E+3", "1000.00"),
        ("-5.1", "-5.10"),
        ("0.3333", "0.3333"),
        ("12345678901234.5678", "12345678901234.5678"),
    ],
)
def test_format_amount(value: str, expected: str) -> None:
    assert format_amount(Decimal(value)) == expected
