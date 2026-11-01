"""Hallucination guards: a value only survives if it can be found in the document itself.

The model is asked not to invent anything, but this is where that is enforced. A value that cannot
be traced to the source becomes null with a reason, and a person reviews it.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from app.extraction.normalize import (
    amount_candidates,
    amounts_in_text,
    dates_in_text,
    normalize_key,
    ocr_tolerant_key,
)
from app.extraction.schemas import ExtractedField

# Word-level similarity for names read through OCR noise.
_NAME_SIMILARITY = 0.85
_INEXACT_MATCH_CONFIDENCE = 0.7
_AMBIGUOUS_DATE_CONFIDENCE = 0.6
_AMBIGUOUS_CURRENCY_CONFIDENCE = 0.6
_MAX_SNIPPET_CHARS = 300

NOT_IN_SOURCE = "The extracted value was not found in the document, so it was discarded."

_CODE_PATTERN = re.compile(r"\b[A-Z]{3}\b")
_UNAMBIGUOUS_SYMBOLS = {"€": "EUR", "£": "GBP", "₹": "INR", "৳": "BDT", "₩": "KRW", "₦": "NGN"}
# Each of these symbols is shared by several currencies.
_SHARED_SYMBOLS = {
    "$": {"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN"},
    "¥": {"JPY", "CNY"},
    "kr": {"SEK", "NOK", "DKK"},
}
_CURRENCY_NAMES = {
    "USD": ("us dollar", "u.s. dollar", "american dollar"),
    "EUR": ("euro",),
    "GBP": ("pound sterling", "british pound"),
    "INR": ("indian rupee", "rupees"),
    "BDT": ("taka",),
    "JPY": ("japanese yen", "yen"),
    "CAD": ("canadian dollar",),
    "AUD": ("australian dollar",),
}


@dataclass(frozen=True, slots=True)
class SourcePage:
    number: int
    text: str
    ocr_confidence: float | None = None


def _locate_line(pages: Sequence[SourcePage], needle: str) -> tuple[str | None, int | None]:
    """The first line containing `needle`, ignoring case, and its page."""
    wanted = needle.casefold()
    for page in pages:
        for line in page.text.splitlines():
            if wanted in line.casefold():
                return line.strip()[:_MAX_SNIPPET_CHARS], page.number
    return None, None


def _discard() -> ExtractedField[Any]:
    return ExtractedField(value=None, confidence=0.0, reason=NOT_IN_SOURCE)


def _cap_by_ocr(confidence: float, page_number: int | None, pages: Sequence[SourcePage]) -> float:
    """A value cannot be more reliable than the OCR of the page it was read from."""
    for page in pages:
        if page.number == page_number and page.ocr_confidence is not None:
            return min(confidence, page.ocr_confidence)
    return confidence


def _verified(
    value: object,
    confidence: float,
    reason: str | None,
    snippet: str | None,
    page: int | None,
    pages: Sequence[SourcePage],
) -> ExtractedField[Any]:
    return ExtractedField(
        value=value,
        confidence=_cap_by_ocr(confidence, page, pages),
        reason=reason,
        source_snippet=snippet,
        source_page=page,
    )


def _claimed_snippet_page(
    pages: Sequence[SourcePage], snippet: str | None
) -> tuple[str | None, int | None]:
    """Keep the model's snippet only if the document really contains it."""
    if not snippet:
        return None, None
    wanted = normalize_key(snippet)
    if not wanted:
        return None, None
    for page in pages:
        if wanted in normalize_key(page.text):
            return snippet.strip()[:_MAX_SNIPPET_CHARS], page.number
    return None, None


def guard_text_id(
    value: str | None,
    confidence: float,
    reason: str | None,
    snippet: str | None,
    pages: Sequence[SourcePage],
) -> ExtractedField[Any]:
    """An identifier such as an invoice number must appear, ignoring punctuation and spacing."""
    if not value or not value.strip():
        return ExtractedField(value=None, confidence=0.0, reason=reason or "Not in the document.")
    value = value.strip()
    wanted = normalize_key(value)
    if not wanted:
        return _discard()

    for page in pages:
        if wanted in normalize_key(page.text):
            found_snippet, found_page = _claimed_snippet_page(pages, snippet)
            if found_snippet is None or normalize_key(value) not in normalize_key(found_snippet):
                found_snippet, found_page = _locate_line([page], value)
            return _verified(
                value, confidence, reason, found_snippet, found_page or page.number, pages
            )

    # OCR can turn 0 into O. Keep the value, but do not trust it: a person should look.
    tolerant = ocr_tolerant_key(value)
    for page in pages:
        if tolerant in ocr_tolerant_key(page.text):
            note = "The value only matches the document after allowing for OCR character errors."
            return _verified(
                value,
                min(confidence, _INEXACT_MATCH_CONFIDENCE),
                reason or note,
                None,
                page.number,
                pages,
            )
    return _discard()


def _best_line_similarity(wanted: str, line: str) -> float:
    words = line.split()
    wanted_count = len(wanted.split())
    if not words or not wanted_count:
        return 0.0
    best = 0.0
    # Slide a window of about the name's length over the line.
    for size in {max(wanted_count - 1, 1), wanted_count, wanted_count + 1}:
        for start in range(max(len(words) - size + 1, 1)):
            window = " ".join(words[start : start + size])
            best = max(best, SequenceMatcher(None, wanted, window, autojunk=False).ratio())
    return best


def guard_name(
    value: str | None,
    confidence: float,
    reason: str | None,
    snippet: str | None,
    pages: Sequence[SourcePage],
) -> ExtractedField[Any]:
    """A company name must appear, tolerating the small errors OCR makes."""
    if not value or not value.strip():
        return ExtractedField(value=None, confidence=0.0, reason=reason or "Not in the document.")
    value = value.strip()
    wanted = value.casefold()

    found_line, found_page = _locate_line(pages, value)
    if found_line is not None:
        return _verified(value, confidence, reason, found_line, found_page, pages)

    best_score, best_line, best_page = 0.0, None, None
    for page in pages:
        for line in page.text.splitlines():
            score = _best_line_similarity(wanted, line.casefold())
            if score > best_score:
                best_score, best_line, best_page = score, line.strip(), page.number
    if best_score >= _NAME_SIMILARITY and best_line is not None:
        note = f"The name only approximately matches the document ({best_score:.0%} similar)."
        return _verified(
            value,
            min(confidence, _INEXACT_MATCH_CONFIDENCE),
            reason or note,
            best_line[:_MAX_SNIPPET_CHARS],
            best_page,
            pages,
        )
    return _discard()


def guard_amount(
    value: str | None,
    confidence: float,
    reason: str | None,
    snippet: str | None,
    pages: Sequence[SourcePage],
) -> ExtractedField[Any]:
    """A money value must be printed in the document. This is what catches computed totals."""
    if value is None or not str(value).strip():
        return ExtractedField(value=None, confidence=0.0, reason=reason or "Not in the document.")
    candidates = amount_candidates(str(value))
    if not candidates:
        return ExtractedField(
            value=None,
            confidence=0.0,
            reason=f"The extracted text {str(value)[:40]!r} is not a number.",
        )

    magnitudes = {abs(candidate) for candidate in candidates}
    for page in pages:
        if magnitudes & {abs(number) for number in amounts_in_text(page.text)}:
            found_snippet, found_page = _claimed_snippet_page(pages, snippet)
            if found_snippet is None or not magnitudes & {
                abs(number) for number in amounts_in_text(found_snippet)
            }:
                found_snippet, found_page = _line_with_amount([page], magnitudes)
            return _verified(
                candidates[0],
                confidence,
                reason,
                found_snippet,
                found_page or page.number,
                pages,
            )
    return _discard()


def _line_with_amount(
    pages: Sequence[SourcePage], magnitudes: set[Decimal]
) -> tuple[str | None, int | None]:
    for page in pages:
        for line in page.text.splitlines():
            if magnitudes & {abs(number) for number in amounts_in_text(line)}:
                return line.strip()[:_MAX_SNIPPET_CHARS], page.number
    return None, None


def guard_date(
    value: str | None,
    confidence: float,
    reason: str | None,
    snippet: str | None,
    pages: Sequence[SourcePage],
) -> ExtractedField[Any]:
    """A date must be printed. If its day and month could be swapped, a person must confirm it."""
    if not value or not str(value).strip():
        return ExtractedField(value=None, confidence=0.0, reason=reason or "Not in the document.")
    parsed = dates_in_text(str(value))
    claimed: date | None = parsed[0][0] if parsed and not parsed[0][1] else None
    if claimed is None:
        return ExtractedField(
            value=None,
            confidence=0.0,
            reason=f"The extracted text {str(value)[:40]!r} is not an unambiguous date.",
        )

    for page in pages:
        for line in page.text.splitlines():
            for found, ambiguous in dates_in_text(line):
                if found != claimed:
                    continue
                if ambiguous:
                    note = "The day and month in this date could be read either way."
                    return _verified(
                        claimed,
                        min(confidence, _AMBIGUOUS_DATE_CONFIDENCE),
                        note,
                        line.strip()[:_MAX_SNIPPET_CHARS],
                        page.number,
                        pages,
                    )
                return _verified(
                    claimed,
                    confidence,
                    reason,
                    line.strip()[:_MAX_SNIPPET_CHARS],
                    page.number,
                    pages,
                )
    return _discard()


def guard_currency(
    value: str | None,
    confidence: float,
    reason: str | None,
    snippet: str | None,
    pages: Sequence[SourcePage],
) -> ExtractedField[Any]:
    """A currency must be stated: as a code, a name, or a symbol. "$" alone is only a hint."""
    if not value or not value.strip():
        return ExtractedField(value=None, confidence=0.0, reason=reason or "Not in the document.")
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        return ExtractedField(
            value=None, confidence=0.0, reason=f"{value[:20]!r} is not a currency code."
        )

    for page in pages:
        if code in _CODE_PATTERN.findall(page.text):
            line, number = _locate_line([page], code)
            return _verified(code, confidence, reason, line, number, pages)
    for symbol, symbol_code in _UNAMBIGUOUS_SYMBOLS.items():
        if symbol_code == code:
            line, number = _locate_line(pages, symbol)
            if line is not None:
                return _verified(code, confidence, reason, line, number, pages)
    for name in _CURRENCY_NAMES.get(code, ()):
        line, number = _locate_line(pages, name)
        if line is not None:
            return _verified(code, confidence, reason, line, number, pages)
    for symbol, codes in _SHARED_SYMBOLS.items():
        if code in codes:
            line, number = _locate_line(pages, symbol)
            if line is not None:
                note = f"The document only shows {symbol!r}, which several currencies use."
                return _verified(
                    code, min(confidence, _AMBIGUOUS_CURRENCY_CONFIDENCE), note, line, number, pages
                )
    return _discard()
