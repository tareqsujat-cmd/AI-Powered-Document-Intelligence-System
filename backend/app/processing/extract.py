from collections.abc import Iterator
from pathlib import Path

from app.core.config import Settings
from app.db.enums import FileType, PageStatus
from app.processing.errors import AllPagesFailedError, EmptyFileError
from app.processing.images import iter_image_pages
from app.processing.pdf import iter_pdf_pages
from app.processing.results import PageResult
from app.processing.spreadsheets import iter_spreadsheet_pages


def _pages_for(path: Path, file_type: FileType, settings: Settings) -> Iterator[PageResult]:
    if file_type is FileType.PDF:
        return iter_pdf_pages(path, settings)
    if file_type in (FileType.JPEG, FileType.PNG, FileType.TIFF):
        return iter_image_pages(path, settings)
    return iter_spreadsheet_pages(path, file_type, settings)


def extract_pages(path: Path, file_type: FileType, settings: Settings) -> Iterator[PageResult]:
    """Yield a file's pages as they are read, so the caller can store them and report progress.

    Raises ProcessingError, after yielding whatever it could, if the file as a whole has no usable
    content. One bad page among good ones is not a failure: it is yielded with status=failed.
    """
    total = failed = with_text = 0
    first_error: str | None = None
    for page in _pages_for(path, file_type, settings):
        total += 1
        if page.status is PageStatus.FAILED:
            failed += 1
            first_error = first_error or page.error
        elif page.text:
            with_text += 1
        yield page

    if with_text:
        return
    if failed:
        raise AllPagesFailedError(
            f"None of the {total} page(s) could be read. First error: {first_error}"
        )
    raise EmptyFileError("The file contains no readable text.")
