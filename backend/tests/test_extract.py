from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from app.core.config import Settings
from app.db.enums import FileType, PageStatus, TextSource
from app.processing import extract
from app.processing.errors import (
    AllPagesFailedError,
    CorruptedFileError,
    EmptyFileError,
    EncryptedFileError,
)
from app.processing.extract import extract_pages
from app.processing.results import PageResult
from tests.document_fixtures import make_digital_pdf, render_invoice


def page(number: int, **kwargs: object) -> PageResult:
    return PageResult(page_number=number, **kwargs)


def patch_pages(monkeypatch: pytest.MonkeyPatch, pages: list[PageResult]) -> None:
    monkeypatch.setattr(extract, "_pages_for", lambda *_args: iter(pages))


def test_routes_pdf(tmp_path: Path, settings: Settings) -> None:
    pages = list(extract_pages(make_digital_pdf(tmp_path / "a.pdf"), FileType.PDF, settings))

    assert pages[0].source is TextSource.TEXT_LAYER


def test_routes_csv(tmp_path: Path, settings: Settings) -> None:
    path = tmp_path / "a.csv"
    path.write_text("Description,Amount\nWidget,10.00\n", encoding="utf-8")

    pages = list(extract_pages(path, FileType.CSV, settings))

    assert pages[0].source is TextSource.SPREADSHEET
    assert "Widget" in pages[0].text


@pytest.mark.ocr
@pytest.mark.parametrize(
    ("name", "file_type"),
    [("a.png", FileType.PNG), ("a.jpg", FileType.JPEG), ("a.tiff", FileType.TIFF)],
)
def test_routes_images(tmp_path: Path, settings: Settings, name: str, file_type: FileType) -> None:
    path = tmp_path / name
    Image.fromarray(render_invoice(200)).save(path)

    pages = list(extract_pages(path, file_type, settings))

    assert pages[0].source is TextSource.OCR
    assert "INV-2026-0042" in pages[0].text


def test_pages_are_yielded_as_they_arrive(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    produced: list[int] = []

    def lazy(*_args: object):
        for number in (1, 2, 3):
            produced.append(number)
            yield page(number, text="content")

    monkeypatch.setattr(extract, "_pages_for", lazy)
    pages = extract_pages(Path("x"), FileType.PDF, settings)

    next(pages)
    assert produced == [1]


def test_bad_page_among_good_ones_is_not_a_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_pages(
        monkeypatch,
        [page(1, text="good"), page(2, status=PageStatus.FAILED, error="bad"), page(3, text="ok")],
    )

    pages = list(extract_pages(Path("x"), FileType.PDF, settings))

    assert [item.status for item in pages] == [PageStatus.OK, PageStatus.FAILED, PageStatus.OK]


def test_blank_pages_among_good_ones_are_not_a_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_pages(monkeypatch, [page(1, status=PageStatus.EMPTY), page(2, text="good")])

    assert len(list(extract_pages(Path("x"), FileType.PDF, settings))) == 2


def test_all_blank_pages_fail_as_empty_after_yielding_them(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_pages(monkeypatch, [page(1, status=PageStatus.EMPTY), page(2, status=PageStatus.EMPTY)])
    yielded: list[PageResult] = []

    with pytest.raises(EmptyFileError, match="no readable text"):
        for item in extract_pages(Path("x"), FileType.PDF, settings):
            yielded.append(item)

    assert len(yielded) == 2


def test_all_failed_pages_raise_with_the_first_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_pages(
        monkeypatch,
        [
            page(1, status=PageStatus.FAILED, error="first problem"),
            page(2, status=PageStatus.FAILED, error="second problem"),
        ],
    )

    with pytest.raises(AllPagesFailedError, match="None of the 2 page.*first problem"):
        list(extract_pages(Path("x"), FileType.PDF, settings))


def test_failed_and_blank_pages_with_no_text_count_as_failed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_pages(
        monkeypatch,
        [page(1, status=PageStatus.EMPTY), page(2, status=PageStatus.FAILED, error="broken")],
    )

    with pytest.raises(AllPagesFailedError, match="broken"):
        list(extract_pages(Path("x"), FileType.PDF, settings))


def test_no_pages_at_all_is_empty(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_pages(monkeypatch, [])

    with pytest.raises(EmptyFileError):
        list(extract_pages(Path("x"), FileType.PDF, settings))


def test_blank_pdf_with_only_blank_pages_fails_as_empty(tmp_path: Path, settings: Settings) -> None:
    path = tmp_path / "a.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.save(path)

    with pytest.raises(EmptyFileError):
        list(extract_pages(path, FileType.PDF, settings))


def test_csv_with_only_blank_lines_fails_as_empty(tmp_path: Path, settings: Settings) -> None:
    path = tmp_path / "a.csv"
    path.write_text(",,,\n,,,\n", encoding="utf-8")

    with pytest.raises(EmptyFileError):
        list(extract_pages(path, FileType.CSV, settings))


def test_encrypted_and_corrupted_files_keep_their_specific_errors(
    tmp_path: Path, settings: Settings
) -> None:
    encrypted = tmp_path / "e.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), "secret")
        document.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    corrupted = tmp_path / "c.png"
    corrupted.write_bytes(b"\x89PNG\r\n\x1a\ngarbage")

    with pytest.raises(EncryptedFileError):
        list(extract_pages(encrypted, FileType.PDF, settings))
    with pytest.raises(CorruptedFileError):
        list(extract_pages(corrupted, FileType.PNG, settings))
