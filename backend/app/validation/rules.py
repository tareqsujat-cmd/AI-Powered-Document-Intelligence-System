from datetime import date, timedelta
from decimal import Decimal

from app.db.enums import Severity
from app.extraction.schemas import INVOICE_FIELDS, REQUIRED_FIELDS, InvoiceData, LineItemData
from app.validation.issues import ValidationCode, ValidationIssue

TOLERANCE = Decimal("0.01")
# Most a unit price stored to four decimals can have been rounded by, per unit sold.
UNIT_PRICE_ROUNDING = Decimal("0.00005")
LOW_CONFIDENCE_THRESHOLD = 0.6
MAX_INVOICE_AGE_YEARS = 10
FUTURE_DATE_GRACE = timedelta(days=1)
RECOMMENDED_FIELDS = ("customer_name", "subtotal", "tax", "currency")
# fmt: off
KNOWN_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CNY", "INR", "BDT", "AUD", "CAD", "CHF", "SGD", "AED", "SAR",
    "ZAR", "SEK", "NOK", "DKK", "NZD", "HKD", "KRW", "BRL", "MXN", "PKR", "LKR", "MYR", "THB",
    "IDR", "PHP", "VND", "TRY", "RUB", "PLN", "CZK", "HUF", "ILS", "EGP", "NGN", "KES",
})
# fmt: on

_FIELD_LABELS = {
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "supplier_name": "Supplier name",
    "customer_name": "Customer name",
    "subtotal": "Subtotal",
    "tax": "Tax",
    "total": "Total",
    "currency": "Currency",
}


def amounts_match(first: Decimal, second: Decimal) -> bool:
    return abs(first - second) <= TOLERANCE


def format_amount(value: Decimal) -> str:
    """Plain digits with at least two decimals, so 110.5000 from storage reads like 110.50."""
    whole, _, fraction = f"{value:f}".partition(".")
    return f"{whole}.{fraction.rstrip('0').ljust(2, '0')}"


def _has_value(invoice: InvoiceData, name: str) -> bool:
    value = invoice.field(name).value
    return value is not None and not (isinstance(value, str) and not value.strip())


def _check_missing_fields(invoice: InvoiceData) -> list[ValidationIssue]:
    issues = []
    for names, code, severity in (
        (REQUIRED_FIELDS, ValidationCode.MISSING_REQUIRED_FIELD, Severity.ERROR),
        (RECOMMENDED_FIELDS, ValidationCode.MISSING_FIELD, Severity.WARNING),
    ):
        for name in names:
            if _has_value(invoice, name):
                continue
            reason = invoice.field(name).reason
            detail = f" {reason}" if reason else ""
            message = f"{_FIELD_LABELS[name]} is missing.{detail}"
            issues.append(ValidationIssue(code, severity, message, field=name))
    return issues


def _check_amount_sanity(invoice: InvoiceData) -> list[ValidationIssue]:
    issues = []
    for name in ("subtotal", "tax", "total"):
        value: Decimal | None = invoice.field(name).value
        if value is not None and value < 0:
            issues.append(
                ValidationIssue(
                    ValidationCode.NEGATIVE_AMOUNT,
                    Severity.WARNING,
                    f"{_FIELD_LABELS[name]} is negative ({format_amount(value)}). "
                    "This is expected only on a credit note.",
                    field=name,
                    actual_value=format_amount(value),
                )
            )

    total = invoice.total.value
    if total is not None and total == 0:
        issues.append(
            ValidationIssue(
                ValidationCode.ZERO_TOTAL,
                Severity.WARNING,
                "Total is zero.",
                field="total",
                actual_value=format_amount(total),
            )
        )

    subtotal, tax = invoice.subtotal.value, invoice.tax.value
    # Magnitudes, because on a credit note -20 tax on a -100 subtotal is normal.
    if subtotal is not None and tax is not None and abs(tax) > abs(subtotal):
        issues.append(
            ValidationIssue(
                ValidationCode.TAX_EXCEEDS_SUBTOTAL,
                Severity.WARNING,
                f"Tax ({format_amount(tax)}) is greater than the subtotal "
                f"({format_amount(subtotal)}).",
                field="tax",
                actual_value=format_amount(tax),
            )
        )
    return issues


def _check_total(invoice: InvoiceData) -> list[ValidationIssue]:
    subtotal, tax, total = invoice.subtotal.value, invoice.tax.value, invoice.total.value
    if subtotal is None or total is None:
        return []
    if tax is None:
        if amounts_match(subtotal, total):
            return []
        return [
            ValidationIssue(
                ValidationCode.TOTAL_UNVERIFIED,
                Severity.WARNING,
                f"Subtotal ({format_amount(subtotal)}) and total ({format_amount(total)}) differ "
                "and the tax is unknown, so the total could not be fully checked.",
                field="total",
                actual_value=format_amount(total),
            )
        ]
    expected = subtotal + tax
    if amounts_match(expected, total):
        return []
    return [
        ValidationIssue(
            ValidationCode.TOTAL_MISMATCH,
            Severity.ERROR,
            f"Subtotal ({format_amount(subtotal)}) plus tax ({format_amount(tax)}) is "
            f"{format_amount(expected)}, but the total is {format_amount(total)}.",
            field="total",
            expected_value=format_amount(expected),
            actual_value=format_amount(total),
        )
    ]


def _check_line_items_sum(invoice: InvoiceData) -> list[ValidationIssue]:
    if not invoice.line_items:
        return []
    amounts = [item.amount for item in invoice.line_items if item.amount is not None]
    if len(amounts) < len(invoice.line_items):
        unknown = [str(item.position) for item in invoice.line_items if item.amount is None]
        return [
            ValidationIssue(
                ValidationCode.LINE_ITEMS_INCOMPLETE,
                Severity.WARNING,
                f"Line items at position {', '.join(unknown)} have no amount, so the line items "
                "could not be added up.",
                field="line_items",
            )
        ]

    line_sum = sum(amounts, Decimal(0))
    subtotal, tax, total = invoice.subtotal.value, invoice.tax.value, invoice.total.value
    if subtotal is not None:
        if amounts_match(line_sum, subtotal):
            return []
        return [
            ValidationIssue(
                ValidationCode.LINE_ITEMS_SUBTOTAL_MISMATCH,
                Severity.ERROR,
                f"Line items add up to {format_amount(line_sum)}, but the subtotal is "
                f"{format_amount(subtotal)}.",
                field="subtotal",
                expected_value=format_amount(line_sum),
                actual_value=format_amount(subtotal),
            )
        ]

    # No subtotal and no known tax: lines should reach the total, but unknown tax could explain a
    # gap, so this is a WARNING under its own code. Known nonzero tax is not guessed around.
    if total is None or (tax is not None and tax != 0) or amounts_match(line_sum, total):
        return []
    return [
        ValidationIssue(
            ValidationCode.LINE_ITEMS_TOTAL_MISMATCH,
            Severity.WARNING,
            f"Line items add up to {format_amount(line_sum)}, but the total is "
            f"{format_amount(total)}. There is no subtotal to compare with, and tax left off the "
            "invoice could explain the difference.",
            field="total",
            expected_value=format_amount(line_sum),
            actual_value=format_amount(total),
        )
    ]


def _check_line_item(item: LineItemData) -> list[ValidationIssue]:
    issues = []
    prefix = f"line_items[{item.position}]"
    quantity, unit_price, amount = item.quantity, item.unit_price, item.amount

    for name, value in (("quantity", quantity), ("unit_price", unit_price)):
        if value is not None and value < 0:
            label = name.replace("_", " ")
            issues.append(
                ValidationIssue(
                    ValidationCode.LINE_ITEM_NEGATIVE_VALUE,
                    Severity.WARNING,
                    f"Line item {item.position} has a negative {label} ({format_amount(value)}).",
                    field=f"{prefix}.{name}",
                    actual_value=format_amount(value),
                )
            )

    if quantity is not None and quantity == 0 and amount is not None and amount != 0:
        # Quantity times price cannot match here either; one issue is enough for one problem.
        issues.append(
            ValidationIssue(
                ValidationCode.LINE_ITEM_ZERO_QUANTITY,
                Severity.WARNING,
                f"Line item {item.position} has a zero quantity but an amount of "
                f"{format_amount(amount)}.",
                field=f"{prefix}.quantity",
                actual_value=format_amount(quantity),
            )
        )
    elif quantity is not None and unit_price is not None and amount is not None:
        product = quantity * unit_price
        allowed = TOLERANCE + abs(quantity) * UNIT_PRICE_ROUNDING
        if abs(product - amount) > allowed:
            issues.append(
                ValidationIssue(
                    ValidationCode.LINE_ITEM_AMOUNT_MISMATCH,
                    Severity.WARNING,
                    f"Line item {item.position}: quantity ({format_amount(quantity)}) times unit "
                    f"price ({format_amount(unit_price)}) is {format_amount(product)}, but the "
                    f"amount is {format_amount(amount)}. Allowed difference: "
                    f"{format_amount(allowed)} (0.01 plus rounding of a four-decimal unit price).",
                    field=f"{prefix}.amount",
                    expected_value=format_amount(product),
                    actual_value=format_amount(amount),
                )
            )
    return issues


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February, in a year that has none.
        return day.replace(year=day.year - years, day=28)


def _check_date(invoice: InvoiceData, today: date) -> list[ValidationIssue]:
    invoice_date = invoice.invoice_date.value
    if invoice_date is None:
        return []
    if invoice_date > today + FUTURE_DATE_GRACE:
        return [
            ValidationIssue(
                ValidationCode.DATE_IN_FUTURE,
                Severity.WARNING,
                f"Invoice date {invoice_date.isoformat()} is in the future "
                f"(today is {today.isoformat()}).",
                field="invoice_date",
                actual_value=invoice_date.isoformat(),
            )
        ]
    if invoice_date < _years_before(today, MAX_INVOICE_AGE_YEARS):
        return [
            ValidationIssue(
                ValidationCode.DATE_TOO_OLD,
                Severity.WARNING,
                f"Invoice date {invoice_date.isoformat()} is more than "
                f"{MAX_INVOICE_AGE_YEARS} years old.",
                field="invoice_date",
                actual_value=invoice_date.isoformat(),
            )
        ]
    return []


def _check_currency(invoice: InvoiceData) -> list[ValidationIssue]:
    currency = invoice.currency.value
    if not _has_value(invoice, "currency") or currency is None:
        return []
    if currency in KNOWN_CURRENCIES:
        return []
    return [
        ValidationIssue(
            ValidationCode.INVALID_CURRENCY,
            Severity.WARNING,
            f'Currency "{currency}" is not a recognised three-letter currency code.',
            field="currency",
            actual_value=currency,
        )
    ]


def _check_confidence(invoice: InvoiceData) -> list[ValidationIssue]:
    issues = []
    for name in INVOICE_FIELDS:
        extracted = invoice.field(name)
        if not _has_value(invoice, name) or extracted.confidence >= LOW_CONFIDENCE_THRESHOLD:
            continue
        detail = f" {extracted.reason}" if extracted.reason else ""
        issues.append(
            ValidationIssue(
                ValidationCode.LOW_CONFIDENCE,
                Severity.WARNING,
                f"{_FIELD_LABELS[name]} has low confidence ({extracted.confidence:.2f}).{detail}",
                field=name,
                actual_value=str(extracted.value),
            )
        )
    return issues


def validate_invoice(invoice: InvoiceData, *, today: date | None = None) -> list[ValidationIssue]:
    """Every problem found in one invoice. Reads the invoice only, and unknown is never zero."""
    issues = [
        *_check_missing_fields(invoice),
        *_check_amount_sanity(invoice),
        *_check_total(invoice),
        *_check_line_items_sum(invoice),
    ]
    for item in invoice.line_items:
        issues.extend(_check_line_item(item))
    issues.extend(_check_date(invoice, today or date.today()))
    issues.extend(_check_currency(invoice))
    issues.extend(_check_confidence(invoice))
    issues.extend(
        ValidationIssue(ValidationCode.EXTRACTION_WARNING, Severity.WARNING, warning)
        for warning in invoice.warnings
    )
    if not invoice.line_items:
        issues.append(
            ValidationIssue(
                ValidationCode.NO_LINE_ITEMS,
                Severity.INFO,
                "No line items were found.",
                field="line_items",
            )
        )
    return issues
