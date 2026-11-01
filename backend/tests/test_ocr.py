import numpy as np
import pytesseract
import pytest

from app.core.config import Settings
from app.processing import ocr
from app.processing.errors import OcrUnavailableError
from tests.document_fixtures import (
    DEGRADATIONS,
    INVOICE_TOKENS,
    add_noise,
    add_salt_and_pepper,
    render_invoice,
    rotate_image,
    uneven_lighting,
)


@pytest.fixture(scope="module")
def invoice() -> np.ndarray:
    return render_invoice(300)


def recall(text: str) -> float:
    return sum(token in text for token in INVOICE_TOKENS) / len(INVOICE_TOKENS)


def test_to_grayscale_handles_every_channel_layout() -> None:
    gray = np.full((4, 4), 200, dtype=np.uint8)
    rgb = np.dstack([gray, gray, gray])
    rgba = np.dstack([gray, gray, gray, np.full((4, 4), 255, dtype=np.uint8)])

    assert ocr.to_grayscale(gray).shape == (4, 4)
    assert ocr.to_grayscale(rgb).shape == (4, 4)
    assert ocr.to_grayscale(rgba).shape == (4, 4)
    assert ocr.to_grayscale(rgb)[0, 0] == 200


@pytest.mark.parametrize("value", [0, 128, 255])
def test_uniform_page_is_blank(value: int) -> None:
    assert ocr.is_blank(np.full((400, 400), value, dtype=np.uint8))


def test_page_with_only_faint_specks_is_blank() -> None:
    page = np.full((1000, 1000), 255, dtype=np.uint8)
    page[::97, ::89] = 0

    assert ocr.is_blank(page)


def test_invoice_is_not_blank(invoice: np.ndarray) -> None:
    assert not ocr.is_blank(invoice)


def test_small_images_are_upscaled_and_large_ones_left_alone() -> None:
    small = np.zeros((300, 400), dtype=np.uint8)
    large = np.zeros((2000, 1500), dtype=np.uint8)

    assert ocr.upscale_if_small(small).shape == (900, 1200)
    assert ocr.upscale_if_small(large) is large


@pytest.mark.parametrize("angle", [-8.0, -3.0, 0.0, 1.5, 6.0])
def test_estimate_skew_recovers_known_rotation(invoice: np.ndarray, angle: float) -> None:
    measured = ocr.estimate_skew(rotate_image(invoice, angle))

    assert measured == pytest.approx(-angle, abs=0.3)


def test_deskew_leaves_a_straight_page_untouched(invoice: np.ndarray) -> None:
    assert ocr.deskew(invoice) is invoice


def test_rotate_grows_canvas_and_fills_with_page_background(invoice: np.ndarray) -> None:
    rotated = ocr.rotate(invoice, 10)

    assert rotated.shape[0] > invoice.shape[0]
    assert rotated[0, 0] == 255


def test_clean_page_is_not_denoised(invoice: np.ndarray) -> None:
    assert ocr.estimate_noise(invoice) < ocr._NOISE_THRESHOLD
    assert ocr.denoise(invoice) is invoice


@pytest.mark.parametrize(
    "degrade",
    [lambda page: add_noise(page, 25), lambda page: add_salt_and_pepper(page, 0.02)],
)
def test_noisy_page_is_denoised(invoice: np.ndarray, degrade) -> None:
    noisy = degrade(invoice)

    assert ocr.estimate_noise(noisy) >= ocr._NOISE_THRESHOLD
    assert ocr.estimate_noise(ocr.denoise(noisy)) < ocr.estimate_noise(noisy) / 2


def test_even_page_background_is_left_alone(invoice: np.ndarray) -> None:
    assert ocr.flatten_background(invoice) is invoice


def test_shadow_is_removed(invoice: np.ndarray) -> None:
    shadowed = uneven_lighting(invoice)
    flattened = ocr.flatten_background(shadowed)

    # Paper in the shadowed left margin and in the lit right margin now nearly match.
    assert int(shadowed[100, -20]) - int(shadowed[100, 20]) > 150
    assert abs(int(flattened[100, 20]) - int(flattened[100, -20])) < 30


def test_binarize_returns_only_black_and_white(invoice: np.ndarray) -> None:
    assert set(np.unique(ocr.binarize(invoice))) <= {0, 255}


def test_missing_tesseract_raises_a_transient_error(
    invoice: np.ndarray, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", "/nonexistent/tesseract")

    with pytest.raises(OcrUnavailableError) as caught:
        ocr.ocr_image(invoice, settings)

    assert caught.value.transient is True


def test_blank_page_skips_tesseract(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Tesseract should not run on a blank page")

    monkeypatch.setattr(pytesseract, "image_to_data", fail)

    result = ocr.ocr_image(np.full((500, 500), 255, dtype=np.uint8), settings)

    assert result == ocr.OcrResult(text="", confidence=None, word_count=0)


@pytest.mark.ocr
@pytest.mark.parametrize("name", DEGRADATIONS)
def test_reads_every_value_from_degraded_scans(
    invoice: np.ndarray, settings: Settings, name: str
) -> None:
    result = ocr.ocr_image(DEGRADATIONS[name](invoice), settings)

    assert recall(result.text) == 1.0, result.text
    assert result.confidence is not None
    assert result.confidence > 0.8


@pytest.mark.ocr
def test_keeps_table_rows_on_one_line(invoice: np.ndarray, settings: Settings) -> None:
    text = ocr.ocr_image(invoice, settings).text

    assert "Industrial Bearing Kit 4 312.50 1,250.00" in text.splitlines()


@pytest.mark.ocr
def test_clean_page_needs_only_one_tesseract_pass(
    invoice: np.ndarray, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    real = pytesseract.image_to_data

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(pytesseract, "image_to_data", counting)

    ocr.ocr_image(invoice, settings)

    assert calls == 1


@pytest.mark.ocr
def test_pure_noise_gives_low_confidence_rather_than_invented_text(settings: Settings) -> None:
    noise = np.random.default_rng(1).integers(0, 256, (800, 800), dtype=np.uint8)

    result = ocr.ocr_image(noise, settings)

    assert recall(result.text) == 0.0
    assert result.reliable_chars < 50
    assert result.confidence is None or result.confidence < ocr.RETRY_BELOW_CONFIDENCE


@pytest.mark.ocr
def test_timeout_is_passed_to_tesseract(
    invoice: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    real = pytesseract.image_to_data

    def spy(*args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pytesseract, "image_to_data", spy)

    ocr.ocr_image(invoice, Settings(_env_file=None, ocr_page_timeout_seconds=77))

    assert seen["timeout"] == 77
