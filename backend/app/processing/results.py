from dataclasses import dataclass, field

from app.db.enums import PageStatus, TextSource


@dataclass(slots=True)
class SheetTable:
    """Structured content of one sheet, kept for rule-based column mapping.

    Cells are normalized strings: numbers without float noise, dates as ISO 8601, blanks as "".
    `header_row` is the 1-based row number in the sheet, or None if no header row was found, in
    which case `headers` is empty and every row is in `rows`. `preamble` holds the rows above the
    header, which often carry the invoice number and date.
    """

    headers: list[str]
    rows: list[list[str]]
    header_row: int | None = None
    preamble: list[list[str]] = field(default_factory=list)


@dataclass(slots=True)
class PageResult:
    """One PDF page, image frame, or sheet. Field names match the document_pages columns."""

    page_number: int
    text: str = ""
    source: TextSource | None = None
    status: PageStatus = PageStatus.OK
    ocr_confidence: float | None = None
    processing_ms: int = 0
    error: str | None = None
    sheet_name: str | None = None
    warnings: list[str] = field(default_factory=list)
    table: SheetTable | None = None
