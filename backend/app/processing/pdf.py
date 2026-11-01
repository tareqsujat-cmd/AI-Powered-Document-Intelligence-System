import logging
import time
from collections.abc import Iterator
from pathlib import Path
from statistics import median

import numpy as np
import pymupdf

from app.core.config import Settings
from app.db.enums import PageStatus, TextSource
from app.processing.errors import (
    CorruptedFileError,
    EmptyFileError,
    EncryptedFileError,
    ProcessingError,
)
from app.processing.ocr import Image, ocr_image
from app.processing.results import PageResult
from app.processing.text import clean_text

logger = logging.getLogger(__name__)

# x0, y0, x1, y1, text, block, line, word
_Word = tuple[float, float, float, float, str, int, int, int]

# A real text layer has at least this many letters and digits. Fewer is usually a page number or
# a stamp on top of a scan.
MIN_TEXT_LAYER_CHARS = 20
# A page mostly covered by an image, with little text, is a scan with a small overlay such as
# a header added by the scanner. OCR reads the whole page; the overlay alone would lose the content.
SCAN_IMAGE_COVERAGE = 0.5
SCAN_OVERLAY_MAX_CHARS = 200
# Text whose fonts lack a Unicode mapping comes out as replacement characters.
MAX_UNMAPPED_RATIO = 0.2
MAX_RENDER_PIXELS = 40_000_000
_STORE_SHRINK_EVERY_PAGES = 25
_COLUMN_GAP_RATIO = 1.5

# MuPDF prints damaged-file diagnostics to stderr; failures still surface as exceptions.
pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)

_TEXT_FLAGS = pymupdf.TEXTFLAGS_WORDS & ~pymupdf.TEXT_PRESERVE_LIGATURES


def _page_text(page: pymupdf.Page) -> str:
    """Rebuild visual lines, so that each row of an invoice table stays on one line.

    PDF text order is arbitrary: a table often comes out one cell per line, which loses the link
    between a description and its amount. A tab marks a wide gap, usually a column boundary.
    """
    words: list[_Word] = page.get_text("words", flags=_TEXT_FLAGS)
    if not words:
        return ""

    tolerance = median(word[3] - word[1] for word in words) / 2
    lines: list[list[_Word]] = []
    centers: list[float] = []
    for word in sorted(words, key=lambda w: (w[1] + w[3]) / 2):
        center = (word[1] + word[3]) / 2
        if centers and abs(center - centers[-1]) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
            centers.append(center)

    rendered: list[str] = []
    for line in lines:
        line.sort(key=lambda w: w[0])
        height = median(word[3] - word[1] for word in line)
        parts: list[str] = []
        for previous, word in zip([None, *line[:-1]], line, strict=True):
            if previous is not None:
                parts.append("\t" if word[0] - previous[2] > height * _COLUMN_GAP_RATIO else " ")
            parts.append(word[4])
        rendered.append("".join(parts))
    return "\n".join(rendered)


def _alnum_count(text: str) -> int:
    return sum(character.isalnum() for character in text)


def _image_coverage(page: pymupdf.Page) -> float:
    area = abs(page.rect)
    if not area:
        return 0.0
    # Clip to the page: scans are often placed slightly larger than the page they sit on.
    covered = (abs(pymupdf.Rect(info["bbox"]) & page.rect) for info in page.get_image_info())
    return float(min(sum(covered) / area, 1.0))


def needs_ocr(page: pymupdf.Page, text: str) -> bool:
    """Decide whether a page's text layer can be trusted. OCR is by far the slowest step."""
    chars = _alnum_count(text)
    if chars < MIN_TEXT_LAYER_CHARS:
        return True
    if text.count("�") / max(len(text), 1) > MAX_UNMAPPED_RATIO:
        return True
    return chars < SCAN_OVERLAY_MAX_CHARS and _image_coverage(page) >= SCAN_IMAGE_COVERAGE


def _render(page: pymupdf.Page, dpi: int) -> tuple[Image, int]:
    pixels = abs(page.rect) * (dpi / 72) ** 2
    if pixels > MAX_RENDER_PIXELS:
        dpi = int(dpi * (MAX_RENDER_PIXELS / pixels) ** 0.5)
    pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    array = np.frombuffer(pixmap.samples, dtype=np.uint8)
    return array.reshape(pixmap.height, pixmap.stride)[:, : pixmap.width].copy(), dpi


def _process_page(document: pymupdf.Document, index: int, settings: Settings) -> PageResult:
    started = time.perf_counter()
    result = PageResult(page_number=index + 1)
    try:
        page = document.load_page(index)
        text = clean_text(_page_text(page))
        if needs_ocr(page, text):
            image, dpi = _render(page, settings.ocr_dpi)
            ocr = ocr_image(image, settings, dpi=dpi)
            result.source = TextSource.OCR
            result.ocr_confidence = ocr.confidence
            # A stamp in the text layer beats nothing if the rest of the page cannot be read.
            result.text = ocr.text or text
        else:
            result.source = TextSource.TEXT_LAYER
            result.text = text
        if not result.text:
            result.status = PageStatus.EMPTY
    except ProcessingError:
        raise
    except Exception as exc:
        logger.warning("page extraction failed", extra={"page": index + 1}, exc_info=True)
        result.status = PageStatus.FAILED
        result.error = f"Page {index + 1} could not be read ({type(exc).__name__})."
    result.processing_ms = int((time.perf_counter() - started) * 1000)
    return result


def iter_pdf_pages(path: Path, settings: Settings, *, start_page: int = 1) -> Iterator[PageResult]:
    """Yield pages one at a time, so memory use does not grow with the length of the document."""
    try:
        document = pymupdf.open(path, filetype="pdf")
    except Exception as exc:
        raise CorruptedFileError("The PDF is damaged and could not be opened.") from exc

    try:
        if document.needs_pass:
            raise EncryptedFileError("The PDF is password protected.")
        if document.page_count == 0:
            raise EmptyFileError("The PDF has no pages.")
        for index in range(start_page - 1, document.page_count):
            yield _process_page(document, index, settings)
            if (index + 1) % _STORE_SHRINK_EVERY_PAGES == 0:
                pymupdf.TOOLS.store_shrink(100)
    finally:
        document.close()
