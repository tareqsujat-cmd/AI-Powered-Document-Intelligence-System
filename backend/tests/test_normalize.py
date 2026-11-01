from datetime import date
from decimal import Decimal

import pytest

from app.extraction.normalize import (
    amount_candidates,
    amounts_in_text,
    dates_in_text,
    duplicate_key,
    normalize_key,
    normalize_party_name,
    ocr_tolerant_key,
    parse_amount,
    parse_date,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,234.56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("1234.56", "1234.56"),
        ("1234,56", "1234.56"),
        ("1 234,56", "1234.56"),
        ("1 234.56", "1234.56"),
        ("1'234.56", "1234.56"),
        ("1,234,567.89", "1234567.89"),
        ("1.234.567,89", "1234567.89"),
        ("1,234,567", "1234567"),
        ("1,234", "1234"),
        ("0,123", "0.123"),
        ("12.5", "12.5"),
        ("12,5", "12.5"),
        ("100", "100"),
        ("0", "0"),
        ("0.00", "0.00"),
        ("$1,234.56", "1234.56"),
        ("€ 1.234,56", "1234.56"),
        ("1,234.56 USD", "1234.56"),
        ("USD 1,234.56", "1234.56"),
        ("৳500", "500"),
        ("(100.00)", "-100.00"),
        ("-100.00", "-100.00"),
        ("100.00-", "-100.00"),
        ("-$100.00", "-100.00"),
        ("$-100.00", "-100.00"),
        ("100.00 CR", "-100.00"),
        ("1,00,000.00", "100000.00"),
        ("12,34,567", "1234567"),
    ],
)
def test_parse_amount(text: str, expected: str) -> None:
    assert parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text", ["", None, "TBD", "N/A", "abc", "-", "$", "12/05/2026", "1.2.3,4,5", "1" * 20]
)
def test_parse_amount_refuses_what_is_not_an_amount(text: str | None) -> None:
    assert parse_amount(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.234", ["1.234", "1234"]),
        ("1,234", ["1234", "1.234"]),
        ("1.50", ["1.50"]),
    ],
)
def test_ambiguous_amounts_offer_every_reading(text: str, expected: list[str]) -> None:
    assert amount_candidates(text) == [Decimal(value) for value in expected]


def test_amounts_in_text_finds_values_in_invoice_lines() -> None:
    found = amounts_in_text("Widget  4  312.50\t1,250.00\nTotal: $1,762.75")

    assert {Decimal("4"), Decimal("312.50"), Decimal("1250"), Decimal("1762.75")} <= found


def test_amounts_in_text_does_not_invent_values() -> None:
    assert Decimal("999.99") not in amounts_in_text("Widget 4 312.50 1,250.00")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-03-15", date(2026, 3, 15)),
        ("2026/03/15", date(2026, 3, 15)),
        ("15/03/2026", date(2026, 3, 15)),
        ("03/15/2026", date(2026, 3, 15)),
        ("15.03.2026", date(2026, 3, 15)),
        ("15-03-26", date(2026, 3, 15)),
        ("15 March 2026", date(2026, 3, 15)),
        ("15th March, 2026", date(2026, 3, 15)),
        ("March 15, 2026", date(2026, 3, 15)),
        ("Mar 15 2026", date(2026, 3, 15)),
        ("15-Mar-2026", date(2026, 3, 15)),
        ("15-Sept-26", date(2026, 9, 15)),
        ("5/5/2026", date(2026, 5, 5)),
    ],
)
def test_parse_date(text: str, expected: date) -> None:
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", ["", None, "soon", "2026-13-45", "32/13/2026", "31 Feb 2026"])
def test_parse_date_refuses_what_is_not_a_date(text: str | None) -> None:
    assert parse_date(text) is None


def test_parse_date_refuses_to_pick_between_day_and_month_order() -> None:
    assert parse_date("03/04/2026") is None


def test_dates_in_text_offers_both_readings_and_marks_them_ambiguous() -> None:
    found = dates_in_text("Invoice Date: 03/04/2026")

    assert (date(2026, 4, 3), True) in found
    assert (date(2026, 3, 4), True) in found


def test_dates_in_text_finds_unambiguous_dates_among_words() -> None:
    found = dates_in_text("Issued on 2026-03-15, due by 15 April 2026.")

    assert (date(2026, 3, 15), False) in found
    assert (date(2026, 4, 15), False) in found


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("INV-0042", "inv 0042"),
        ("INV/0042", "INV.0042"),
        ("  INV-0042 ", "INV0042"),
    ],
)
def test_normalize_key_ignores_formatting(first: str, second: str) -> None:
    assert normalize_key(first) == normalize_key(second)


def test_normalize_key_keeps_different_numbers_apart() -> None:
    assert normalize_key("INV-0042") != normalize_key("INV-0043")
    assert normalize_key(None) == ""


def test_ocr_tolerant_key_equates_confusable_characters() -> None:
    assert ocr_tolerant_key("INV-0O42") == ocr_tolerant_key("INV-0042")
    assert ocr_tolerant_key("1NV-0042") == ocr_tolerant_key("INV-0042")
    assert ocr_tolerant_key("INV-0042") != ocr_tolerant_key("INV-0043")


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Northwind Traders Ltd", "NORTHWIND TRADERS LTD."),
        ("Northwind Traders Ltd", "Northwind Traders Limited"),
        ("Northwind Traders, Inc.", "Northwind Traders"),
        ("Müller GmbH", "Muller GmbH"),
    ],
)
def test_normalize_party_name_ignores_case_punctuation_and_legal_form(
    first: str, second: str
) -> None:
    assert normalize_party_name(first) == normalize_party_name(second)


def test_normalize_party_name_keeps_different_companies_apart() -> None:
    assert normalize_party_name("Northwind Traders") != normalize_party_name("Southwind Traders")


def test_duplicate_key_is_stable_across_formatting() -> None:
    first = duplicate_key("Northwind Ltd", "INV-1", date(2026, 3, 15), Decimal("100.00"))
    second = duplicate_key("NORTHWIND LIMITED", "inv 1", date(2026, 3, 15), Decimal("100"))

    assert first == second
    assert first is not None
    assert len(first) == 64


@pytest.mark.parametrize(
    "changed",
    [
        ("Southwind Ltd", "INV-1", date(2026, 3, 15), Decimal("100")),
        ("Northwind Ltd", "INV-2", date(2026, 3, 15), Decimal("100")),
        ("Northwind Ltd", "INV-1", date(2026, 3, 16), Decimal("100")),
        ("Northwind Ltd", "INV-1", date(2026, 3, 15), Decimal("100.01")),
    ],
)
def test_duplicate_key_changes_with_each_part(changed: tuple) -> None:
    base = duplicate_key("Northwind Ltd", "INV-1", date(2026, 3, 15), Decimal("100"))

    assert duplicate_key(*changed) != base


@pytest.mark.parametrize(
    "missing",
    [
        (None, "INV-1", date(2026, 3, 15), Decimal("100")),
        ("Northwind", None, date(2026, 3, 15), Decimal("100")),
        ("Northwind", "INV-1", None, Decimal("100")),
        ("Northwind", "INV-1", date(2026, 3, 15), None),
        ("", "INV-1", date(2026, 3, 15), Decimal("100")),
    ],
)
def test_duplicate_key_is_none_when_any_part_is_unknown(missing: tuple) -> None:
    assert duplicate_key(*missing) is None


def test_zero_total_still_produces_a_duplicate_key() -> None:
    assert duplicate_key("Northwind", "INV-1", date(2026, 3, 15), Decimal("0")) is not None
