import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.extraction.normalize import ocr_tolerant_key
from app.processing.results import PageResult

_NUMBER_LABEL = re.compile(
    r"\b(?:invoice|inv|bill|tax\s+invoice)\s*"
    r"(?:no\.?|num(?:ber)?\.?|nbr\.?|#|id)?\s*[:#.\-]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9/\-_.]{2,30})",
    re.IGNORECASE,
)
# A column heading such as "INVOICE #" in a row of headings, with the values on the line below.
_COLUMN_LABEL = re.compile(
    r"\b(?:tax\s+)?(?:invoice|inv|bill)\s*(?:no\.?|num(?:ber)?\.?|nbr\.?|#|id)(?=[\s:]|$)",
    re.IGNORECASE,
)
_NUMBER_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/\-_.]{2,30}")
_DATE_SHAPE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")
_PAGE_ONE = re.compile(r"\bpage\s+1\s*(?:of|/)\s*\d+\b", re.IGNORECASE)
# Words that follow "invoice" in ordinary text and must not be taken for a number.
_NOT_A_NUMBER = frozenset(
    {
        "date",
        "number",
        "total",
        "amount",
        "address",
        "details",
        "summary",
        "terms",
        "due",
        "to",
        "from",
        "for",
        "the",
        "and",
        "was",
        "will",
        "payment",
    }
)


@dataclass(slots=True)
class Segment:
    """Consecutive pages believed to be one invoice."""

    pages: list[PageResult]

    @property
    def first_page(self) -> int:
        return self.pages[0].page_number

    @property
    def last_page(self) -> int:
        return self.pages[-1].page_number


def find_invoice_number(text: str) -> str | None:
    """A labeled invoice number on a page, used only to notice where one invoice ends."""
    # Headings first: otherwise the label pattern reaches past the line end, into another column.
    if (below := _number_below_heading(text)) is not None:
        return below
    # Resume just after each match's start, not its end: in "INVOICE\nInvoice Number: A-1", the
    # first match takes the second "Invoice" as its value, which must not hide the real label.
    position = 0
    while (match := _NUMBER_LABEL.search(text, position)) is not None:
        position = match.start() + 1
        candidate = match[1].rstrip(".-_/")
        if candidate.casefold() in _NOT_A_NUMBER:
            continue
        # A real invoice number nearly always contains a digit.
        if any(character.isdigit() for character in candidate):
            return candidate
    return None


def _number_below_heading(text: str) -> str | None:
    """The value under an "INVOICE #" heading, in layouts that print headings in one row and
    values in the next. Digital text keeps the columns as tabs; OCR text only has spaces."""
    lines = text.split("\n")
    for heading, values in zip(lines, lines[1:], strict=False):
        match = _COLUMN_LABEL.search(heading)
        # A row of headings holds no figures. A digit after the label means the value is beside
        # it, and the line below is something else.
        if match is None or any(c.isdigit() for c in heading[match.end() :]):
            continue
        if "\t" in heading and "\t" in values:
            column = heading[: match.start()].count("\t")
            cells = values.split("\t")
            candidate = cells[column].strip() if column < len(cells) else ""
            if _NUMBER_SHAPE.fullmatch(candidate) and any(c.isdigit() for c in candidate):
                return candidate
            continue
        # Without columns, take the first word shaped like an identifier: a digit, a letter or
        # separator, and not a date. A plain number is too likely to be a quantity or a year.
        for word in values.split():
            if (
                _NUMBER_SHAPE.fullmatch(word)
                and len(word) >= 4
                and any(c.isdigit() for c in word)
                and any(c.isalpha() or c in "-/" for c in word)
                and not _DATE_SHAPE.fullmatch(word)
            ):
                return word
    return None


def split_into_invoices(pages: Iterable[PageResult]) -> list[Segment]:
    """Group pages into invoices without using the LLM, so a long batch costs one call per invoice.

    A page starts a new invoice if it shows a different invoice number, or says "Page 1 of N". A
    page with no number continues the current invoice. Numbers are compared in a way that tolerates
    OCR confusions, so a misread repeated number does not split one invoice in two.
    """
    segments: list[Segment] = []
    current_key: str | None = None
    for page in pages:
        if not page.text:
            continue
        number = find_invoice_number(page.text)
        key = ocr_tolerant_key(number) if number else None
        is_first_page = _PAGE_ONE.search(page.text) is not None
        changed_number = key is not None and current_key is not None and key != current_key
        starts_new = changed_number or (is_first_page and (key is None or key != current_key))
        if starts_new or not segments:
            segments.append(Segment(pages=[page]))
            current_key = key
        else:
            segments[-1].pages.append(page)
            current_key = current_key or key
    return segments


def split_long_text(text: str, max_chars: int) -> list[str]:
    """Split at line boundaries, so that a table row is never cut in half."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        # A single line longer than the limit has nowhere natural to break.
        pieces = [line[i : i + max_chars] for i in range(0, len(line), max_chars)] or [""]
        for piece in pieces:
            if size + len(piece) + 1 > max_chars and current:
                chunks.append("\n".join(current))
                current, size = [], 0
            current.append(piece)
            size += len(piece) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def chunk_segment(segment: Segment, max_chars: int) -> list[str]:
    """Fit a segment into prompts of at most max_chars, marking which page each part came from."""
    chunks: list[str] = []
    current = ""
    for page in segment.pages:
        marker = f"[Page {page.page_number}]\n"
        for piece in split_long_text(page.text, max_chars - len(marker)):
            block = f"{marker}{piece}\n"
            if current and len(current) + len(block) > max_chars:
                chunks.append(current)
                current = ""
            current += block
    if current:
        chunks.append(current)
    return chunks
