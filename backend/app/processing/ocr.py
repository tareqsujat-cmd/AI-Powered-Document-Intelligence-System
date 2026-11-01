import logging
from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract
from cv2.typing import MatLike

from app.core.config import Settings
from app.processing.errors import OcrUnavailableError
from app.processing.text import clean_text

logger = logging.getLogger(__name__)

Image = MatLike

# Below this mean word confidence, the page is worth a second, more aggressive attempt.
RETRY_BELOW_CONFIDENCE = 0.75
_MIN_DIMENSION_FOR_OCR = 1200
_MAX_UPSCALE = 3.0
_DESKEW_WIDTH = 800
_DESKEW_MAX_DEGREES = 10.0
_BLANK_INK_RATIO = 0.0005
_RELIABLE_WORD_CONFIDENCE = 60.0
_NOISE_THRESHOLD = 12.0
_HEAVY_NOISE_THRESHOLD = 25.0
_BACKGROUND_WIDTH = 200
_BACKGROUND_KERNEL = 15
_UNEVEN_BACKGROUND_RANGE = 40.0
_TESSERACT_CONFIG = "--oem 1 --psm 6"


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    confidence: float | None
    word_count: int
    # Characters in words read with reasonable confidence: how much of the page was really read.
    reliable_chars: int = 0


def to_grayscale(image: Image) -> Image:
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_RGBA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def is_blank(gray: Image) -> bool:
    """True if a page has effectively no ink, so OCR would only return noise."""
    if gray.size == 0 or int(gray.max()) - int(gray.min()) < 30:
        return True
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Otsu always splits a page in two, so isolated specks must not count as ink.
    opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return bool(cv2.countNonZero(opened) < gray.size * _BLANK_INK_RATIO)


def upscale_if_small(gray: Image) -> Image:
    smaller = min(gray.shape[:2])
    if smaller >= _MIN_DIMENSION_FOR_OCR:
        return gray
    factor = min(_MIN_DIMENSION_FOR_OCR / smaller, _MAX_UPSCALE)
    return cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)


def estimate_skew(gray: Image) -> float:
    """Skew in degrees, by finding the rotation that gives the sharpest rows of text."""
    scale = min(_DESKEW_WIDTH / gray.shape[1], 1.0)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    _, binary = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    height, width = binary.shape
    center = (width / 2, height / 2)

    def sharpness(angle: float) -> float:
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(binary, matrix, (width, height), flags=cv2.INTER_NEAREST)
        return float(np.var(rotated.sum(axis=1, dtype=np.float64)))

    coarse = np.arange(-_DESKEW_MAX_DEGREES, _DESKEW_MAX_DEGREES + 0.5, 1.0)
    best = max(coarse, key=sharpness)
    fine = np.arange(best - 0.9, best + 0.95, 0.1)
    return float(max(fine, key=sharpness))


def rotate(gray: Image, degrees: float) -> Image:
    """Rotate about the center, growing the canvas and filling with the page's background."""
    height, width = gray.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), degrees, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_width, new_height = int(height * sin + width * cos), int(height * cos + width * sin)
    matrix[0, 2] += new_width / 2 - width / 2
    matrix[1, 2] += new_height / 2 - height / 2
    background = int(np.median(gray))
    return cv2.warpAffine(
        gray,
        matrix,
        (new_width, new_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=background,
    )


def deskew(gray: Image) -> Image:
    angle = estimate_skew(gray)
    return rotate(gray, angle) if abs(angle) >= 0.3 else gray


def estimate_noise(gray: Image) -> float:
    """Standard deviation of fine detail, sampled from a small crop for speed."""
    height, width = gray.shape
    crop = gray[height // 4 : height // 4 + 400, width // 4 : width // 4 + 400]
    return float(np.std(crop.astype(np.int16) - cv2.medianBlur(crop, 3).astype(np.int16)))


def denoise(gray: Image) -> Image:
    """Remove speckle and sensor noise. Text strokes survive because they are wider than noise."""
    noise = estimate_noise(gray)
    if noise < _NOISE_THRESHOLD:
        return gray
    denoised = cv2.medianBlur(gray, 3)
    if noise >= _HEAVY_NOISE_THRESHOLD:
        denoised = cv2.GaussianBlur(cv2.medianBlur(denoised, 5), (5, 5), 0)
    return denoised


def flatten_background(gray: Image) -> Image:
    """Even out shadows and gradients.

    Tesseract's global threshold splits a page with a shadow into one half it can read and one it
    cannot, and reports high confidence on what it did read, so this must happen before OCR.
    """
    height, width = gray.shape
    scale = _BACKGROUND_WIDTH / width
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    # Closing removes dark features narrower than the kernel, which leaves only the paper.
    kernel = np.ones((_BACKGROUND_KERNEL, _BACKGROUND_KERNEL), np.uint8)
    paper = cv2.GaussianBlur(cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel), (0, 0), 8)
    low, high = np.percentile(paper, (5, 95))
    if high - low < _UNEVEN_BACKGROUND_RANGE:
        return gray
    background = cv2.resize(paper, (width, height), interpolation=cv2.INTER_LINEAR)
    flattened = gray.astype(np.float32) / np.maximum(background.astype(np.float32), 1.0) * 255.0
    return np.clip(flattened, 0, 255).astype(np.uint8)


def binarize(gray: Image) -> Image:
    """Local threshold and contrast boost, for pages that still read poorly after cleanup."""
    boosted = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    block_size = max(31, (min(gray.shape[:2]) // 40) | 1)
    return cv2.adaptiveThreshold(
        boosted, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, 15
    )


def _run_tesseract(gray: Image, settings: Settings, dpi: int | None) -> OcrResult:
    config = _TESSERACT_CONFIG if dpi is None else f"{_TESSERACT_CONFIG} --dpi {dpi}"
    data = pytesseract.image_to_data(
        gray,
        lang=settings.ocr_lang,
        config=config,
        output_type=pytesseract.Output.DICT,
        timeout=settings.ocr_page_timeout_seconds,
    )

    lines: dict[tuple[int, int, int], list[str]] = {}
    weighted_confidence = 0.0
    total_chars = word_count = reliable_chars = 0
    for index, word in enumerate(data["text"]):
        word = word.strip()
        confidence = float(data["conf"][index])
        if not word or confidence < 0:
            continue
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        lines.setdefault(key, []).append(word)
        weighted_confidence += confidence * len(word)
        total_chars += len(word)
        word_count += 1
        if confidence >= _RELIABLE_WORD_CONFIDENCE:
            reliable_chars += len(word)

    text = clean_text("\n".join(" ".join(words) for words in lines.values()))
    confidence_score = weighted_confidence / total_chars / 100 if total_chars else None
    return OcrResult(text, confidence_score, word_count, reliable_chars)


def _orientation_correction(gray: Image) -> int:
    """Clockwise degrees to right a sideways or upside-down page; 0 if there is too little text."""
    try:
        osd = pytesseract.image_to_osd(gray, output_type=pytesseract.Output.DICT, timeout=30)
    except (pytesseract.TesseractError, RuntimeError):
        return 0
    return int(osd.get("rotate", 0))


def _is_confident(result: OcrResult) -> bool:
    return result.confidence is not None and result.confidence >= RETRY_BELOW_CONFIDENCE


def ocr_image(image: Image, settings: Settings, *, dpi: int | None = None) -> OcrResult:
    """OCR one page. Clean pages take one pass; only poor ones pay for the extra attempts."""
    try:
        gray = to_grayscale(image)
        if is_blank(gray):
            return OcrResult(text="", confidence=None, word_count=0)

        cleaned = denoise(flatten_background(upscale_if_small(gray)))
        prepared = deskew(cleaned)
        best = _run_tesseract(prepared, settings, dpi)
        if _is_confident(best):
            return best

        # Before deskew, which would mangle a page that is on its side.
        rotation = _orientation_correction(cleaned)
        if rotation:
            prepared = deskew(np.rot90(cleaned, k=-rotation // 90).copy())
            rotated = _run_tesseract(prepared, settings, dpi)
            if rotated.reliable_chars > best.reliable_chars:
                best = rotated
            if _is_confident(best):
                return best

        binarized = _run_tesseract(binarize(prepared), settings, dpi)
        return binarized if binarized.reliable_chars > best.reliable_chars else best
    except (pytesseract.TesseractNotFoundError, FileNotFoundError) as exc:
        raise OcrUnavailableError("The OCR engine is not available.") from exc
