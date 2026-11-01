from collections.abc import Iterator
from pathlib import Path

import pymupdf
from PIL import Image as PILImage

from app.core.config import Settings
from app.db.enums import FileType, PageStatus
from app.processing.errors import AllPagesFailedError, EmptyFileError
from app.processing.images import iter_image_pages
from app.processing.pdf import iter_pdf_pages
from app.processing.results import PageResult
from app.processing.spreadsheets import iter_spreadsheet_pages


def _pages_for(
    path: Path, file_type: FileType, settings: Settings, start_page: int
) -> Iterator[PageResult]:
    if file_type is FileType.PDF:
        return iter_pdf_pages(path, settings, start_page=start_page)
    if file_type in (FileType.JPEG, FileType.PNG, FileType.TIFF):
        return iter_image_pages(path, settings, start_page=start_page)
    # Sheets cannot be skipped to, but reading them is quick compared with OCR.
    return (
        page
        for page in iter_spreadsheet_pages(path, file_type, settings)
        if page.page_number >= start_page
    )


def count_pages(path: Path, file_type: FileType) -> int | None:
    """How many pages a file has, for showing progress, if that is known without reading it all."""
    try:
        if file_type is FileType.PDF:
            with pymupdf.open(path, filetype="pdf") as document:
                return int(document.page_count) if not document.needs_pass else None
        if file_type in (FileType.JPEG, FileType.PNG, FileType.TIFF):
            with PILImage.open(path) as image:
                return int(getattr(image, "n_frames", 1))
    except Exception:
        # The readers report damaged files properly. This count is only a progress hint.
        return None
    return None


def extract_pages(
    path: Path,
    file_type: FileType,
    settings: Settings,
    *,
    start_page: int = 1,
    has_earlier_text: bool = False,
) -> Iterator[PageResult]:
    """Yield a file's pages as they are read, so the caller can store them and report progress.

    Raises ProcessingError, after yielding whatever it could, if the file as a whole has no usable
    content. One bad page among good ones is not a failure: it is yielded with status=failed.

    To resume an interrupted run, start_page skips what is stored already, and has_earlier_text says
    whether those pages held any text, as they count towards the file having content.
    """
    total = failed = with_text = 0
    first_error: str | None = None
    for page in _pages_for(path, file_type, settings, start_page):
        total += 1
        if page.status is PageStatus.FAILED:
            failed += 1
            first_error = first_error or page.error
        elif page.text:
            with_text += 1
        yield page

    if with_text or has_earlier_text:
        return
    if failed:
        raise AllPagesFailedError(
            f"None of the {total} page(s) could be read. First error: {first_error}"
        )
    raise EmptyFileError("The file contains no readable text.")
