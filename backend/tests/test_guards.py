from datetime import date
from decimal import Decimal

import pytest

from app.extraction.guards import (
    NOT_IN_SOURCE,
    SourcePage,
    guard_amount,
    guard_currency,
    guard_date,
    guard_name,
    guard_text_id,
)

INVOICE = """INVOICE
Invoice Number: INV-2026-0042
Invoice Date: 2026-03-15
Supplier: Northwind Traders Ltd\tBill To: Contoso Retail Inc
Description\tQty\tUnit Price\tAmount
Industrial Bearing Kit\t4\t312.50\t1,250.00
Subtotal\t1,250.00
Tax (10%)\t125.00
Total\t1,375.00"""
PAGES = [SourcePage(1, INVOICE)]


def assert_discarded(result) -> None:
    assert result.value is None
    assert result.confidence == 0.0
    assert result.reason == NOT_IN_SOURCE


class TestIdentifiers:
    def test_value_in_the_document_is_kept_with_its_evidence(self) -> None:
        result = guard_text_id("INV-2026-0042", 0.95, None, "Invoice Number: INV-2026-0042", PAGES)

        assert result.value == "INV-2026-0042"
        assert result.confidence == 0.95
        assert result.source_snippet == "Invoice Number: INV-2026-0042"
        assert result.source_page == 1

    def test_invented_number_is_discarded(self) -> None:
        assert_discarded(guard_text_id("INV-9999", 0.99, None, "Invoice Number: INV-9999", PAGES))

    def test_formatting_differences_are_accepted(self) -> None:
        assert guard_text_id("INV 2026 0042", 0.9, None, None, PAGES).value == "INV 2026 0042"

    def test_fabricated_snippet_is_replaced_with_the_real_line(self) -> None:
        result = guard_text_id("INV-2026-0042", 0.9, None, "this text is not there at all", PAGES)

        assert result.source_snippet == "Invoice Number: INV-2026-0042"

    def test_ocr_confusion_keeps_the_value_with_low_confidence(self) -> None:
        pages = [SourcePage(1, "Invoice Number: INV-2O26-OO42")]

        result = guard_text_id("INV-2026-0042", 0.95, None, None, pages)

        assert result.value == "INV-2026-0042"
        assert result.confidence == 0.7
        assert "OCR" in result.reason

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_value_is_null_and_keeps_the_models_reason(self, value: str | None) -> None:
        result = guard_text_id(value, 0.9, "No number printed.", None, PAGES)

        assert result.value is None
        assert result.confidence == 0.0
        assert result.reason == "No number printed."

    def test_punctuation_only_value_is_discarded(self) -> None:
        assert_discarded(guard_text_id("---", 0.9, None, None, PAGES))

    def test_finds_the_page_the_value_is_on(self) -> None:
        pages = [SourcePage(1, "Terms and conditions"), SourcePage(2, "Invoice INV-77")]

        assert guard_text_id("INV-77", 0.9, None, None, pages).source_page == 2


class TestNames:
    def test_name_in_the_document_is_kept(self) -> None:
        result = guard_name("Northwind Traders Ltd", 0.9, None, None, PAGES)

        assert result.value == "Northwind Traders Ltd"
        assert result.confidence == 0.9
        assert "Northwind Traders Ltd" in result.source_snippet

    def test_case_is_ignored(self) -> None:
        assert guard_name("NORTHWIND TRADERS LTD", 0.9, None, None, PAGES).confidence == 0.9

    def test_invented_company_is_discarded(self) -> None:
        assert_discarded(guard_name("Acme Global Holdings", 0.99, None, None, PAGES))

    def test_small_ocr_errors_are_accepted_with_lower_confidence(self) -> None:
        pages = [SourcePage(1, "Supplier: Northw1nd Traders Lfd")]

        result = guard_name("Northwind Traders Ltd", 0.95, None, None, pages)

        assert result.value == "Northwind Traders Ltd"
        assert result.confidence == 0.7
        assert "similar" in result.reason

    def test_a_similar_but_different_company_is_not_accepted(self) -> None:
        assert_discarded(guard_name("Southwest Trading Ltd", 0.9, None, None, PAGES))


class TestAmounts:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1,375.00", "1375.00"),
            ("1375.00", "1375.00"),
            ("1375", "1375"),
            ("$1,375.00", "1375.00"),
        ],
    )
    def test_printed_amount_is_kept_in_any_format(self, value: str, expected: str) -> None:
        result = guard_amount(value, 0.9, None, None, PAGES)

        assert result.value == Decimal(expected)
        assert result.confidence == 0.9
        assert result.source_page == 1

    def test_computed_total_that_is_not_printed_is_discarded(self) -> None:
        pages = [SourcePage(1, "Subtotal 100.00\nTax 10.00")]

        assert_discarded(guard_amount("110.00", 0.99, None, None, pages))

    def test_invented_amount_is_discarded_even_with_full_confidence(self) -> None:
        assert_discarded(guard_amount("9,999.99", 1.0, None, "Total 9,999.99", PAGES))

    def test_one_digit_change_is_discarded(self) -> None:
        assert_discarded(guard_amount("1,375.01", 0.9, None, None, PAGES))

    def test_european_format_in_the_document_matches(self) -> None:
        pages = [SourcePage(1, "Gesamt: 1.234,56 EUR")]

        assert guard_amount("1234.56", 0.9, None, None, pages).value == Decimal("1234.56")

    def test_credit_note_in_parentheses_keeps_its_sign(self) -> None:
        pages = [SourcePage(1, "Total (100.00)")]

        assert guard_amount("(100.00)", 0.9, None, None, pages).value == Decimal("-100.00")

    @pytest.mark.parametrize("value", ["TBD", "see attached", "N/A", "12/05/2026"])
    def test_non_numbers_are_null_with_a_reason(self, value: str) -> None:
        result = guard_amount(value, 0.9, None, None, PAGES)

        assert result.value is None
        assert "not a number" in result.reason

    def test_snippet_not_containing_the_value_is_replaced(self) -> None:
        result = guard_amount("1,375.00", 0.9, None, "Tax (10%) 125.00", PAGES)

        assert result.source_snippet == "Total\t1,375.00"

    @pytest.mark.parametrize("value", [None, ""])
    def test_missing_amount_is_null(self, value: str | None) -> None:
        assert guard_amount(value, 0.9, "No total.", None, PAGES).reason == "No total."


class TestDates:
    def test_printed_date_is_kept(self) -> None:
        result = guard_date("2026-03-15", 0.9, None, None, PAGES)

        assert result.value == date(2026, 3, 15)
        assert result.confidence == 0.9
        assert result.source_snippet == "Invoice Date: 2026-03-15"

    @pytest.mark.parametrize(
        "printed",
        ["15 March 2026", "March 15, 2026", "15/03/2026", "03/15/2026", "15.03.2026", "15-Mar-26"],
    )
    def test_date_format_in_the_document_does_not_matter(self, printed: str) -> None:
        pages = [SourcePage(1, f"Invoice Date: {printed}")]

        result = guard_date("2026-03-15", 0.9, None, None, pages)

        assert result.value == date(2026, 3, 15)
        assert result.confidence == 0.9

    def test_invented_date_is_discarded(self) -> None:
        assert_discarded(guard_date("2026-12-25", 0.99, None, None, PAGES))

    def test_ambiguous_day_and_month_lowers_confidence_and_says_why(self) -> None:
        pages = [SourcePage(1, "Date: 03/04/2026")]

        result = guard_date("2026-04-03", 0.95, None, None, pages)

        assert result.value == date(2026, 4, 3)
        assert result.confidence == 0.6
        assert "either way" in result.reason

    @pytest.mark.parametrize("value", ["soon", "03/04/2026", "2026-13-45"])
    def test_uninterpretable_value_is_null(self, value: str) -> None:
        result = guard_date(value, 0.9, None, None, PAGES)

        assert result.value is None
        assert "not an unambiguous date" in result.reason


class TestCurrency:
    @pytest.mark.parametrize(
        ("text", "code"),
        [
            ("Total: 100.00 EUR", "EUR"),
            ("Total: €100.00", "EUR"),
            ("Total: £100.00", "GBP"),
            ("মোট: ৳500", "BDT"),
            ("Amount in Euro", "EUR"),
            ("Amount in Taka", "BDT"),
        ],
    )
    def test_stated_currency_is_kept(self, text: str, code: str) -> None:
        result = guard_currency(code, 0.9, None, None, [SourcePage(1, text)])

        assert result.value == code
        assert result.confidence == 0.9

    def test_assumed_currency_with_no_evidence_is_discarded(self) -> None:
        assert_discarded(guard_currency("USD", 0.95, None, None, [SourcePage(1, "Total: 100.00")]))

    def test_dollar_sign_alone_is_only_a_hint(self) -> None:
        result = guard_currency("USD", 0.95, None, None, [SourcePage(1, "Total: $100.00")])

        assert result.value == "USD"
        assert result.confidence == 0.6
        assert "several currencies" in result.reason

    def test_wrong_symbol_does_not_support_a_code(self) -> None:
        assert_discarded(guard_currency("GBP", 0.9, None, None, [SourcePage(1, "Total: €100.00")]))

    def test_lowercase_code_is_normalized(self) -> None:
        assert guard_currency("eur", 0.9, None, None, [SourcePage(1, "100 EUR")]).value == "EUR"

    @pytest.mark.parametrize("value", ["dollars", "US", "E"])
    def test_not_a_code_is_null(self, value: str) -> None:
        result = guard_currency(value, 0.9, None, None, PAGES)

        assert result.value is None
        assert "not a currency code" in result.reason

    def test_a_three_letter_word_in_caps_elsewhere_does_not_count_for_a_different_code(
        self,
    ) -> None:
        assert_discarded(guard_currency("SEK", 0.9, None, None, [SourcePage(1, "TAX INV 100 EUR")]))


class TestOcrConfidenceCap:
    def test_value_cannot_be_more_reliable_than_its_pages_ocr(self) -> None:
        pages = [SourcePage(1, "Clean text INV-1", None), SourcePage(2, "Total 500.00", 0.42)]

        assert guard_amount("500.00", 0.95, None, None, pages).confidence == 0.42

    def test_text_layer_pages_are_not_capped(self) -> None:
        assert guard_amount("1,375.00", 0.95, None, None, PAGES).confidence == 0.95
