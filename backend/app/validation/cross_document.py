import logging
import uuid
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, or_, select
from sqlalchemy.orm import Session, defer, selectinload

from app.db.enums import FileType, Severity
from app.db.models import ExtractionResult
from app.extraction.normalize import duplicate_key, normalize_key, normalize_party_name
from app.extraction.persistence import invoice_from_result
from app.extraction.schemas import InvoiceData
from app.validation.issues import ValidationCode, ValidationIssue
from app.validation.rules import amounts_match, format_amount

logger = logging.getLogger(__name__)

# A number such as 1001 can be shared by thousands of invoices. Only the latest few are compared.
MAX_RELATED_RESULTS = 50

_SPREADSHEET_TYPES = frozenset({FileType.XLSX, FileType.XLS, FileType.CSV})


def _is_pdf_and_spreadsheet(kinds: tuple[FileType, FileType]) -> bool:
    first, second = kinds
    return (first == FileType.PDF and second in _SPREADSHEET_TYPES) or (
        second == FileType.PDF and first in _SPREADSHEET_TYPES
    )


def _mismatch_code(kinds: tuple[FileType, FileType]) -> ValidationCode:
    first, second = kinds
    if first == FileType.PDF and second == FileType.PDF:
        return ValidationCode.PDF_PDF_MISMATCH
    if _is_pdf_and_spreadsheet(kinds):
        return ValidationCode.PDF_EXCEL_MISMATCH
    return ValidationCode.CROSS_DOCUMENT_MISMATCH


def _parties_differ(first: str | None, second: str | None) -> bool:
    """A name that normalizes to nothing, such as "Ltd", says nothing about who the party is."""
    first_key, second_key = normalize_party_name(first), normalize_party_name(second)
    return bool(first_key and second_key and first_key != second_key)


def compare_invoices(
    invoice: InvoiceData,
    other: InvoiceData,
    *,
    kinds: tuple[FileType, FileType],
    related_document_id: uuid.UUID | None,
) -> list[ValidationIssue]:
    """Differences between two copies of one invoice. A value only one side knows is no conflict."""
    code = _mismatch_code(kinds)
    here, there = kinds[0].value.upper(), kinds[1].value.upper()
    issues: list[ValidationIssue] = []

    def report(field: str, label: str, actual: str, expected: str) -> None:
        issues.append(
            ValidationIssue(
                code,
                Severity.ERROR,
                f"{label} is {actual} in this {here} but {expected} in the related {there}.",
                field=field,
                expected_value=expected,
                actual_value=actual,
                related_document_id=related_document_id,
            )
        )

    for name, label in (("supplier_name", "Supplier"), ("customer_name", "Customer")):
        actual_name: str | None = invoice.field(name).value
        expected_name: str | None = other.field(name).value
        if actual_name and expected_name and _parties_differ(actual_name, expected_name):
            report(name, label, actual_name, expected_name)

    actual_date, expected_date = invoice.invoice_date.value, other.invoice_date.value
    if actual_date is not None and expected_date is not None and actual_date != expected_date:
        report("invoice_date", "Invoice date", actual_date.isoformat(), expected_date.isoformat())

    for name, label in (("subtotal", "Subtotal"), ("tax", "Tax"), ("total", "Total")):
        actual_amount: Decimal | None = invoice.field(name).value
        expected_amount: Decimal | None = other.field(name).value
        if (
            actual_amount is not None
            and expected_amount is not None
            and not amounts_match(actual_amount, expected_amount)
        ):
            report(name, label, format_amount(actual_amount), format_amount(expected_amount))

    actual_currency, expected_currency = invoice.currency.value, other.currency.value
    if actual_currency and expected_currency and actual_currency != expected_currency:
        report("currency", "Currency", actual_currency, expected_currency)

    lines = sorted(invoice.line_items, key=lambda item: item.position)
    other_lines = sorted(other.line_items, key=lambda item: item.position)
    if not lines or not other_lines:
        return issues

    if len(lines) != len(other_lines):
        report("line_items", "Number of line items", str(len(lines)), str(len(other_lines)))

    amounts = [item.amount for item in lines if item.amount is not None]
    other_amounts = [item.amount for item in other_lines if item.amount is not None]
    if len(amounts) == len(lines) and len(other_amounts) == len(other_lines):
        line_sum, other_sum = sum(amounts, Decimal(0)), sum(other_amounts, Decimal(0))
        if not amounts_match(line_sum, other_sum):
            report(
                "line_items.sum",
                "Sum of line items",
                format_amount(line_sum),
                format_amount(other_sum),
            )

    if len(lines) == len(other_lines):
        for line, other_line in zip(lines, other_lines, strict=True):
            if (
                line.amount is not None
                and other_line.amount is not None
                and not amounts_match(line.amount, other_line.amount)
            ):
                report(
                    f"line_items[{line.position}].amount",
                    f"Amount of line item {line.position}",
                    format_amount(line.amount),
                    format_amount(other_line.amount),
                )
    return issues


def _most_recent(*conditions: ColumnElement[bool]) -> Select[ExtractionResult]:
    return (
        select(ExtractionResult)
        .options(defer(ExtractionResult.raw_response))
        .where(ExtractionResult.is_current, *conditions)
        .order_by(ExtractionResult.created_at.desc(), ExtractionResult.id.desc())
        .limit(MAX_RELATED_RESULTS)
    )


def _warn_if_truncated(results: list[ExtractionResult], what: str) -> None:
    if len(results) == MAX_RELATED_RESULTS:
        logger.warning("Checked only the latest %d %s", MAX_RELATED_RESULTS, what)


def _verified_issue(result: ExtractionResult) -> ValidationIssue:
    return ValidationIssue(
        ValidationCode.CROSS_DOCUMENT_VERIFIED,
        Severity.INFO,
        f"The {result.document.file_type.value.upper()} copy of this invoice agrees with this one "
        f"(number {result.invoice_number}).",
        field="invoice_number",
        actual_value=result.invoice_number,
        related_document_id=result.document_id,
    )


def _duplicate_issue(result: ExtractionResult, document_id: uuid.UUID) -> ValidationIssue:
    where = (
        "another invoice in this document"
        if result.document_id == document_id
        else "an invoice in another document"
    )
    return ValidationIssue(
        ValidationCode.DUPLICATE_INVOICE,
        Severity.ERROR,
        f"This invoice has the same supplier, number, date and total as {where} "
        f"(number {result.invoice_number}).",
        field="invoice_number",
        actual_value=result.invoice_number,
        related_document_id=result.document_id,
    )


def check_against_stored(
    session: Session,
    document_id: uuid.UUID,
    file_type: FileType,
    invoice: InvoiceData,
    *,
    exclude_result_id: uuid.UUID | None = None,
) -> list[ValidationIssue]:
    """Duplicates of this invoice and conflicts with other copies of it among current results."""
    number = invoice.invoice_number.value
    supplier = invoice.supplier_name.value
    invoice_duplicate_key = duplicate_key(
        supplier, number, invoice.invoice_date.value, invoice.total.value
    )
    invoice_key = normalize_key(number)
    issues: list[ValidationIssue] = []

    if invoice_duplicate_key:
        # The same document and index is this invoice's own row, not a duplicate of it.
        conditions = [
            ExtractionResult.duplicate_key == invoice_duplicate_key,
            or_(
                ExtractionResult.document_id != document_id,
                ExtractionResult.invoice_index != invoice.invoice_index,
            ),
        ]
        if exclude_result_id is not None:
            conditions.append(ExtractionResult.id != exclude_result_id)
        duplicates = list(
            session.scalars(
                _most_recent(*conditions).options(
                    selectinload(ExtractionResult.line_items),
                    selectinload(ExtractionResult.document),
                )
            )
        )
        _warn_if_truncated(duplicates, "duplicates")
        for result in duplicates:
            kinds = (file_type, result.document.file_type)
            if not _is_pdf_and_spreadsheet(kinds):
                issues.append(_duplicate_issue(result, document_id))
                continue
            # A PDF and a spreadsheet of one invoice are a cross-check, not the invoice entered
            # twice. They can still disagree on anything the duplicate key does not cover.
            differences = compare_invoices(
                invoice,
                invoice_from_result(result),
                kinds=kinds,
                related_document_id=result.document_id,
            )
            issues.extend(differences or [_verified_issue(result)])

    if not invoice_key:
        return issues

    conditions = [
        ExtractionResult.invoice_key == invoice_key,
        ExtractionResult.document_id != document_id,
    ]
    if invoice_duplicate_key:
        conditions.append(ExtractionResult.duplicate_key.is_distinct_from(invoice_duplicate_key))
    related = list(
        session.scalars(
            _most_recent(*conditions).options(
                selectinload(ExtractionResult.line_items),
                selectinload(ExtractionResult.document),
            )
        )
    )
    _warn_if_truncated(related, "results sharing an invoice number")

    for result in related:
        # Different companies reuse numbers such as 1001. That is no conflict.
        if _parties_differ(supplier, result.supplier_name):
            continue
        issues.extend(
            compare_invoices(
                invoice,
                invoice_from_result(result),
                kinds=(file_type, result.document.file_type),
                related_document_id=result.document_id,
            )
        )
    return issues
