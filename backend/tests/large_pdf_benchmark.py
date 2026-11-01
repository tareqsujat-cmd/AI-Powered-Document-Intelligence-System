"""Measure real OCR on a fully scanned PDF. Run: python -m tests.large_pdf_benchmark [pages]"""

import statistics
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
import psutil
import pymupdf

from app.core.config import Settings
from app.db.enums import PageStatus
from app.processing.pdf import iter_pdf_pages
from tests.document_fixtures import INVOICE_TOKENS, add_noise, render_invoice


def build(path: Path, pages: int) -> None:
    base = add_noise(render_invoice(200), 6)
    with pymupdf.open() as document:
        for index in range(pages):
            page = document.new_page(width=595, height=842)
            scan = base.copy()
            # A unique image per page, so MuPDF cannot serve every page from one cached copy.
            scan[5:9, 5:405] = np.frombuffer(
                index.to_bytes(4, "big") * 400, dtype=np.uint8
            ).reshape(4, 400)
            # JPEG, as real scanners produce: noisy PNGs would make a multi-gigabyte test file.
            ok, jpeg = cv2.imencode(".jpg", scan, [cv2.IMWRITE_JPEG_QUALITY, 80])
            assert ok
            page.insert_image(page.rect, stream=jpeg.tobytes())
        document.save(path)


def main(pages: int) -> None:
    settings = Settings(_env_file=None)
    process = psutil.Process()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "scanned.pdf"
        started = time.perf_counter()
        build(path, pages)
        print(
            f"built {pages} scanned pages in {time.perf_counter() - started:.0f}s, "
            f"{path.stat().st_size / 1024 / 1024:.0f} MB",
            flush=True,
        )

        durations: list[int] = []
        recalls: list[float] = []
        confidences: list[float] = []
        failed = 0
        started = time.perf_counter()
        for page in iter_pdf_pages(path, settings):
            durations.append(page.processing_ms)
            failed += page.status is PageStatus.FAILED
            recalls.append(
                sum(token in page.text for token in INVOICE_TOKENS) / len(INVOICE_TOKENS)
            )
            if page.ocr_confidence is not None:
                confidences.append(page.ocr_confidence)
            if page.page_number % max(pages // 10, 1) == 0:
                rss = process.memory_info().rss / 1024 / 1024
                elapsed = time.perf_counter() - started
                print(
                    f"page {page.page_number}: {elapsed:.0f}s elapsed, RSS {rss:.0f} MB", flush=True
                )
        elapsed = time.perf_counter() - started

    ordered = sorted(durations)
    print(f"pages: {len(durations)}, failed: {failed}")
    print(f"total: {elapsed:.0f}s ({elapsed / 60:.1f} min), {elapsed / len(durations):.2f} s/page")
    print(
        f"page ms: median {statistics.median(ordered):.0f}, "
        f"p95 {ordered[int(len(ordered) * 0.95)]}, max {ordered[-1]}"
    )
    print(f"mean token recall: {statistics.mean(recalls):.4f}, min {min(recalls):.2f}")
    print(f"mean OCR confidence: {statistics.mean(confidences):.3f}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000)
