import logging
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

from app.core.config import Settings
from app.db.enums import PageStatus, TextSource
from app.processing.errors import CorruptedFileError, FileTooLargeError, ProcessingError
from app.processing.ocr import Image, ocr_image
from app.processing.results import PageResult

logger = logging.getLogger(__name__)

# About an A0 sheet at 300 DPI. Checked before decoding, as a defense against decompression bombs.
MAX_IMAGE_PIXELS = 150_000_000
_DEFAULT_MIN_DPI = 70
PILImage.MAX_IMAGE_PIXELS = None  # replaced by the explicit check in _frame_to_array


def _frame_to_array(frame: PILImage.Image) -> Image:
    width, height = frame.size
    if width * height > MAX_IMAGE_PIXELS:
        raise FileTooLargeError(f"The image is too large to process ({width}x{height} pixels).")
    if frame.mode in ("RGBA", "LA", "PA") or "transparency" in frame.info:
        # Transparent regions read as black if the alpha channel is just dropped.
        rgba = frame.convert("RGBA")
        background = PILImage.new("RGBA", rgba.size, (255, 255, 255, 255))
        frame = PILImage.alpha_composite(background, rgba)
    elif frame.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        # 16-bit scans do not convert meaningfully to 8-bit without rescaling.
        data = np.asarray(frame, dtype=np.float64)
        low, high = float(data.min()), float(data.max())
        scale = 255.0 / (high - low) if high > low else 0.0
        return ((data - low) * scale).astype(np.uint8)
    return np.asarray(frame.convert("L"), dtype=np.uint8).copy()


def _frame_dpi(frame: PILImage.Image) -> int | None:
    dpi = frame.info.get("dpi")
    if not dpi:
        return None
    try:
        value = int(round(float(dpi[0])))
    except (TypeError, ValueError, IndexError):
        return None
    # Many tools write a placeholder such as 1 or 72, which would mislead Tesseract.
    return value if value >= _DEFAULT_MIN_DPI and value != 72 else None


def _process_frame(frame: PILImage.Image, number: int, settings: Settings) -> PageResult:
    started = time.perf_counter()
    result = PageResult(page_number=number, source=TextSource.OCR)
    try:
        array = _frame_to_array(ImageOps.exif_transpose(frame))
        ocr = ocr_image(array, settings, dpi=_frame_dpi(frame))
        result.text = ocr.text
        result.ocr_confidence = ocr.confidence
        if not result.text:
            result.status = PageStatus.EMPTY
    except ProcessingError:
        raise
    except Exception as exc:
        logger.warning("image frame failed", extra={"page": number}, exc_info=True)
        result.status = PageStatus.FAILED
        result.error = f"Image page {number} could not be read ({type(exc).__name__})."
    result.processing_ms = int((time.perf_counter() - started) * 1000)
    return result


def iter_image_pages(path: Path, settings: Settings) -> Iterator[PageResult]:
    """Yield one page per frame. TIFF frames are decoded one at a time."""
    try:
        image = PILImage.open(path)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise CorruptedFileError("The image is damaged and could not be opened.") from exc

    with image:
        number = 0
        while True:
            try:
                image.seek(number)
            except EOFError:
                break
            except (OSError, ValueError, SyntaxError) as exc:
                if number == 0:
                    raise CorruptedFileError("The image is damaged.") from exc
                yield PageResult(
                    page_number=number + 1,
                    source=TextSource.OCR,
                    status=PageStatus.FAILED,
                    error=f"Image page {number + 1} could not be read.",
                )
                break
            number += 1
            yield _process_frame(image, number, settings)
