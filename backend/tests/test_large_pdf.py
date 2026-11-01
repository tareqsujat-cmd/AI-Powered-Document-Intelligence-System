from pathlib import Path

import numpy as np
import psutil
import pymupdf
import pytest

from app.core.config import Settings
from app.db.enums import TextSource
from app.processing import pdf
from app.processing.ocr import OcrResult
from app.processing.pdf import iter_pdf_pages
from tests.document_fixtures import draw_invoice, encode_png, render_invoice

PAGES = 1000
SCAN_EVERY = 10


def build_large_pdf(path: Path) -> None:
    """Mostly digital, with every tenth page a distinct scan, like a real batch."""
    base = render_invoice(100)
    with pymupdf.open() as document:
        for index in range(PAGES):
            page = document.new_page(width=595, height=842)
            if index % SCAN_EVERY == 0:
                # A unique image per scan page, so that MuPDF cannot share one cached copy.
                scan = base.copy()
                scan[10:30, 10:210] = np.frombuffer(
                    index.to_bytes(4, "big") * 1000, dtype=np.uint8
                ).reshape(20, 200)
                page.insert_image(page.rect, stream=encode_png(scan))
            else:
                draw_invoice(page, f"INV-{index:06d}")
        document.save(path)


def test_memory_does_not_grow_with_page_count(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real rendering at full DPI, but no Tesseract: this test is about memory, not OCR speed.
    monkeypatch.setattr(
        pdf,
        "ocr_image",
        lambda *_args, **_kwargs: OcrResult("scanned text", 0.9, 2, 11),
    )
    path = tmp_path / "large.pdf"
    build_large_pdf(path)

    process = psutil.Process()
    rss_by_page: dict[int, int] = {}
    sources = {TextSource.TEXT_LAYER: 0, TextSource.OCR: 0}
    last_page = 0
    for page in iter_pdf_pages(path, settings):
        last_page = page.page_number
        sources[page.source] += 1
        rss_by_page[page.page_number] = process.memory_info().rss

    assert last_page == PAGES
    assert sources == {TextSource.OCR: PAGES // SCAN_EVERY, TextSource.TEXT_LAYER: PAGES * 9 // 10}

    # Allow for warm-up, then compare early against late: 10x the pages must not mean 10x memory.
    early = max(rss_by_page[n] for n in range(90, 111))
    late = max(rss_by_page[n] for n in range(PAGES - 20, PAGES + 1))
    growth_mb = (late - early) / (1024 * 1024)
    assert growth_mb < 50, f"RSS grew {growth_mb:.0f} MB between page 100 and page {PAGES}"
