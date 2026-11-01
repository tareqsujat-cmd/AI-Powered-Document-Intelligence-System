"""Exact rules for spreadsheets with recognizable columns, so they need no LLM call.

Cells are facts rather than OCR guesses, so a mapped value carries high confidence. If a sheet
cannot be mapped with certainty, map_sheet returns None and the caller falls back to the LLM.
"""

import re
from collections.abc import Sequence
from typing import Any

import pandas as pd

from app.db.enums import ExtractionMethod
from app.extraction.normalize import dates_in_text, parse_amount
from app.extraction.schemas import ExtractedField, InvoiceData, LineItemData
from app.processing.results import PageResult, SheetTable

MAPPED_CONFIDENCE = 0.95

_LABELS: dict[str, frozenset[str]] = {
    "invoice_number": frozenset(
        {
            "invoice no",
            "invoice number",
            "invoice num",
            "invoice nbr",
            "invoice",
            "invoice id",
            "inv no",
            "inv number",
            "inv",
            "bill no",
            "bill number",
        }
    ),
    "invoice_date": frozenset({"invoice date", "date", "issue date", "bill date", "inv date"}),
    "supplier_name": frozenset(
        {"supplier", "supplier name", "vendor", "vendor name", "seller", "from", "company"}
    ),
    "customer_name": frozenset(
        {"customer", "customer name", "client", "client name", "buyer", "bill to", "billed to"}
    ),
    "subtotal": frozenset({"subtotal", "sub total", "net total", "net amount", "taxable amount"}),
    "tax": frozenset(
        {"tax", "tax amount", "total tax", "vat", "vat amount", "gst", "gst amount", "sales tax"}
    ),
    "total": frozenset(
        {
            "total",
            "grand total",
            "total amount",
            "invoice total",
            "amount due",
            "total due",
            "balance due",
            "gross total",
        }
    ),
    "currency": frozenset({"currency", "currency code", "ccy"}),
}
_LINE_LABELS: dict[str, frozenset[str]] = {
    "description": frozenset(
        {
            "description",
            "item",
            "item description",
            "product",
            "product name",
            "service",
            "details",
            "particulars",
        }
    ),
    "quantity": frozenset({"qty", "quantity", "units", "hours"}),
    "unit_price": frozenset({"unit price", "price", "rate", "unit cost", "price per unit"}),
    "amount": frozenset({"amount", "line total", "extended price", "ext price", "line amount"}),
}
_UNAMBIGUOUS_SYMBOLS = {"€": "EUR", "£": "GBP", "₹": "INR", "৳": "BDT"}
_CURRENCY_CODE = re.compile(r"^[A-Za-z]{3}$")
_SUMMARY_FIELDS = ("subtotal", "tax", "total")


def _normalize_label(text: str) -> str:
    # "Tax (10%)" and "VAT 20%" name the tax column, and "Invoice #" is the same as "Invoice No".
    cleaned = re.sub(r"\(.*?\)|\d+(?:\.\d+)?\s*%", " ", text.casefold())
    cleaned = cleaned.replace("#", " no ").replace(".", " ")
    return " ".join(re.sub(r"[^\w\s]", " ", cleaned).split())


def _label_field(text: str, labels: dict[str, frozenset[str]]) -> str | None:
    label = _normalize_label(text)
    for name, known in labels.items():
        if label in known:
            return name
    return None


def _cell_field(name: str, raw: str, header: str, page_number: int) -> ExtractedField[Any]:
    """Read one cell, keeping the value only if it is what it claims to be."""
    snippet = f"{header}: {raw}"
    value: Any = raw
    if name in _SUMMARY_FIELDS:
        value = parse_amount(raw)
        problem = f"The cell {raw!r} under {header!r} is not a number."
    elif name == "invoice_date":
        readings = dates_in_text(raw)
        distinct = {reading for reading, _ in readings}
        if len(distinct) > 1:
            return ExtractedField(
                reason=f"The date {raw!r} could be read with the day and month either way.",
                source_snippet=snippet,
                source_page=page_number,
            )
        value = readings[0][0] if readings else None
        problem = f"The cell {raw!r} under {header!r} is not a date."
    elif name == "currency":
        code = raw.strip().upper()
        value = code if _CURRENCY_CODE.match(code) else None
        problem = f"The cell {raw!r} under {header!r} is not a currency code."
    else:
        problem = ""

    if value is None:
        return ExtractedField(reason=problem, source_snippet=snippet, source_page=page_number)
    return ExtractedField(
        value=value,
        confidence=MAPPED_CONFIDENCE,
        source_snippet=snippet,
        source_page=page_number,
    )


def _preamble_values(preamble: Sequence[Sequence[str]]) -> dict[str, tuple[str, str]]:
    """Pick out "Invoice No: | INV-1", and "Invoice No: INV-1" written in a single cell."""
    found: dict[str, tuple[str, str]] = {}
    for row in preamble:
        cells = [cell for cell in row if cell]
        pairs = zip(cells, cells[1:], strict=False)
        for label, value in pairs:
            name = _label_field(label, _LABELS)
            if name:
                found.setdefault(name, (label.rstrip(": "), value))
        for cell in cells:
            if ":" in cell:
                label, _, value = cell.partition(":")
                name = _label_field(label, _LABELS)
                if name and value.strip():
                    found.setdefault(name, (label.strip(), value.strip()))
    return found


def _summary_row(row: Sequence[str]) -> tuple[str, str, str] | None:
    """Recognize a "Total | | 110.00" row below a table, returning (field, label, value)."""
    cells = [cell for cell in row if cell]
    if len(cells) < 2:
        return None
    name = _label_field(cells[0], _LABELS)
    if name not in _SUMMARY_FIELDS or name is None:
        return None
    return name, cells[0].rstrip(": "), cells[-1]


def _to_frame(table: SheetTable) -> pd.DataFrame:
    """A frame of strings. Rows can be shorter than the header: trailing blanks are dropped."""
    width = len(table.headers)
    rows = [(row + [""] * width)[:width] for row in table.rows]
    # Positional names, because a sheet can repeat or leave blank a header.
    return pd.DataFrame(rows, columns=range(width), dtype="string").fillna("")


def _find_columns(headers: Sequence[str], labels: dict[str, frozenset[str]]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for index, header in enumerate(headers):
        name = _label_field(header, labels) if header else None
        if name and name not in columns:
            columns[name] = index
    return columns


def _line_items(
    frame: pd.DataFrame, columns: dict[str, int], page_number: int
) -> list[LineItemData]:
    items: list[LineItemData] = []
    for row in frame.itertuples(index=False, name=None):

        def cell(name: str, values: tuple[Any, ...] = row) -> str:
            return str(values[columns[name]]) if name in columns else ""

        description, amount = cell("description"), cell("amount")
        quantity, unit_price = cell("quantity"), cell("unit_price")
        if not (description or amount or quantity or unit_price):
            continue
        items.append(
            LineItemData(
                position=len(items) + 1,
                description=description or None,
                quantity=parse_amount(quantity),
                unit_price=parse_amount(unit_price),
                amount=parse_amount(amount),
                confidence=MAPPED_CONFIDENCE,
                source_snippet=" | ".join(cell for cell in row if cell)[:300],
                source_page=page_number,
            )
        )
    return items


def _currency_from_symbols(frame: pd.DataFrame) -> tuple[str, str] | None:
    """A "€" in amount cells states the currency, when no column does. "$" is too ambiguous."""
    for symbol, code in _UNAMBIGUOUS_SYMBOLS.items():
        for column in frame.columns:
            matches = frame[column][frame[column].str.contains(symbol, regex=False)]
            if not matches.empty:
                return code, str(matches.iloc[0])
    return None


def _set_symbol_currency(invoice: InvoiceData, frame: pd.DataFrame, page_number: int) -> None:
    if invoice.currency.value is not None:
        return
    found = _currency_from_symbols(frame)
    if found:
        invoice.currency = ExtractedField(
            value=found[0],
            confidence=MAPPED_CONFIDENCE,
            source_snippet=found[1][:300],
            source_page=page_number,
        )


def _base_invoice(page: PageResult) -> InvoiceData:
    return InvoiceData(
        page_start=page.page_number,
        page_end=page.page_number,
        method=ExtractionMethod.RULE_BASED,
        warnings=list(page.warnings),
    )


def _map_single_invoice(page: PageResult, table: SheetTable) -> list[InvoiceData] | None:
    """One invoice on a sheet: details above the table, line items in it, totals below it."""
    columns = _find_columns(table.headers, _LINE_LABELS)
    # On a line-item table, "Total" means the row's amount rather than the invoice's total.
    if "amount" not in columns:
        total_column = _find_columns(table.headers, {"amount": _LABELS["total"]}).get("amount")
        if total_column is not None:
            columns["amount"] = total_column
    preamble = _preamble_values(table.preamble)
    if "invoice_number" not in preamble or "amount" not in columns:
        return None

    line_rows: list[list[str]] = []
    invoice = _base_invoice(page)
    for name, (label, value) in preamble.items():
        setattr(invoice, name, _cell_field(name, value, label, page.page_number))
    for row in table.rows:
        summary = _summary_row(row)
        if summary is None:
            line_rows.append(row)
        else:
            name, label, value = summary
            setattr(invoice, name, _cell_field(name, value, label, page.page_number))

    frame = _to_frame(SheetTable(headers=table.headers, rows=line_rows))
    invoice.line_items = _line_items(frame, columns, page.page_number)
    _set_symbol_currency(invoice, frame, page.page_number)
    return [invoice]


def _map_invoice_rows(page: PageResult, table: SheetTable) -> list[InvoiceData] | None:
    """A column of invoice numbers: each number is one invoice, on one row or several."""
    columns = _find_columns(table.headers, _LABELS)
    line_columns = _find_columns(table.headers, _LINE_LABELS)
    if "invoice_number" not in columns:
        return None
    if "total" not in columns and "amount" not in line_columns:
        return None

    frame = _to_frame(table)
    number_column = columns["invoice_number"]
    # Exports often print the number only on an invoice's first row.
    numbers = frame[number_column].replace("", pd.NA).ffill()
    unassigned = int(numbers.isna().sum())
    grouped = frame[numbers.notna()].groupby(numbers[numbers.notna()], sort=False)

    invoices: list[InvoiceData] = []
    for number, group in grouped:
        invoice = _base_invoice(page)
        if unassigned:
            invoice.warnings.append(
                f"{unassigned} row(s) above the first invoice number were skipped."
            )
        invoice.invoice_number = _cell_field(
            "invoice_number", str(number), table.headers[number_column], page.page_number
        )
        for name, column in columns.items():
            if name == "invoice_number":
                continue
            values = [value for value in group[column].unique() if value]
            if not values:
                continue
            # A total repeated on each row is the invoice's. Varying totals are per-line amounts.
            if len(values) > 1 and name == "total" and "amount" not in line_columns:
                continue
            if len(values) > 1:
                invoice.warnings.append(
                    f"Rows of invoice {number} disagree on {name}; the first value was kept."
                )
            setattr(
                invoice,
                name,
                _cell_field(name, str(values[0]), table.headers[column], page.page_number),
            )

        item_columns = dict(line_columns)
        if "amount" not in item_columns and "total" in columns and "description" in item_columns:
            item_columns["amount"] = columns["total"]
        if "description" in item_columns or "amount" in item_columns:
            invoice.line_items = _line_items(group, item_columns, page.page_number)
        _set_symbol_currency(invoice, group, page.page_number)
        invoices.append(invoice)
    return invoices or None


def map_sheet(page: PageResult) -> list[InvoiceData] | None:
    """Map a sheet by its column names, or return None if the LLM should look at it instead."""
    table = page.table
    if table is None or not table.headers or not table.rows:
        return None
    return _map_invoice_rows(page, table) or _map_single_invoice(page, table)
