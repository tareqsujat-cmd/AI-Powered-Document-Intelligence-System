from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from app.core.config import Settings
from app.db.enums import ExtractionMethod, FileType
from app.extraction.spreadsheet_mapper import MAPPED_CONFIDENCE, map_sheet
from app.processing.results import PageResult, SheetTable
from app.processing.spreadsheets import iter_spreadsheet_pages


def sheet(
    headers: list[str],
    rows: list[list[str]],
    preamble: list[list[str]] | None = None,
    **kwargs: object,
) -> PageResult:
    return PageResult(
        page_number=1,
        text="x",
        table=SheetTable(headers=headers, rows=rows, preamble=preamble or []),
        **kwargs,
    )


class TestOneRowPerInvoice:
    HEADERS = [
        "Invoice No",
        "Invoice Date",
        "Supplier",
        "Customer",
        "Subtotal",
        "Tax",
        "Total",
        "Currency",
    ]

    def test_each_row_is_one_invoice_with_every_field(self) -> None:
        invoices = map_sheet(
            sheet(
                self.HEADERS,
                [
                    [
                        "INV-1",
                        "2026-03-15",
                        "Northwind",
                        "Contoso",
                        "100.00",
                        "10.00",
                        "110.00",
                        "usd",
                    ],
                    ["INV-2", "2026-03-16", "Northwind", "Fabrikam", "200", "20", "220", "EUR"],
                ],
            )
        )

        assert invoices is not None
        assert len(invoices) == 2
        first, second = invoices
        assert first.invoice_number.value == "INV-1"
        assert first.invoice_date.value == date(2026, 3, 15)
        assert first.supplier_name.value == "Northwind"
        assert first.customer_name.value == "Contoso"
        assert first.subtotal.value == Decimal("100.00")
        assert first.tax.value == Decimal("10.00")
        assert first.total.value == Decimal("110.00")
        assert first.currency.value == "USD"
        assert second.total.value == Decimal("220")
        assert first.method is ExtractionMethod.RULE_BASED
        assert first.total.confidence == MAPPED_CONFIDENCE
        assert first.total.source_snippet == "Total: 110.00"
        assert first.total.source_page == 1

    @pytest.mark.parametrize(
        ("number", "date_label", "total"),
        [
            ("Invoice Number", "Date", "Grand Total"),
            ("INVOICE #", "Issue Date", "Amount Due"),
            ("Inv. No.", "Bill Date", "total amount"),
            ("invoice id", "invoice date", "Invoice Total"),
        ],
    )
    def test_common_column_names_are_recognized(
        self, number: str, date_label: str, total: str
    ) -> None:
        invoices = map_sheet(sheet([number, date_label, total], [["A-1", "2026-01-02", "5.00"]]))

        assert invoices is not None
        assert invoices[0].invoice_number.value == "A-1"
        assert invoices[0].invoice_date.value == date(2026, 1, 2)
        assert invoices[0].total.value == Decimal("5.00")

    def test_a_column_that_is_absent_stays_unknown_with_a_reason(self) -> None:
        invoices = map_sheet(sheet(["Invoice No", "Total"], [["A-1", "5.00"]]))

        assert invoices is not None
        assert invoices[0].supplier_name.value is None
        assert invoices[0].supplier_name.confidence == 0.0
        assert invoices[0].supplier_name.reason

    def test_a_blank_cell_stays_unknown(self) -> None:
        invoices = map_sheet(sheet(["Invoice No", "Tax", "Total"], [["A-1", "", "5.00"]]))

        assert invoices is not None
        assert invoices[0].tax.value is None

    @pytest.mark.parametrize("bad", ["TBD", "see note", "n/a"])
    def test_a_non_numeric_amount_is_null_with_a_reason_not_a_guess(self, bad: str) -> None:
        invoices = map_sheet(sheet(["Invoice No", "Total"], [["A-1", bad]]))

        assert invoices is not None
        assert invoices[0].total.value is None
        assert "not a number" in invoices[0].total.reason

    def test_an_ambiguous_date_is_null_rather_than_a_guess(self) -> None:
        invoices = map_sheet(sheet(["Invoice No", "Date", "Total"], [["A-1", "03/04/2026", "5"]]))

        assert invoices is not None
        assert invoices[0].invoice_date.value is None
        assert "either way" in invoices[0].invoice_date.reason

    def test_an_invalid_date_is_null(self) -> None:
        invoices = map_sheet(sheet(["Invoice No", "Date", "Total"], [["A-1", "soon", "5"]]))

        assert invoices is not None
        assert invoices[0].invoice_date.value is None
        assert "not a date" in invoices[0].invoice_date.reason

    def test_invalid_currency_is_null(self) -> None:
        invoices = map_sheet(sheet(["Invoice No", "Currency", "Total"], [["A-1", "dollars", "5"]]))

        assert invoices is not None
        assert invoices[0].currency.value is None

    def test_rows_shorter_than_the_headers_are_handled(self) -> None:
        # Trailing blank cells are dropped by the spreadsheet reader.
        invoices = map_sheet(sheet(["Invoice No", "Total", "Note"], [["A-1", "5.00"]]))

        assert invoices is not None
        assert invoices[0].total.value == Decimal("5.00")

    def test_european_amount_format(self) -> None:
        invoices = map_sheet(sheet(["Invoice No", "Total"], [["A-1", "1.234,56"]]))

        assert invoices is not None
        assert invoices[0].total.value == Decimal("1234.56")

    def test_page_warnings_are_carried_over(self) -> None:
        page = sheet(
            ["Invoice No", "Total"], [["A-1", "5"]], warnings=["3 formulas have no value."]
        )

        invoices = map_sheet(page)

        assert invoices is not None
        assert invoices[0].warnings == ["3 formulas have no value."]


class TestSeveralRowsPerInvoice:
    HEADERS = ["Invoice No", "Supplier", "Description", "Qty", "Unit Price", "Amount", "Total"]

    def test_rows_are_grouped_by_invoice_with_line_items(self) -> None:
        invoices = map_sheet(
            sheet(
                self.HEADERS,
                [
                    ["A-1", "Northwind", "Widget", "2", "5.00", "10.00", "30.00"],
                    ["A-1", "Northwind", "Gadget", "1", "20.00", "20.00", "30.00"],
                    ["A-2", "Southwind", "Bolt", "4", "1.00", "4.00", "4.00"],
                ],
            )
        )

        assert invoices is not None
        assert [invoice.invoice_number.value for invoice in invoices] == ["A-1", "A-2"]
        first = invoices[0]
        assert first.total.value == Decimal("30.00")
        assert [item.description for item in first.line_items] == ["Widget", "Gadget"]
        assert [item.position for item in first.line_items] == [1, 2]
        assert first.line_items[1].quantity == Decimal("1")
        assert first.line_items[1].unit_price == Decimal("20.00")
        assert first.line_items[1].amount == Decimal("20.00")
        assert invoices[1].supplier_name.value == "Southwind"

    def test_invoice_number_only_on_the_first_row_is_carried_down(self) -> None:
        invoices = map_sheet(
            sheet(
                ["Invoice No", "Description", "Amount"],
                [["A-1", "Widget", "10"], ["", "Gadget", "20"], ["A-2", "Bolt", "4"]],
            )
        )

        assert invoices is not None
        assert len(invoices) == 2
        assert [item.description for item in invoices[0].line_items] == ["Widget", "Gadget"]

    def test_rows_before_the_first_number_are_skipped_with_a_warning(self) -> None:
        invoices = map_sheet(
            sheet(
                ["Invoice No", "Description", "Amount"],
                [["", "Stray row", "1"], ["A-1", "Widget", "10"]],
            )
        )

        assert invoices is not None
        assert [item.description for item in invoices[0].line_items] == ["Widget"]
        assert any("1 row(s)" in warning for warning in invoices[0].warnings)

    def test_disagreeing_rows_keep_the_first_value_and_warn(self) -> None:
        invoices = map_sheet(
            sheet(
                ["Invoice No", "Supplier", "Total"],
                [["A-1", "Northwind", "5"], ["A-1", "Southwind", "5"]],
            )
        )

        assert invoices is not None
        assert invoices[0].supplier_name.value == "Northwind"
        assert any("disagree on supplier_name" in warning for warning in invoices[0].warnings)

    def test_varying_total_column_means_line_amounts_not_invoice_totals(self) -> None:
        invoices = map_sheet(
            sheet(
                ["Invoice No", "Description", "Total"],
                [["A-1", "Widget", "10"], ["A-1", "Gadget", "20"]],
            )
        )

        assert invoices is not None
        assert invoices[0].total.value is None
        assert [item.amount for item in invoices[0].line_items] == [Decimal("10"), Decimal("20")]

    def test_unreadable_line_amount_is_null(self) -> None:
        invoices = map_sheet(
            sheet(["Invoice No", "Description", "Amount"], [["A-1", "Widget", "TBD"]])
        )

        assert invoices is not None
        assert invoices[0].line_items[0].description == "Widget"
        assert invoices[0].line_items[0].amount is None


class TestSingleInvoiceSheet:
    def page(self) -> PageResult:
        return sheet(
            ["Description", "Qty", "Unit Price", "Amount"],
            [
                ["Widget", "2", "5.00", "10.00"],
                ["Gadget", "1", "20.00", "20.00"],
                ["Subtotal", "", "", "30.00"],
                ["VAT (10%)", "", "", "3.00"],
                ["Total", "", "", "33.00"],
            ],
            preamble=[
                ["Invoice No:", "INV-9"],
                ["Invoice Date:", "2026-03-15"],
                ["Supplier: Northwind Traders"],
                ["Bill To:", "Contoso"],
                ["Currency", "GBP"],
            ],
        )

    def test_details_above_lines_in_and_totals_below_the_table(self) -> None:
        invoices = map_sheet(self.page())

        assert invoices is not None
        invoice = invoices[0]
        assert invoice.invoice_number.value == "INV-9"
        assert invoice.invoice_date.value == date(2026, 3, 15)
        assert invoice.supplier_name.value == "Northwind Traders"
        assert invoice.customer_name.value == "Contoso"
        assert invoice.currency.value == "GBP"
        assert invoice.subtotal.value == Decimal("30.00")
        assert invoice.tax.value == Decimal("3.00")
        assert invoice.total.value == Decimal("33.00")

    def test_summary_rows_do_not_become_line_items(self) -> None:
        invoices = map_sheet(self.page())

        assert invoices is not None
        assert [item.description for item in invoices[0].line_items] == ["Widget", "Gadget"]

    def test_total_column_on_a_line_table_is_the_line_amount(self) -> None:
        invoices = map_sheet(
            sheet(
                ["Description", "Total"],
                [["Widget", "10"]],
                preamble=[["Invoice No:", "A-1"]],
            )
        )

        assert invoices is not None
        assert invoices[0].line_items[0].amount == Decimal("10")
        assert invoices[0].total.value is None

    def test_unambiguous_symbol_in_amounts_states_the_currency(self) -> None:
        invoices = map_sheet(
            sheet(
                ["Description", "Amount"],
                [["Widget", "€10.00"]],
                preamble=[["Invoice No:", "A-1"]],
            )
        )

        assert invoices is not None
        assert invoices[0].currency.value == "EUR"
        assert invoices[0].line_items[0].amount == Decimal("10.00")

    def test_dollar_sign_does_not_assign_a_currency(self) -> None:
        invoices = map_sheet(
            sheet(
                ["Description", "Amount"], [["Widget", "$10.00"]], preamble=[["Invoice No:", "A-1"]]
            )
        )

        assert invoices is not None
        assert invoices[0].currency.value is None

    def test_values_in_the_preamble_are_validated_too(self) -> None:
        invoices = map_sheet(
            sheet(
                ["Description", "Amount"],
                [["Widget", "1"]],
                preamble=[["Invoice No:", "A-1"], ["Invoice Date:", "whenever"]],
            )
        )

        assert invoices is not None
        assert invoices[0].invoice_date.value is None


class TestFallsBackToLLM:
    @pytest.mark.parametrize(
        ("headers", "rows", "preamble"),
        [
            (["Ref", "Thing", "Cost"], [["A", "B", "1"]], None),
            ([], [["A-1", "5"]], None),
            (["Invoice No", "Total"], [], None),
            # A line table with no invoice number above it does not identify an invoice.
            (["Description", "Amount"], [["Widget", "1"]], None),
            # An invoice number column with no amount at all.
            (["Invoice No", "Note"], [["A-1", "hello"]], None),
            # A preamble number with nothing to read amounts from.
            (["Description", "Note"], [["Widget", "x"]], [["Invoice No:", "A-1"]]),
        ],
    )
    def test_sheets_it_cannot_be_sure_of_return_none(
        self, headers: list[str], rows: list[list[str]], preamble: list[list[str]] | None
    ) -> None:
        assert map_sheet(sheet(headers, rows, preamble)) is None

    def test_page_with_no_table_returns_none(self) -> None:
        assert map_sheet(PageResult(page_number=1, text="x")) is None


class TestRealFiles:
    def test_xlsx_with_preamble_and_totals_end_to_end(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        for row in (
            ["Invoice No:", "INV-77"],
            ["Invoice Date:", date(2026, 3, 15)],
            ["Supplier:", "Northwind"],
            [],
            ["Description", "Qty", "Unit Price", "Amount"],
            ["Widget", 2, 5.5, 11.0],
            ["Gadget", 1, 20, 20],
            ["Total", None, None, 31.0],
        ):
            worksheet.append(row)
        path = tmp_path / "invoice.xlsx"
        workbook.save(path)

        page = next(iter_spreadsheet_pages(path, FileType.XLSX, settings))
        invoices = map_sheet(page)

        assert invoices is not None
        invoice = invoices[0]
        assert invoice.invoice_number.value == "INV-77"
        assert invoice.invoice_date.value == date(2026, 3, 15)
        assert invoice.total.value == Decimal("31")
        assert [item.amount for item in invoice.line_items] == [Decimal("11"), Decimal("20")]
        assert invoice.line_items[0].unit_price == Decimal("5.5")

    def test_semicolon_csv_with_european_numbers_end_to_end(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        path = tmp_path / "invoices.csv"
        path.write_text(
            "Rechnung;x\nInvoice No;Supplier;Total\n"
            "A-1;Müller GmbH;1.234,56\nA-2;Müller GmbH;7,50\n",
            encoding="utf-8",
        )

        page = next(iter_spreadsheet_pages(path, FileType.CSV, settings))
        invoices = map_sheet(page)

        assert invoices is not None
        assert [invoice.total.value for invoice in invoices] == [
            Decimal("1234.56"),
            Decimal("7.50"),
        ]
        assert invoices[0].supplier_name.value == "Müller GmbH"
