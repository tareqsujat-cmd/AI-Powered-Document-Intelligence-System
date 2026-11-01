from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import Document, ExtractionResult, LineItem
from app.extraction.normalize import duplicate_key, normalize_key
from app.extraction.schemas import INVOICE_FIELDS, ExtractedField, InvoiceData, LineItemData

_EVIDENCE = ("confidence", "reason", "source_snippet", "source_page")


_COLUMN_LIMITS = {
    "invoice_number": 128,
    "supplier_name": 512,
    "customer_name": 512,
    "currency": 3,
}


def _fit_columns(invoice: InvoiceData) -> InvoiceData:
    """A value too long for its column is not a real invoice value, and must not abort the save."""
    too_long = {
        name: ExtractedField[Any](
            reason=f"The value was over {limit} characters, so it was dropped."
        )
        for name, limit in _COLUMN_LIMITS.items()
        if len(str(invoice.field(name).value or "")) > limit
    }
    return invoice.model_copy(update=too_long)


def _field_metadata(invoice: InvoiceData) -> dict[str, Any]:
    return {
        name: {key: getattr(invoice.field(name), key) for key in _EVIDENCE}
        for name in INVOICE_FIELDS
    }


def save_invoice(session: Session, document: Document, invoice: InvoiceData) -> ExtractionResult:
    """Store an invoice as the current version, keeping earlier versions. Does not commit."""
    invoice = _fit_columns(invoice)
    previous_version = session.scalar(
        select(func.max(ExtractionResult.version)).where(
            ExtractionResult.document_id == document.id,
            ExtractionResult.invoice_index == invoice.invoice_index,
        )
    )
    session.execute(
        update(ExtractionResult)
        .where(
            ExtractionResult.document_id == document.id,
            ExtractionResult.invoice_index == invoice.invoice_index,
            ExtractionResult.is_current,
        )
        .values(is_current=False)
    )
    result = ExtractionResult(
        document_id=document.id,
        invoice_index=invoice.invoice_index,
        version=(previous_version or 0) + 1,
        is_current=True,
        page_start=invoice.page_start,
        page_end=invoice.page_end,
        invoice_number=invoice.invoice_number.value,
        invoice_key=normalize_key(invoice.invoice_number.value) or None,
        invoice_date=invoice.invoice_date.value,
        supplier_name=invoice.supplier_name.value,
        customer_name=invoice.customer_name.value,
        subtotal=invoice.subtotal.value,
        tax=invoice.tax.value,
        total=invoice.total.value,
        currency=invoice.currency.value,
        field_metadata=_field_metadata(invoice),
        overall_confidence=invoice.overall_confidence,
        duplicate_key=duplicate_key(
            invoice.supplier_name.value,
            invoice.invoice_number.value,
            invoice.invoice_date.value,
            invoice.total.value,
        ),
        method=invoice.method,
        llm_model=invoice.llm_model,
        prompt_version=invoice.prompt_version,
        raw_response=invoice.raw_response,
        warnings=invoice.warnings,
        line_items=[
            LineItem(
                position=item.position,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                amount=item.amount,
                field_metadata={
                    "confidence": item.confidence,
                    "source_snippet": item.source_snippet,
                    "source_page": item.source_page,
                },
            )
            for item in invoice.line_items
        ],
    )
    session.add(result)
    session.flush()
    return result


def _field_from_result(result: ExtractionResult, name: str) -> ExtractedField[Any]:
    evidence = result.field_metadata.get(name, {})
    return ExtractedField[Any](
        value=getattr(result, name),
        confidence=evidence.get("confidence", 0.0),
        reason=evidence.get("reason"),
        source_snippet=evidence.get("source_snippet"),
        source_page=evidence.get("source_page"),
    )


def invoice_from_result(result: ExtractionResult) -> InvoiceData:
    """Rebuild an invoice from storage, for re-validating it or comparing it to another."""
    return InvoiceData(
        invoice_index=result.invoice_index,
        page_start=result.page_start,
        page_end=result.page_end,
        method=result.method,
        llm_model=result.llm_model,
        prompt_version=result.prompt_version,
        warnings=list(result.warnings),
        line_items=[
            LineItemData(
                position=item.position,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                amount=item.amount,
                confidence=item.field_metadata.get("confidence", 0.0),
                source_snippet=item.field_metadata.get("source_snippet"),
                source_page=item.field_metadata.get("source_page"),
            )
            for item in result.line_items
        ],
        **{name: _field_from_result(result, name) for name in INVOICE_FIELDS},
    )
