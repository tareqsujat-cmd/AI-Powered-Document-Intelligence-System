from typing import ClassVar


class ProcessingError(Exception):
    """A file could not be processed. `reason` is safe to show to users.

    Every failure can be retried by hand. `transient` means a retry may succeed without any change
    to the file, so background workers should retry it automatically.
    """

    code: ClassVar[str] = "processing_error"
    transient: ClassVar[bool] = False

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnsupportedFileError(ProcessingError):
    code = "unsupported_file"


class FileTooLargeError(ProcessingError):
    code = "file_too_large"


class EmptyFileError(ProcessingError):
    code = "empty_file"


class CorruptedFileError(ProcessingError):
    code = "corrupted_file"


class EncryptedFileError(ProcessingError):
    code = "encrypted_file"


class AllPagesFailedError(ProcessingError):
    code = "all_pages_failed"


class OcrUnavailableError(ProcessingError):
    """The OCR engine itself is broken, so failing page by page would only hide the real cause."""

    code = "ocr_unavailable"
    transient = True
