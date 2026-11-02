import io
from pathlib import Path

import pymupdf
from PIL import Image as PILImage
from PIL import ImageOps

from app.db.enums import FileType
from app.processing.images import MAX_IMAGE_PIXELS

PREVIEW_WIDTH = 1400


def _pdf_page(path: Path, page_number: int, width: int) -> bytes | None:
    with pymupdf.open(path, filetype="pdf") as document:
        if document.needs_pass or not 1 <= page_number <= document.page_count:
            return None
        page = document.load_page(page_number - 1)
        page_width = page.rect.width or 1
        scale = width / page_width
        # A very large page must not become a very large image.
        limit = (MAX_IMAGE_PIXELS / max(abs(page.rect), 1)) ** 0.5
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(min(scale, limit), min(scale, limit)))
        return bytes(pixmap.tobytes("png"))


def _image_page(path: Path, page_number: int, width: int) -> bytes | None:
    with PILImage.open(path) as image:
        if image.size[0] * image.size[1] > MAX_IMAGE_PIXELS:
            return None
        try:
            image.seek(page_number - 1)
        except EOFError:
            return None
        frame = ImageOps.exif_transpose(image).convert("RGB")
        frame.thumbnail((width, width * 4))
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG")
        return buffer.getvalue()


def render_page_png(
    path: Path, file_type: FileType, page_number: int, width: int = PREVIEW_WIDTH
) -> bytes | None:
    """A page as a PNG, the one format every browser can show. None if it has no picture to show.

    Browsers cannot display TIFF at all, and a served PDF cannot safely be embedded, so previews
    are always rendered here. Spreadsheets have no page image.
    """
    try:
        if file_type is FileType.PDF:
            return _pdf_page(path, page_number, width)
        if file_type in (FileType.JPEG, FileType.PNG, FileType.TIFF):
            return _image_page(path, page_number, width)
    except Exception:
        # A damaged file has no preview. The document's own status explains what went wrong.
        return None
    return None
