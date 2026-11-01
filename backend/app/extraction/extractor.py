import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from app.core.config import Settings
from app.db.enums import ExtractionMethod, PageStatus
from app.extraction.errors import LLMInvalidResponseError, LLMNotConfiguredError
from app.extraction.guards import (
    SourcePage,
    guard_amount,
    guard_currency,
    guard_date,
    guard_name,
    guard_text_id,
)
from app.extraction.llm_client import LLMClient
from app.extraction.normalize import normalize_key
from app.extraction.prompts import Prompt, load_prompt
from app.extraction.schemas import ExtractedField, InvoiceData, LineItemData
from app.extraction.segmentation import Segment, chunk_segment, split_into_invoices
from app.extraction.spreadsheet_mapper import map_sheet
from app.processing.results import PageResult
from app.processing.text import CONTROL_CHARS

logger = logging.getLogger(__name__)

PROMPT_NAME = "invoice_extraction"
_INVALID_RESPONSE_ATTEMPTS = 2
_PERCENT_SCALE = 100.0

_NULLABLE_STRING = {"type": ["string", "null"]}
_FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "value": _NULLABLE_STRING,
        "confidence": {"type": "number"},
        "reason": _NULLABLE_STRING,
        "source_snippet": _NULLABLE_STRING,
    },
    "required": ["value", "confidence", "reason", "source_snippet"],
}
_SCALAR_FIELDS = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "customer_name",
    "subtotal",
    "tax",
    "total",
    "currency",
)
INVOICE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **dict.fromkeys(_SCALAR_FIELDS, _FIELD_SCHEMA),
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": _NULLABLE_STRING,
                    "quantity": _NULLABLE_STRING,
                    "unit_price": _NULLABLE_STRING,
                    "amount": _NULLABLE_STRING,
                    "confidence": {"type": "number"},
                    "source_snippet": _NULLABLE_STRING,
                },
                "required": ["description", "quantity", "unit_price", "amount", "confidence"],
            },
        },
    },
    "required": [*_SCALAR_FIELDS, "line_items"],
}


def _without_control_chars(value: object) -> object:
    if isinstance(value, str):
        return CONTROL_CHARS.sub("", value)
    if isinstance(value, dict):
        return {key: _without_control_chars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_control_chars(item) for item in value]
    return value


def _to_text(value: object) -> object:
    # Models sometimes answer 12.5 where the schema asks for "12.5".
    return str(value) if isinstance(value, int | float) and not isinstance(value, bool) else value


class _RawModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    @field_validator("confidence", mode="before", check_fields=False)
    @classmethod
    def _clamp_confidence(cls, value: object) -> float:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        if number > 1.0:
            number /= _PERCENT_SCALE
        return max(0.0, min(number, 1.0))


class _RawField(_RawModel):
    value: str | None = None
    confidence: float = 0.0
    reason: str | None = None
    source_snippet: str | None = None

    _coerce_value = field_validator("value", mode="before")(_to_text)


class _RawLineItem(_RawModel):
    description: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None
    confidence: float = 0.0
    source_snippet: str | None = None

    _coerce_numbers = field_validator("quantity", "unit_price", "amount", mode="before")(_to_text)


class _RawInvoice(_RawModel):
    invoice_number: _RawField = _RawField()
    invoice_date: _RawField = _RawField()
    supplier_name: _RawField = _RawField()
    customer_name: _RawField = _RawField()
    subtotal: _RawField = _RawField()
    tax: _RawField = _RawField()
    total: _RawField = _RawField()
    currency: _RawField = _RawField()
    line_items: list[_RawLineItem] = []

    @model_validator(mode="before")
    @classmethod
    def _strip_control_chars(cls, value: object) -> object:
        # PostgreSQL rejects NUL in both TEXT and JSONB, and a model's answer can contain anything.
        return _without_control_chars(value)


_GUARDS = {
    "invoice_number": guard_text_id,
    "invoice_date": guard_date,
    "supplier_name": guard_name,
    "customer_name": guard_name,
    "subtotal": guard_amount,
    "tax": guard_amount,
    "total": guard_amount,
    "currency": guard_currency,
}
# Identifying details are usually at the top of an invoice, and money totals at the end. So for
# these fields, a value from a later part of a long invoice wins.
_LAST_VALUE_WINS = frozenset({"subtotal", "tax", "total"})


def _guard_line_item(
    raw: _RawLineItem, position: int, pages: Sequence[SourcePage], warnings: list[str]
) -> LineItemData | None:
    """Keep a row only if the document backs it up, and drop any cell it does not."""
    description = guard_name(raw.description, raw.confidence, None, raw.source_snippet, pages)
    cells = {
        name: guard_amount(getattr(raw, name), raw.confidence, None, raw.source_snippet, pages)
        for name in ("quantity", "unit_price", "amount")
    }
    if description.value is None and all(cell.value is None for cell in cells.values()):
        warnings.append(f"Line item {position} was discarded: it is not in the document.")
        return None

    claimed = sum(
        getattr(raw, name) is not None
        for name in ("description", "quantity", "unit_price", "amount")
    )
    verified = sum(cell.value is not None for cell in (description, *cells.values()))
    if verified < claimed:
        warnings.append(
            f"Line item {position}: {claimed - verified} value(s) not in the document were dropped."
        )
    found = [item for item in (description, *cells.values()) if item.value is not None]
    return LineItemData(
        position=position,
        description=description.value,
        quantity=cells["quantity"].value,
        unit_price=cells["unit_price"].value,
        amount=cells["amount"].value,
        confidence=min(item.confidence for item in found),
        source_snippet=raw.source_snippet or found[0].source_snippet,
        source_page=found[0].source_page,
    )


def _choose_field(name: str, candidates: Sequence[ExtractedField[Any]]) -> ExtractedField[Any]:
    found = [candidate for candidate in candidates if candidate.value is not None]
    if not found:
        reasons = [candidate.reason for candidate in candidates if candidate.reason]
        # A discarded value is more important to explain than "not in this part".
        for candidate_reason in reasons:
            if "discarded" in candidate_reason:
                return ExtractedField(value=None, confidence=0.0, reason=candidate_reason)
        return ExtractedField(
            value=None, confidence=0.0, reason=reasons[0] if reasons else "Not in the document."
        )
    if name in _LAST_VALUE_WINS:
        return found[-1]
    return max(found, key=lambda candidate: candidate.confidence)


def _conflict_warning(name: str, candidates: Sequence[ExtractedField[Any]]) -> str | None:
    values = {
        normalize_key(str(candidate.value))
        for candidate in candidates
        if candidate.value is not None
    }
    if len(values) < 2 or name in _LAST_VALUE_WINS:
        return None
    return f"Different parts of this invoice disagree on {name}; a person should check it."


def _merge_parts(
    parts: Sequence[_RawInvoice], segment: Segment, invoice_index: int, prompt: Prompt, model: str
) -> InvoiceData:
    pages = [SourcePage(page.page_number, page.text, page.ocr_confidence) for page in segment.pages]
    warnings: list[str] = []
    guarded: dict[str, ExtractedField[Any]] = {}
    for name in _SCALAR_FIELDS:
        candidates = [
            _GUARDS[name](raw.value, raw.confidence, raw.reason, raw.source_snippet, pages)
            for raw in (getattr(part, name) for part in parts)
        ]
        guarded[name] = _choose_field(name, candidates)
        conflict = _conflict_warning(name, candidates)
        if conflict:
            warnings.append(conflict)

    items: list[LineItemData] = []
    for raw_item in (item for part in parts for item in part.line_items):
        kept = _guard_line_item(raw_item, len(items) + 1, pages, warnings)
        if kept is not None:
            items.append(kept)

    return InvoiceData(
        invoice_index=invoice_index,
        page_start=segment.first_page,
        page_end=segment.last_page,
        line_items=items,
        method=ExtractionMethod.LLM,
        llm_model=model,
        prompt_version=prompt.version,
        raw_response={"parts": [part.model_dump(mode="json") for part in parts]},
        warnings=warnings,
        **guarded,
    )


@dataclass(slots=True)
class ExtractionRun:
    invoices: list[InvoiceData] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _call_model(client: LLMClient, prompt: Prompt, text: str, run: ExtractionRun) -> _RawInvoice:
    rendered = prompt.render(text)
    problem: Exception | None = None
    for _ in range(_INVALID_RESPONSE_ATTEMPTS):
        run.llm_calls += 1
        try:
            response = client.generate_json(rendered, INVOICE_RESPONSE_SCHEMA)
            run.input_tokens += response.input_tokens or 0
            run.output_tokens += response.output_tokens or 0
            return _RawInvoice.model_validate(response.data)
        except (LLMInvalidResponseError, ValidationError) as exc:
            logger.warning("llm returned an unusable answer", extra={"error": type(exc).__name__})
            problem = exc
    raise LLMInvalidResponseError("The model did not return a usable answer.") from problem


def _unprocessed(segment: Segment, invoice_index: int, reason: str) -> InvoiceData:
    return InvoiceData(
        invoice_index=invoice_index,
        page_start=segment.first_page,
        page_end=segment.last_page,
        warnings=[reason],
    )


def extract_invoices(
    pages: Iterable[PageResult], client: LLMClient | None, settings: Settings
) -> ExtractionRun:
    """Find the invoices in a document's pages. Spreadsheets try exact rules before the LLM.

    Raises ProcessingError if the LLM cannot be reached. An invoice with no usable values is still
    returned, with nulls and reasons, so that a person can review it.
    """
    run = ExtractionRun()
    text_segments: list[Segment] = []
    plain_pages: list[PageResult] = []
    ordered: list[tuple[int, list[InvoiceData] | Segment]] = []

    for page in pages:
        if page.status is not PageStatus.OK or not page.text:
            continue
        if page.table is None:
            plain_pages.append(page)
            continue
        mapped = map_sheet(page)
        ordered.append((page.page_number, mapped if mapped else Segment([page])))

    text_segments = split_into_invoices(plain_pages)
    ordered.extend((segment.first_page, segment) for segment in text_segments)
    ordered.sort(key=lambda entry: entry[0])

    needs_llm = any(isinstance(item, Segment) for _, item in ordered)
    if needs_llm and client is None:
        raise LLMNotConfiguredError("LLM_API_KEY is not set, and this document needs the LLM.")

    prompt = load_prompt(PROMPT_NAME, settings.prompts_dir) if needs_llm else None
    for _, item in ordered:
        if not isinstance(item, Segment):
            for invoice in item:
                invoice.invoice_index = len(run.invoices)
                run.invoices.append(invoice)
            continue

        assert client is not None and prompt is not None  # noqa: S101 (checked above)
        index = len(run.invoices)
        chunks = chunk_segment(item, settings.llm_max_chunk_chars)
        if run.llm_calls + len(chunks) > settings.llm_max_calls_per_document:
            run.invoices.append(
                _unprocessed(
                    item,
                    index,
                    "This invoice was not extracted, because the document reached its limit of "
                    f"{settings.llm_max_calls_per_document} LLM calls.",
                )
            )
            continue
        parts = [_call_model(client, prompt, chunk, run) for chunk in chunks]
        run.invoices.append(_merge_parts(parts, item, index, prompt, client.model))
    return run
