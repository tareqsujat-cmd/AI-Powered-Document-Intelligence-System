from datetime import date
from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from app.db.enums import ExtractionMethod

T = TypeVar("T")

REQUIRED_FIELDS = ("invoice_number", "invoice_date", "supplier_name", "total")
INVOICE_FIELDS = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "customer_name",
    "subtotal",
    "tax",
    "total",
    "currency",
)


class ExtractedField(BaseModel, Generic[T]):
    """One value and the evidence for it. An unknown value is None with a reason, never a guess."""

    value: T | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str | None = None
    source_snippet: str | None = None
    source_page: int | None = None


def _unknown() -> ExtractedField[Any]:
    return ExtractedField(reason="Not found in the document.")


class LineItemData(BaseModel):
    position: int = Field(ge=1)
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_snippet: str | None = None
    source_page: int | None = None


class InvoiceData(BaseModel):
    """One invoice found in a document, independent of how it was extracted or stored."""

    invoice_index: int = 0
    page_start: int | None = None
    page_end: int | None = None

    invoice_number: ExtractedField[str] = Field(default_factory=_unknown)
    invoice_date: ExtractedField[date] = Field(default_factory=_unknown)
    supplier_name: ExtractedField[str] = Field(default_factory=_unknown)
    customer_name: ExtractedField[str] = Field(default_factory=_unknown)
    subtotal: ExtractedField[Decimal] = Field(default_factory=_unknown)
    tax: ExtractedField[Decimal] = Field(default_factory=_unknown)
    total: ExtractedField[Decimal] = Field(default_factory=_unknown)
    currency: ExtractedField[str] = Field(default_factory=_unknown)
    line_items: list[LineItemData] = Field(default_factory=list)

    method: ExtractionMethod = ExtractionMethod.LLM
    llm_model: str | None = None
    prompt_version: str | None = None
    raw_response: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)

    def field(self, name: str) -> ExtractedField[Any]:
        extracted: ExtractedField[Any] = getattr(self, name)
        return extracted

    @property
    def overall_confidence(self) -> float | None:
        """Mean confidence of the fields that were found, or None if none were."""
        found = [self.field(name).confidence for name in INVOICE_FIELDS if self.has_value(name)]
        return sum(found) / len(found) if found else None

    def has_value(self, name: str) -> bool:
        return self.field(name).value is not None
