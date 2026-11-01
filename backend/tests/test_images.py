import io
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from app.core.config import Settings
from app.db.enums import PageStatus, TextSource
from app.processing import images
from app.processing.errors import CorruptedFileError, FileTooLargeError, OcrUnavailableError
from app.processing.images import iter_image_pages
from app.processing.ocr import OcrResult
from tests.document_fixtures import INVOICE_TOKENS, render_invoice

FAKE_OCR = OcrResult(text="read", confidence=0.9, word_count=1, reliable_chars=4)


@pytest.fixture
def ocr_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[np.ndarray, int | None]]:
    calls: list[tuple[np.ndarray, int | None]] = []

    def fake(image: np.ndarray, _settings: Settings, *, dpi: int | None = None) -> OcrResult:
        calls.append((image, dpi))
        return FAKE_OCR

    monkeypatch.setattr(images, "ocr_image", fake)
    return calls


@pytest.fixture(scope="module")
def invoice() -> Image.Image:
    return Image.fromarray(render_invoice(150))


def save(image: Image.Image, path: Path, **kwargs: object) -> Path:
    image.save(path, **kwargs)
    return path


@pytest.mark.parametrize("name", ["a.png", "a.jpg", "a.tiff"])
def test_single_image_formats_yield_one_page(
    tmp_path: Path, settings: Settings, invoice: Image.Image, ocr_calls: list, name: str
) -> None:
    pages = list(iter_image_pages(save(invoice, tmp_path / name), settings))

    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert pages[0].source is TextSource.OCR
    assert pages[0].text == "read"
    assert pages[0].ocr_confidence == 0.9
    assert pages[0].processing_ms >= 0


def test_multi_page_tiff_yields_every_frame_in_order(
    tmp_path: Path, settings: Settings, ocr_calls: list
) -> None:
    frames = [Image.new("L", (300, 300), value) for value in (10, 120, 200)]
    path = save(frames[0], tmp_path / "a.tiff", save_all=True, append_images=frames[1:])

    pages = list(iter_image_pages(path, settings))

    assert [page.page_number for page in pages] == [1, 2, 3]
    assert [int(call[0][0, 0]) for call in ocr_calls] == [10, 120, 200]


def test_tiff_frames_are_read_lazily(tmp_path: Path, settings: Settings, ocr_calls: list) -> None:
    frames = [Image.new("L", (100, 100)) for _ in range(4)]
    path = save(frames[0], tmp_path / "a.tiff", save_all=True, append_images=frames[1:])

    pages = iter_image_pages(path, settings)
    next(pages)

    assert len(ocr_calls) == 1
    pages.close()


def test_transparent_regions_become_white_not_black(
    tmp_path: Path, settings: Settings, ocr_calls: list
) -> None:
    rgba = np.zeros((100, 100, 4), dtype=np.uint8)  # fully transparent black
    rgba[40:60, 40:60] = (0, 0, 0, 255)  # opaque black square
    path = save(Image.fromarray(rgba, "RGBA"), tmp_path / "a.png")

    list(iter_image_pages(path, settings))

    array = ocr_calls[0][0]
    assert array[0, 0] == 255
    assert array[50, 50] == 0


def test_palette_png_with_transparency_is_handled(
    tmp_path: Path, settings: Settings, ocr_calls: list
) -> None:
    palette = Image.new("P", (50, 50), 0)
    palette.putpalette([0, 0, 0, 255, 255, 255])
    path = save(palette, tmp_path / "a.png", transparency=0)

    list(iter_image_pages(path, settings))

    assert ocr_calls[0][0][0, 0] == 255


def test_cmyk_jpeg_is_converted(tmp_path: Path, settings: Settings, ocr_calls: list) -> None:
    path = save(Image.new("CMYK", (50, 50), (0, 0, 0, 0)), tmp_path / "a.jpg")

    pages = list(iter_image_pages(path, settings))

    assert pages[0].status is PageStatus.OK
    assert ocr_calls[0][0].ndim == 2


def test_one_bit_tiff_is_converted(tmp_path: Path, settings: Settings, ocr_calls: list) -> None:
    path = save(Image.new("1", (50, 50), 1), tmp_path / "a.tiff", compression="group4")

    list(iter_image_pages(path, settings))

    assert ocr_calls[0][0][0, 0] == 255


def test_sixteen_bit_tiff_keeps_its_contrast(
    tmp_path: Path, settings: Settings, ocr_calls: list
) -> None:
    data = np.full((60, 60), 60000, dtype=np.uint16)
    data[20:40, 20:40] = 1000
    path = tmp_path / "a.tiff"
    assert cv2.imwrite(str(path), data)

    list(iter_image_pages(path, settings))

    array = ocr_calls[0][0]
    assert array.dtype == np.uint8
    assert array[0, 0] == 255
    assert array[30, 30] == 0


def test_exif_orientation_is_applied(tmp_path: Path, settings: Settings, ocr_calls: list) -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # stored rotated, display 90 degrees clockwise
    path = save(Image.new("L", (200, 100), 255), tmp_path / "a.jpg", exif=exif)

    list(iter_image_pages(path, settings))

    assert ocr_calls[0][0].shape == (200, 100)


@pytest.mark.parametrize(
    ("dpi", "expected"),
    [((300, 300), 300), ((600, 600), 600), ((72, 72), None), ((1, 1), None)],
)
def test_only_believable_dpi_values_reach_tesseract(
    tmp_path: Path, settings: Settings, ocr_calls: list, dpi: tuple[int, int], expected: int | None
) -> None:
    path = save(Image.new("L", (100, 100), 255), tmp_path / "a.png", dpi=dpi)

    list(iter_image_pages(path, settings))

    assert ocr_calls[0][1] == expected


def test_missing_dpi_is_passed_as_unknown(
    tmp_path: Path, settings: Settings, ocr_calls: list
) -> None:
    list(iter_image_pages(save(Image.new("L", (10, 10)), tmp_path / "a.png"), settings))

    assert ocr_calls[0][1] is None


def test_blank_result_is_marked_empty(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        images, "ocr_image", lambda *_a, **_k: OcrResult(text="", confidence=None, word_count=0)
    )
    path = save(Image.new("L", (100, 100), 255), tmp_path / "a.png")

    assert next(iter_image_pages(path, settings)).status is PageStatus.EMPTY


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("a.png", b"\x89PNG\r\n\x1a\n" + b"garbage" * 50),
        ("a.jpg", b"\xff\xd8\xff" + b"garbage" * 50),
        ("a.tiff", b"II*\x00" + b"garbage" * 50),
    ],
)
def test_corrupted_image_fails_cleanly(
    tmp_path: Path, settings: Settings, ocr_calls: list, name: str, content: bytes
) -> None:
    path = tmp_path / name
    path.write_bytes(content)

    with pytest.raises(CorruptedFileError) as caught:
        list(iter_image_pages(path, settings))

    assert str(tmp_path) not in caught.value.reason
    assert ocr_calls == []


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_truncated_image_is_a_failed_page_not_a_crash(
    tmp_path: Path, settings: Settings, ocr_calls: list, invoice: Image.Image, fmt: str
) -> None:
    buffer = io.BytesIO()
    invoice.save(buffer, format=fmt)
    path = tmp_path / f"a.{fmt.lower()}"
    path.write_bytes(buffer.getvalue()[: buffer.tell() // 3])

    pages = list(iter_image_pages(path, settings))

    assert pages[0].status is PageStatus.FAILED
    assert "page 1" in pages[0].error
    assert ocr_calls == []


def test_tiff_with_damaged_second_frame_keeps_the_first(
    tmp_path: Path, settings: Settings, ocr_calls: list
) -> None:
    frames = [Image.new("L", (400, 400), value) for value in (50, 150)]
    path = save(frames[0], tmp_path / "a.tiff", save_all=True, append_images=frames[1:])
    data = bytearray(path.read_bytes())
    # Frame 2's strip data sits after frame 1's: wipe out the rest of the file past frame 1.
    damaged = bytes(data[: 400 * 400 + 200])
    path.write_bytes(damaged)

    pages = list(iter_image_pages(path, settings))

    assert pages[0].status is PageStatus.OK
    assert all(page.status is PageStatus.FAILED for page in pages[1:])


def test_decompression_bomb_is_rejected_before_decoding(
    tmp_path: Path, settings: Settings, ocr_calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 100)
    path = save(Image.new("L", (11, 11)), tmp_path / "a.png")

    with pytest.raises(FileTooLargeError, match="11x11"):
        list(iter_image_pages(path, settings))

    assert ocr_calls == []


def test_ocr_engine_failure_stops_the_document(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_a: object, **_k: object) -> None:
        raise OcrUnavailableError("The OCR engine is not available.")

    monkeypatch.setattr(images, "ocr_image", unavailable)
    path = save(Image.new("L", (10, 10)), tmp_path / "a.png")

    with pytest.raises(OcrUnavailableError):
        list(iter_image_pages(path, settings))


@pytest.mark.ocr
@pytest.mark.parametrize("name", ["a.png", "a.jpg", "a.tiff"])
def test_real_ocr_reads_each_format(tmp_path: Path, settings: Settings, name: str) -> None:
    path = save(Image.fromarray(render_invoice(300)), tmp_path / name)

    page = next(iter_image_pages(path, settings))

    assert all(token in page.text for token in INVOICE_TOKENS)


@pytest.mark.ocr
def test_real_ocr_reads_each_page_of_a_tiff(tmp_path: Path, settings: Settings) -> None:
    page_image = Image.fromarray(render_invoice(200))
    blank = Image.new("L", page_image.size, 255)
    path = save(page_image, tmp_path / "a.tiff", save_all=True, append_images=[blank, page_image])

    pages = list(iter_image_pages(path, settings))

    assert [page.status for page in pages] == [PageStatus.OK, PageStatus.EMPTY, PageStatus.OK]
    assert "1,762.75" in pages[0].text
    assert "1,762.75" in pages[2].text
