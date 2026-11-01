import codecs
import csv
import io
import logging
import math
import os
import re
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from itertools import islice
from pathlib import Path
from typing import Any, BinaryIO, TextIO

import xlrd
from charset_normalizer import from_bytes
from openpyxl import load_workbook
from openpyxl.cell.cell import ERROR_CODES
from openpyxl.xml.constants import SHEET_MAIN_NS
from openpyxl.xml.functions import iterparse

from app.core.config import Settings
from app.db.enums import FileType, PageStatus, TextSource
from app.processing.errors import CorruptedFileError, EncryptedFileError, UnsupportedFileError
from app.processing.results import PageResult, SheetTable
from app.processing.text import CONTROL_CHARS, clean_text

logger = logging.getLogger(__name__)

# Rounds away binary float noise while keeping any precision an invoice or an FX rate really uses.
_FLOAT_DECIMALS = 9

_HEADER_SCAN_ROWS = 30
_HEADER_LOOKAHEAD_ROWS = 5
_MIN_HEADER_SCORE = 1.0
_MIN_HEADER_LABEL_SHARE = 0.6
_MIN_HEADER_DISTINCT_SHARE = 0.8
_MIN_COLUMN_OVERLAP = 0.7
_HEADER_KEYWORDS = re.compile(
    r"(?<![a-z])(description|details?|item|product|service|sku|code|qty|quantity|units?|price|rate"
    r"|amount|subtotal|total|tax|vat|gst|discount|currency|invoice|date|supplier|vendor|customer"
    r")(?![a-z])"
)

_READ_CHUNK_BYTES = 1024 * 1024
_ENCODING_SAMPLE_BYTES = 1024 * 1024
_ENCODING_SAMPLE_LEAD_BYTES = 1024
_DELIMITER_SAMPLE_CHARS = 64 * 1024
# On a tie, the delimiter listed first wins: a European semicolon file can have the same number of
# decimal commas on every row, but a comma file rarely has a constant number of semicolons.
_DELIMITERS = ("\t", ";", "|", ",")
# UTF-32 LE has to come first, because its BOM begins with the UTF-16 LE BOM.
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]+")
_WESTERN_PUNCTUATION = frozenset("’‘“”–—…€£¥°™®")
_MAX_SUSPICIOUS_RUN_SHARE = 0.1

# csv.field_size_limit is process-wide. The default of 128 KB is too low for long description
# fields, and this still bounds what one stray quote can swallow.
_CSV_FIELD_LIMIT_CHARS = 16 * 1024 * 1024
csv.field_size_limit(_CSV_FIELD_LIMIT_CHARS)

_XLSX_ROW_TAG = f"{{{SHEET_MAIN_NS}}}row"
_XLSX_CELL_TAG = f"{{{SHEET_MAIN_NS}}}c"
_XLSX_FORMULA_TAG = f"{{{SHEET_MAIN_NS}}}f"
_XLSX_VALUE_TAG = f"{{{SHEET_MAIN_NS}}}v"
_XLSX_FORMULA_ELEMENT = re.compile(rb"<(?:[\w.-]+:)?f[\s/>]")
_XLSX_FORMULA_OVERLAP_BYTES = 64

_UNREADABLE_WORKBOOK = "The spreadsheet is damaged or is not a valid Excel workbook."
_ENCRYPTED_WORKBOOK = "This Excel workbook is password protected."


@dataclass(slots=True)
class _ParsedSheet:
    table: SheetTable
    truncated: bool
    last_row: int


def _format_float(value: float) -> str:
    if not math.isfinite(value):
        return ""
    rounded = round(value, _FLOAT_DECIMALS)
    if rounded.is_integer():
        return str(int(rounded))
    return format(Decimal(repr(rounded)), "f")


def _format_datetime(value: datetime) -> str:
    if value.microsecond:
        # Excel stores times as fractions of a day, so they come back with sub-second noise.
        value = (value + timedelta(microseconds=500_000)).replace(microsecond=0)
    if value.time() == clock_time.min:
        return value.date().isoformat()
    return value.isoformat()


def normalize_cell(value: object) -> str:
    """Convert a cell from any of the three formats to the string form kept in SheetTable."""
    if value is None:
        return ""
    if isinstance(value, str):
        # PostgreSQL rejects NUL in TEXT and JSONB, so these must not reach `table` either.
        return CONTROL_CHARS.sub("", value).strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, datetime):
        return _format_datetime(value)
    if isinstance(value, date | clock_time):
        return value.isoformat()
    return str(value).strip()


def _has_digit(cell: str) -> bool:
    return any(character.isdigit() for character in cell)


def _sits_above_values(header: list[str], row: list[str]) -> bool:
    """True if `row` has about the same columns as `header`, and values under its labels."""
    header_columns = {index for index, cell in enumerate(header) if cell}
    row_columns = {index for index, cell in enumerate(row) if cell}
    shared = header_columns & row_columns
    if len(shared) < _MIN_COLUMN_OVERLAP * len(header_columns | row_columns):
        return False
    value_columns = [index for index in shared if _has_digit(row[index])]
    # Digits above digits mean two data rows, which keeps data rows from being taken for a header.
    # Half are allowed, because titles such as "VAT 20%" are normal.
    titled = sum(not _has_digit(header[index]) for index in value_columns)
    return bool(value_columns) and titled * 2 >= len(value_columns)


def _header_score(row: list[str], following: list[list[str]]) -> float:
    labels = [cell for cell in row if cell]
    if len(labels) < 2 or not following:
        return 0.0
    # "Invoice No: | INV-1" is a key and a value, not column titles.
    if any(label.endswith(":") for label in labels):
        return 0.0
    if len({label.casefold() for label in labels}) < _MIN_HEADER_DISTINCT_SHARE * len(labels):
        return 0.0
    if sum(not _has_digit(label) for label in labels) < _MIN_HEADER_LABEL_SHARE * len(labels):
        return 0.0
    fitting = sum(_sits_above_values(row, below) for below in following)
    if not fitting:
        return 0.0
    keywords = sum(bool(_HEADER_KEYWORDS.search(label.casefold())) for label in labels)
    return 2 * fitting / len(following) + 0.5 * min(keywords, 3)


def _find_header(rows: list[list[str]]) -> int | None:
    best_index = None
    best_score = 0.0
    for index, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        following = rows[index + 1 : index + 1 + _HEADER_LOOKAHEAD_ROWS]
        score = _header_score(row, following)
        if score > best_score:
            best_index, best_score = index, score
    return best_index if best_score >= _MIN_HEADER_SCORE else None


def _non_blank_rows(rows: Iterable[list[str]]) -> Iterator[tuple[int, list[str]]]:
    """Yield (1-based sheet row number, cells), without trailing blanks or fully blank rows."""
    for number, cells in enumerate(rows, start=1):
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            yield number, cells


def _read_table(rows: Iterable[list[str]], max_rows: int) -> _ParsedSheet:
    numbered = _non_blank_rows(rows)
    head = list(islice(numbered, _HEADER_SCAN_ROWS + _HEADER_LOOKAHEAD_ROWS))
    header_index = _find_header([cells for _, cells in head])

    table = SheetTable(headers=[], rows=[])
    data = head
    if header_index is not None:
        table.header_row, table.headers = head[header_index]
        table.preamble = [cells for _, cells in head[:header_index]]
        data = head[header_index + 1 :]

    truncated = len(data) > max_rows
    data = data[:max_rows]
    table.rows = [cells for _, cells in data]
    last_row = data[-1][0] if data else 0
    if not truncated:
        # One row past the limit is read, to tell a sheet of exactly max_rows from a longer one.
        for number, cells in numbered:
            if len(table.rows) >= max_rows:
                truncated = True
                break
            table.rows.append(cells)
            last_row = number
    return _ParsedSheet(table=table, truncated=truncated, last_row=last_row)


def _render_text(table: SheetTable, sheet_name: str | None) -> str:
    rows = [*table.preamble, *([table.headers] if table.headers else []), *table.rows]
    if not rows:
        return ""
    lines = [f"Sheet: {sheet_name}"] if sheet_name else []
    # A newline inside a cell would otherwise split one table row across lines.
    lines.extend(" | ".join(cell.replace("\n", " ") for cell in row if cell) for row in rows)
    return clean_text("\n".join(lines))


def _finish_page(
    number: int,
    sheet_name: str | None,
    parsed: _ParsedSheet,
    warnings: list[str],
    started: float,
    max_rows: int,
) -> PageResult:
    if parsed.truncated:
        warnings.append(f"Only the first {max_rows} data rows were read; the rest were dropped.")
    text = _render_text(parsed.table, sheet_name)
    return PageResult(
        page_number=number,
        text=text,
        source=TextSource.SPREADSHEET,
        status=PageStatus.OK if text else PageStatus.EMPTY,
        processing_ms=int((time.perf_counter() - started) * 1000),
        sheet_name=sheet_name,
        warnings=warnings,
        table=parsed.table,
    )


def _failed_page(number: int, sheet_name: str, exc: Exception, started: float) -> PageResult:
    logger.warning("sheet failed", extra={"page": number}, exc_info=True)
    return PageResult(
        page_number=number,
        source=TextSource.SPREADSHEET,
        status=PageStatus.FAILED,
        processing_ms=int((time.perf_counter() - started) * 1000),
        error=f"Sheet {sheet_name!r} could not be read ({type(exc).__name__}).",
        sheet_name=sheet_name,
    )


def _error_cell_warning(count: int) -> str:
    return f"{count} cell(s) contain a spreadsheet error such as #REF! and were read as blank."


def _has_formula_element(sheet: Any) -> bool:
    with sheet._get_source() as source:
        tail = b""
        while chunk := source.read(_READ_CHUNK_BYTES):
            if _XLSX_FORMULA_ELEMENT.search(tail + chunk):
                return True
            tail = chunk[-_XLSX_FORMULA_OVERLAP_BYTES:]
    return False


def _count_uncached_formulas(sheet: Any, last_row: int | None) -> int:
    """Count formula cells with no stored result, in rows up to `last_row` if it is given.

    This reads the XML rather than comparing against a data_only=False workbook, because openpyxl
    reports None both for "no result" and for a formula that Excel calculated as an empty string.
    Excel writes t="str" and an empty <v> for the latter, and libraries write no type.
    """
    # Most sheets have no formulas, and a raw search takes a small fraction of a second XML parse.
    if not _has_formula_element(sheet):
        return 0
    count = 0
    row_number = 0
    # The sheet's zip member is only reachable through this private method. openpyxl is pinned, and
    # the tests fail if it goes away.
    with sheet._get_source() as source:
        for _event, element in iterparse(source):
            if element.tag != _XLSX_ROW_TAG:
                continue
            row_number = int(element.get("r") or row_number + 1)
            if last_row is not None and row_number > last_row:
                break
            for cell in element.iterfind(_XLSX_CELL_TAG):
                if cell.find(_XLSX_FORMULA_TAG) is None:
                    continue
                value = cell.find(_XLSX_VALUE_TAG)
                if value is None or (not value.text and cell.get("t") != "str"):
                    count += 1
            element.clear()
    return count


def _read_xlsx_sheet(sheet: Any, number: int, max_rows: int) -> PageResult:
    started = time.perf_counter()
    name = normalize_cell(sheet.title)
    error_cells = 0

    def rows() -> Iterator[list[str]]:
        nonlocal error_cells
        for values in sheet.iter_rows(values_only=True):
            cells = []
            for value in values:
                if isinstance(value, str) and value in ERROR_CODES:
                    error_cells += 1
                    cells.append("")
                else:
                    cells.append(normalize_cell(value))
            yield cells

    try:
        # Some writers store a wrong <dimension>, which would make read-only mode drop real cells.
        sheet.reset_dimensions()
        parsed = _read_table(rows(), max_rows)
        uncached = _count_uncached_formulas(sheet, parsed.last_row if parsed.truncated else None)
    except Exception as exc:
        return _failed_page(number, name, exc, started)

    warnings = []
    if uncached:
        warnings.append(
            f"{uncached} formula cell(s) have no cached value and were read as blank. "
            "Open and save the file in Excel to store the calculated values."
        )
    if error_cells:
        warnings.append(_error_cell_warning(error_cells))
    return _finish_page(number, name, parsed, warnings, started, max_rows)


def _open_xlsx(stream: BinaryIO) -> Any:
    # A stream, because given a path openpyxl rejects any file not named .xlsx, and stored uploads
    # need not be. Parsers of hostile input raise nearly anything, so this catches broadly.
    try:
        return load_workbook(stream, read_only=True, data_only=True, keep_links=False)
    except Exception as exc:
        logger.warning("xlsx failed to open", exc_info=True)
        raise CorruptedFileError(_UNREADABLE_WORKBOOK) from exc


def _iter_xlsx(path: Path, settings: Settings) -> Iterator[PageResult]:
    # Filesystem errors stay outside _open_xlsx: a missing or unreadable file is not a corrupt one.
    with path.open("rb") as stream:
        workbook = _open_xlsx(stream)
        try:
            for number, sheet in enumerate(workbook.worksheets, start=1):
                yield _read_xlsx_sheet(sheet, number, settings.max_sheet_rows)
        finally:
            workbook.close()


def _xls_cell(kind: int, value: object, datemode: int) -> str:
    if kind == xlrd.XL_CELL_DATE:
        try:
            return normalize_cell(xlrd.xldate_as_datetime(value, datemode))
        except (ValueError, OverflowError, xlrd.XLDateError):
            return normalize_cell(value)
    if kind == xlrd.XL_CELL_BOOLEAN:
        return normalize_cell(bool(value))
    if kind in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
        return ""
    return normalize_cell(value)


def _read_xls_sheet(book: Any, index: int, name: str, max_rows: int) -> PageResult:
    started = time.perf_counter()
    number = index + 1
    name = normalize_cell(name)
    error_cells = 0

    def rows(sheet: Any) -> Iterator[list[str]]:
        nonlocal error_cells
        for row in range(sheet.nrows):
            kinds = sheet.row_types(row)
            error_cells += sum(kind == xlrd.XL_CELL_ERROR for kind in kinds)
            yield [
                _xls_cell(kind, value, book.datemode)
                for kind, value in zip(kinds, sheet.row_values(row), strict=True)
            ]

    try:
        parsed = _read_table(rows(book.sheet_by_index(index)), max_rows)
    except Exception as exc:
        return _failed_page(number, name, exc, started)
    finally:
        if book.sheet_loaded(index):
            book.unload_sheet(index)

    warnings = [_error_cell_warning(error_cells)] if error_cells else []
    return _finish_page(number, name, parsed, warnings, started, max_rows)


def _open_xls(path: Path, log: TextIO) -> Any:
    try:
        return xlrd.open_workbook(str(path), on_demand=True, logfile=log)
    except xlrd.XLRDError as exc:
        logger.warning("xls failed to open", exc_info=True)
        if "encrypted" in str(exc).lower():
            raise EncryptedFileError(_ENCRYPTED_WORKBOOK) from exc
        raise CorruptedFileError(_UNREADABLE_WORKBOOK) from exc
    except OSError:
        raise
    except Exception as exc:
        logger.warning("xls failed to open", exc_info=True)
        raise CorruptedFileError(_UNREADABLE_WORKBOOK) from exc


def _iter_xls(path: Path, settings: Settings) -> Iterator[PageResult]:
    # xlrd prints file problems to stdout unless it is given somewhere else to log them.
    with open(os.devnull, "w", encoding="utf-8") as log:
        book = _open_xls(path, log)
        try:
            for index, name in enumerate(book.sheet_names()):
                yield _read_xls_sheet(book, index, name, settings.max_sheet_rows)
        finally:
            book.release_resources()


def _first_invalid_utf8(stream: BinaryIO) -> int | None:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    position = 0
    try:
        while chunk := stream.read(_READ_CHUNK_BYTES):
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                return position + exc.start
            position += len(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return position
    return None


def _mostly_utf8(sample: bytes) -> bool:
    """A UTF-8 file with a few damaged bytes, which still reads best as UTF-8."""
    decoded = sample.decode("utf-8", errors="replace")
    damaged = decoded.count("\N{REPLACEMENT CHARACTER}")
    return sum(not character.isascii() for character in decoded) - damaged > 10 * damaged


def _looks_western(text: str) -> bool:
    """Whether bytes decoded as cp1252 read like Western European text.

    In such text, non-ASCII characters are mostly lone letters inside ASCII words. Other encodings
    decoded as cp1252 give long runs of accented letters, or symbols stuck to letters ("Za¿ó³æ").
    """
    runs = suspicious = 0
    for match in _NON_ASCII_RUN.finditer(text):
        runs += 1
        run = match.group()
        symbols = [c for c in run if not c.isalpha() and c not in _WESTERN_PUNCTUATION]
        neighbours = text[max(match.start() - 1, 0) : match.start()] + text[match.end() :][:1]
        touches_letter = any(c.isalpha() for c in run + neighbours)
        if len(run) > 2 or (symbols and touches_letter):
            suspicious += 1
    return suspicious <= runs * _MAX_SUSPICIOUS_RUN_SHARE


def _guess_legacy_encoding(sample: bytes) -> str:
    if _mostly_utf8(sample):
        return "utf-8"
    # Detector confidence does not help here. On short Western text it reports e.g. cp775 with
    # coherence 0, or cp1257 "Romanian" with 0.62, higher than it gives correct Russian. So cp1252
    # is tested first, by what the decoded text looks like.
    try:
        if _looks_western(sample.decode("cp1252")):
            return "cp1252"
    except UnicodeDecodeError:
        pass
    match = from_bytes(sample).best()
    return match.encoding if match is not None else "cp1252"


def _detect_csv_encoding(path: Path) -> tuple[str, str | None]:
    """Return the encoding, and a warning if it was a guess rather than known from the file."""
    with path.open("rb") as stream:
        head = stream.read(4)
        for bom, encoding in _BOMS:
            if head.startswith(bom):
                return encoding, None

        stream.seek(0)
        invalid_at = _first_invalid_utf8(stream)
        if invalid_at is None:
            return "utf-8", None

        # From the first bad byte, since an ASCII-only start of the file says nothing.
        sample_start = max(invalid_at - _ENCODING_SAMPLE_LEAD_BYTES, 0)
        stream.seek(sample_start)
        sample = stream.read(_ENCODING_SAMPLE_BYTES)

    # Multi-byte detectors reject a sample that ends in the middle of a character.
    last_newline = sample.rfind(b"\n")
    if last_newline > invalid_at - sample_start:
        sample = sample[: last_newline + 1]
    encoding = _guess_legacy_encoding(sample)
    return encoding, (
        f"The file is not valid UTF-8 and was read as {encoding}, so some characters may be wrong."
    )


def _detect_delimiter(sample: str) -> str:
    """Choose the delimiter that gives the most rows of one same width, ignoring one-column rows."""
    best = ","
    best_rows = 0
    for delimiter in _DELIMITERS:
        reader = csv.reader(io.StringIO(sample, newline=""), delimiter=delimiter)
        try:
            widths = Counter(len(record) for record in reader)
        except csv.Error:
            continue
        rows = max((count for width, count in widths.items() if width >= 2), default=0)
        if rows > best_rows:
            best, best_rows = delimiter, rows
    return best


def _iter_csv(path: Path, settings: Settings) -> Iterator[PageResult]:
    started = time.perf_counter()
    encoding, encoding_warning = _detect_csv_encoding(path)
    warnings = [encoding_warning] if encoding_warning else []

    def rows(reader: Iterator[list[str]]) -> Iterator[list[str]]:
        count = 0
        try:
            for record in reader:
                count += 1
                yield [normalize_cell(cell) for cell in record]
        except csv.Error as exc:
            if not count:
                raise CorruptedFileError(
                    "The CSV file is malformed and could not be read."
                ) from exc
            warnings.append(
                f"Reading stopped at row {count + 1} because the CSV is malformed there; "
                "the rows after it were dropped."
            )

    # newline="" because csv.reader has to see the newlines inside quoted fields itself.
    with path.open(encoding=encoding, errors="replace", newline="") as stream:
        delimiter = _detect_delimiter(stream.read(_DELIMITER_SAMPLE_CHARS))
        stream.seek(0)
        reader = csv.reader(stream, delimiter=delimiter, skipinitialspace=True)
        parsed = _read_table(rows(reader), settings.max_sheet_rows)
    yield _finish_page(1, None, parsed, warnings, started, settings.max_sheet_rows)


def iter_spreadsheet_pages(
    path: Path, file_type: FileType, settings: Settings
) -> Iterator[PageResult]:
    """Yield one page per sheet as it is read, or a single page for a CSV.

    Blank rows are skipped, and blank cells at the end of a row are dropped, so a row in
    `table.rows` can be shorter than `table.headers`. Raises ProcessingError while iterating if the
    workbook cannot be opened. A sheet that fails after that is yielded with status=failed.
    """
    if file_type is FileType.XLSX:
        yield from _iter_xlsx(path, settings)
    elif file_type is FileType.XLS:
        yield from _iter_xls(path, settings)
    elif file_type is FileType.CSV:
        yield from _iter_csv(path, settings)
    else:
        raise UnsupportedFileError(f"{file_type.value} is not a spreadsheet format.")
