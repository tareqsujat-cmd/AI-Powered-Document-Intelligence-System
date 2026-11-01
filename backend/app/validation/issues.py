import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.db.enums import Severity


class ValidationCode(StrEnum):
    MISSING_REQUIRED_FIELD = "missing_required_field"
    MISSING_FIELD = "missing_field"
    NEGATIVE_AMOUNT = "negative_amount"
    ZERO_TOTAL = "zero_total"
    TAX_EXCEEDS_SUBTOTAL = "tax_exceeds_subtotal"
    LINE_ITEM_NEGATIVE_VALUE = "line_item_negative_value"
    LINE_ITEM_ZERO_QUANTITY = "line_item_zero_quantity"
    TOTAL_MISMATCH = "total_mismatch"
    TOTAL_UNVERIFIED = "total_unverified"
    LINE_ITEMS_SUBTOTAL_MISMATCH = "line_items_subtotal_mismatch"
    LINE_ITEMS_TOTAL_MISMATCH = "line_items_total_mismatch"
    LINE_ITEMS_INCOMPLETE = "line_items_incomplete"
    LINE_ITEM_AMOUNT_MISMATCH = "line_item_amount_mismatch"
    DATE_IN_FUTURE = "date_in_future"
    DATE_TOO_OLD = "date_too_old"
    INVALID_CURRENCY = "invalid_currency"
    LOW_CONFIDENCE = "low_confidence"
    EXTRACTION_WARNING = "extraction_warning"
    NO_LINE_ITEMS = "no_line_items"
    DUPLICATE_INVOICE = "duplicate_invoice"
    PDF_PDF_MISMATCH = "pdf_pdf_mismatch"
    PDF_EXCEL_MISMATCH = "pdf_excel_mismatch"
    CROSS_DOCUMENT_MISMATCH = "cross_document_mismatch"
    CROSS_DOCUMENT_VERIFIED = "cross_document_verified"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One failed rule, shaped like a row of the validation_errors table."""

    code: ValidationCode
    severity: Severity
    message: str
    field: str | None = None
    expected_value: str | None = None
    actual_value: str | None = None
    related_document_id: uuid.UUID | None = None


def needs_review(issues: Iterable[ValidationIssue]) -> bool:
    return any(issue.severity in (Severity.ERROR, Severity.WARNING) for issue in issues)
