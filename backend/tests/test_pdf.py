from pathlib import Path

import numpy as np
import pymupdf
import pytest

from app.core.config import Settings
from app.db.enums import PageStatus, TextSource
from app.processing import pdf
from app.processing.errors import (
    CorruptedFileError,
    EmptyFileError,
    EncryptedFileError,
    OcrUnavailableError,
)
from app.processing.ocr import OcrResult
from app.processing.pdf import iter_pdf_pages, needs_ocr
from tests.document_fixtures import (
    INVOICE_TOKENS,
    draw_invoice,
    insert_scan,
    make_digital_pdf,
    make_scanned_pdf,
    render_invoice,
)

FAKE_OCR = OcrResult(text="text from ocr", confidence=0.9, word_count=3, reliable_chars=11)


@pytest.fixture
def ocr_calls(monkeypatch: pytest.MonkeyPatch) -> list[np.ndarray]:
    """Replaces OCR, recording each image it is asked to read."""
    calls: list[np.ndarray] = []

    def fake(image: np.ndarray, _settings: Settings, *, dpi: int | None = None) -> OcrResult:
        calls.append(image)
        return FAKE_OCR

    monkeypatch.setattr(pdf, "ocr_image", fake)
    return calls


def mixed_pdf(path: Path) -> Path:
    """Page 1 digital, page 2 scanned, page 3 blank, page 4 digital."""
    scan = render_invoice(100)
    with pymupdf.open() as document:
        draw_invoice(document.new_page(width=595, height=842))
        insert_scan(document, scan)
        document.new_page(width=595, height=842)
        draw_invoice(document.new_page(width=595, height=842))
        document.save(path)
    return path


def test_digital_pdf_is_read_from_the_text_layer_without_ocr(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    pages = list(iter_pdf_pages(make_digital_pdf(tmp_path / "a.pdf"), settings))

    assert len(pages) == 1
    assert pages[0].source is TextSource.TEXT_LAYER
    assert pages[0].status is PageStatus.OK
    assert pages[0].ocr_confidence is None
    assert all(token in pages[0].text for token in INVOICE_TOKENS)
    assert ocr_calls == []


def test_table_rows_stay_on_one_line(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    text = next(iter_pdf_pages(make_digital_pdf(tmp_path / "a.pdf"), settings)).text
    lines = text.splitlines()

    row = next(line for line in lines if "Industrial Bearing Kit" in line)
    assert row.split() == ["Industrial", "Bearing", "Kit", "4", "312.50", "1,250.00"]
    assert row.split("\t")[0] == "Industrial Bearing Kit"  # the column boundary survives


def test_lines_are_in_visual_order(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    text = next(iter_pdf_pages(make_digital_pdf(tmp_path / "a.pdf"), settings)).text

    assert text.index("INVOICE") < text.index("Description") < text.index("Subtotal")
    assert text.index("Subtotal") < text.index("Total", text.index("Subtotal") + 1)


def test_pages_are_numbered_from_one(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    pages = list(iter_pdf_pages(make_digital_pdf(tmp_path / "a.pdf", pages=3), settings))

    assert [page.page_number for page in pages] == [1, 2, 3]
    assert "INV-2026-0043" in pages[1].text
    assert all(page.processing_ms >= 0 for page in pages)


def test_mixed_pdf_only_runs_ocr_on_scanned_and_blank_pages(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    pages = list(iter_pdf_pages(mixed_pdf(tmp_path / "a.pdf"), settings))

    assert [page.source for page in pages] == [
        TextSource.TEXT_LAYER,
        TextSource.OCR,
        TextSource.OCR,
        TextSource.TEXT_LAYER,
    ]
    assert len(ocr_calls) == 2
    assert pages[1].text == "text from ocr"
    assert pages[1].ocr_confidence == 0.9
    assert pages[0].ocr_confidence is None


def test_scan_with_a_small_text_overlay_is_still_ocred(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    path = tmp_path / "a.pdf"
    with pymupdf.open() as document:
        page = insert_scan(document, render_invoice(100))
        page.insert_text(
            (50, 20), "Scanned by OfficeCopier 3000 on 2026-03-15 at 10:42", fontsize=8
        )
        document.save(path)

    pages = list(iter_pdf_pages(path, settings))

    assert pages[0].source is TextSource.OCR
    assert pages[0].text == "text from ocr"


def test_searchable_scan_with_a_full_text_layer_uses_the_text_layer(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    path = tmp_path / "a.pdf"
    with pymupdf.open() as document:
        page = insert_scan(document, render_invoice(100))
        for line in range(20):
            page.insert_text(
                (50, 100 + line * 20), "hidden ocr text from an earlier scan run", fontsize=9
            )
        document.save(path)

    pages = list(iter_pdf_pages(path, settings))

    assert pages[0].source is TextSource.TEXT_LAYER
    assert ocr_calls == []


def test_small_logo_on_a_short_digital_page_does_not_trigger_ocr(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    path = tmp_path / "a.pdf"
    logo = np.full((50, 50), 80, dtype=np.uint8)
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=842)
        page.insert_text((50, 100), "Invoice INV-77 total due 500.00 USD", fontsize=11)
        page.insert_image(
            pymupdf.Rect(400, 20, 450, 70),
            stream=pymupdf.Pixmap(pymupdf.csGRAY, 50, 50, logo.tobytes(), False).tobytes("png"),
        )
        document.save(path)

    pages = list(iter_pdf_pages(path, settings))

    assert pages[0].source is TextSource.TEXT_LAYER
    assert ocr_calls == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", True),
        ("Page 3", True),
        ("a" * 19, True),
        ("a" * 20, False),
        ("�" * 30 + "a" * 25, True),
        ("!!! ??? --- " * 20, True),
    ],
)
def test_needs_ocr_on_a_page_without_images(tmp_path: Path, text: str, expected: bool) -> None:
    with pymupdf.open() as document:
        page = document.new_page()

        assert needs_ocr(page, text) is expected


def test_blank_page_with_no_ink_is_empty(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pdf, "ocr_image", lambda *_a, **_k: OcrResult(text="", confidence=None, word_count=0)
    )
    path = tmp_path / "a.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.save(path)

    pages = list(iter_pdf_pages(path, settings))

    assert pages[0].status is PageStatus.EMPTY
    assert pages[0].text == ""


def test_short_text_is_kept_when_ocr_reads_nothing(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pdf, "ocr_image", lambda *_a, **_k: OcrResult(text="", confidence=None, word_count=0)
    )
    path = tmp_path / "a.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 100), "Page 3", fontsize=11)
        document.save(path)

    pages = list(iter_pdf_pages(path, settings))

    assert pages[0].text == "Page 3"
    assert pages[0].status is PageStatus.OK


def test_one_bad_page_does_not_stop_the_others(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch, ocr_calls: list[np.ndarray]
) -> None:
    real = pdf._page_text
    calls = {"count": 0}

    def flaky(page: pymupdf.Page) -> str:
        calls["count"] += 1
        if calls["count"] == 2:
            raise ValueError("content stream is damaged at /secret/path")
        return real(page)

    monkeypatch.setattr(pdf, "_page_text", flaky)

    pages = list(iter_pdf_pages(make_digital_pdf(tmp_path / "a.pdf", pages=3), settings))

    assert [page.status for page in pages] == [PageStatus.OK, PageStatus.FAILED, PageStatus.OK]
    assert pages[1].error == "Page 2 could not be read (ValueError)."
    assert "secret" not in pages[1].error


def test_ocr_engine_failure_stops_the_document(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_a: object, **_k: object) -> None:
        raise OcrUnavailableError("The OCR engine is not available.")

    monkeypatch.setattr(pdf, "ocr_image", unavailable)
    pages = iter_pdf_pages(make_scanned_pdf(tmp_path / "a.pdf", dpi=50), settings)

    with pytest.raises(OcrUnavailableError):
        list(pages)


def test_pages_are_processed_lazily(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    processed: list[int] = []
    real = pdf._process_page

    def spy(document: pymupdf.Document, index: int, page_settings: Settings):
        processed.append(index)
        return real(document, index, page_settings)

    monkeypatch.setattr(pdf, "_process_page", spy)
    pages = iter_pdf_pages(make_digital_pdf(tmp_path / "a.pdf", pages=5), settings)

    assert processed == []
    next(pages)
    assert processed == [0]
    next(pages)
    assert processed == [0, 1]
    pages.close()


def test_huge_page_is_rendered_within_the_pixel_limit(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    path = tmp_path / "a.pdf"
    with pymupdf.open() as document:
        document.new_page(width=14400, height=14400)  # 200 x 200 inches
        document.save(path)

    list(iter_pdf_pages(path, settings))

    assert ocr_calls[0].size <= pdf.MAX_RENDER_PIXELS


def test_rendered_page_has_no_stride_padding(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    list(iter_pdf_pages(make_scanned_pdf(tmp_path / "a.pdf", dpi=50), settings))

    height, width = ocr_calls[0].shape
    assert abs(height - 842 * 300 / 72) <= 2
    assert abs(width - 595 * 300 / 72) <= 2


def test_encrypted_pdf_fails_with_a_clear_reason(tmp_path: Path, settings: Settings) -> None:
    path = tmp_path / "a.pdf"
    with pymupdf.open() as document:
        draw_invoice(document.new_page())
        document.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")

    with pytest.raises(EncryptedFileError, match="password"):
        list(iter_pdf_pages(path, settings))


def test_owner_only_encryption_can_still_be_read(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    path = tmp_path / "a.pdf"
    with pymupdf.open() as document:
        draw_invoice(document.new_page())
        document.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="")

    pages = list(iter_pdf_pages(path, settings))

    assert "INV-2026-0042" in pages[0].text


@pytest.mark.parametrize(
    "content",
    [b"%PDF-1.7\nthis is not really a pdf", b"%PDF-", b"%PDF-1.4\n" + b"\xff\xfe\x00" * 500],
)
def test_garbage_pdf_is_corrupted_or_empty(
    tmp_path: Path, settings: Settings, content: bytes
) -> None:
    path = tmp_path / "a.pdf"
    path.write_bytes(content)

    with pytest.raises((CorruptedFileError, EmptyFileError)):
        list(iter_pdf_pages(path, settings))


def test_truncated_pdf_never_crashes(
    tmp_path: Path, settings: Settings, ocr_calls: list[np.ndarray]
) -> None:
    full = make_digital_pdf(tmp_path / "full.pdf", pages=10).read_bytes()
    path = tmp_path / "a.pdf"
    path.write_bytes(full[: len(full) // 2])

    try:
        pages = list(iter_pdf_pages(path, settings))
    except (CorruptedFileError, EmptyFileError):
        return
    # MuPDF can repair a truncated file: whatever it recovers must be real, in-order content.
    assert 0 < len(pages) <= 10
    assert [page.page_number for page in pages] == list(range(1, len(pages) + 1))


def test_error_reason_does_not_leak_the_path(tmp_path: Path, settings: Settings) -> None:
    path = tmp_path / "secret-folder" / "a.pdf"
    path.parent.mkdir()
    path.write_bytes(b"%PDF-1.7\nnot a pdf")

    with pytest.raises((CorruptedFileError, EmptyFileError)) as caught:
        list(iter_pdf_pages(path, settings))

    assert "secret-folder" not in caught.value.reason


def test_nul_bytes_never_reach_the_text(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch, ocr_calls: list[np.ndarray]
) -> None:
    monkeypatch.setattr(
        pdf, "_page_text", lambda _page: "Invoice\x00 number INV-1 for 500.00 total"
    )

    pages = list(iter_pdf_pages(make_digital_pdf(tmp_path / "a.pdf"), settings))

    assert "\x00" not in pages[0].text
    assert "Invoice number INV-1" in pages[0].text


@pytest.mark.ocr
def test_scanned_pdf_end_to_end_with_real_ocr(tmp_path: Path, settings: Settings) -> None:
    pages = list(iter_pdf_pages(make_scanned_pdf(tmp_path / "a.pdf", pages=2), settings))

    assert len(pages) == 2
    for page in pages:
        assert page.source is TextSource.OCR
        assert page.ocr_confidence is not None
        assert page.ocr_confidence > 0.8
        assert all(token in page.text for token in INVOICE_TOKENS)


@pytest.mark.ocr
def test_mixed_pdf_end_to_end_with_real_ocr(tmp_path: Path, settings: Settings) -> None:
    path = tmp_path / "a.pdf"
    with pymupdf.open() as document:
        draw_invoice(document.new_page(width=595, height=842))
        insert_scan(document, render_invoice(200))
        document.new_page(width=595, height=842)
        document.save(path)

    pages = list(iter_pdf_pages(path, settings))

    assert pages[0].source is TextSource.TEXT_LAYER
    assert pages[1].source is TextSource.OCR
    assert "1,762.75" in pages[0].text
    assert "1,762.75" in pages[1].text
    assert pages[2].status is PageStatus.EMPTY
