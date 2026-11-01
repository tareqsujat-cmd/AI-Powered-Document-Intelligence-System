import hashlib
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import olefile

from app.core.config import Settings
from app.db.enums import FileType
from app.processing.errors import (
    CorruptedFileError,
    EmptyFileError,
    EncryptedFileError,
    FileTooLargeError,
    ProcessingError,
    UnsupportedFileError,
)

_HEAD_BYTES = 8192
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_FILENAME_BYTES = 200

# Zip bomb limits. Real workbooks compress roughly 10:1 and rarely reach 50:1.
MAX_ZIP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_RATIO = 200

_EXTENSION_TYPES = {
    ".pdf": FileType.PDF,
    ".jpg": FileType.JPEG,
    ".jpeg": FileType.JPEG,
    ".png": FileType.PNG,
    ".tif": FileType.TIFF,
    ".tiff": FileType.TIFF,
    ".xlsx": FileType.XLSX,
    ".xls": FileType.XLS,
    ".csv": FileType.CSV,
}

MIME_TYPES = {
    FileType.PDF: "application/pdf",
    FileType.JPEG: "image/jpeg",
    FileType.PNG: "image/png",
    FileType.TIFF: "image/tiff",
    FileType.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    FileType.XLS: "application/vnd.ms-excel",
    FileType.CSV: "text/csv",
}

# A wrong extension within a family is a harmless, common mistake, and the content decides how the
# file is parsed. Across families, it may be an attempt to disguise the file.
_FAMILIES = (
    {FileType.PDF},
    {FileType.JPEG, FileType.PNG, FileType.TIFF},
    {FileType.XLSX, FileType.XLS},
    {FileType.CSV},
)

_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class ValidatedFile:
    file_type: FileType
    mime_type: str
    sha256: str
    size_bytes: int
    safe_filename: str


def sanitize_filename(filename: str) -> str:
    """Reduce a client-supplied name to one safe to display and to use in a header."""
    name = unicodedata.normalize("NFC", filename).replace("\\", "/")
    name = PurePosixPath(name).name
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).strip(" .")

    stem, suffix = (name.rsplit(".", 1) + [""])[:2] if "." in name else (name, "")
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    if not stem:
        stem = "file"

    suffix_part = f".{suffix}" if suffix else ""
    available = _MAX_FILENAME_BYTES - len(suffix_part.encode())
    stem = stem.encode()[: max(available, 1)].decode(errors="ignore").rstrip(" .") or "file"
    return f"{stem}{suffix_part}"


def _sniff_zip(path: Path) -> FileType:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise CorruptedFileError("The file is a damaged ZIP or Excel archive.") from exc

    names = {info.filename for info in infos}
    if "xl/workbook.xml" not in names:
        raise UnsupportedFileError("This ZIP archive is not an Excel workbook.")

    uncompressed = sum(info.file_size for info in infos)
    compressed = sum(info.compress_size for info in infos)
    if uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES or uncompressed > compressed * MAX_ZIP_RATIO:
        raise UnsupportedFileError("This workbook expands to an unsafe size when decompressed.")
    return FileType.XLSX


def _sniff_ole2(path: Path) -> FileType:
    try:
        with olefile.OleFileIO(str(path)) as container:
            if container.exists("EncryptedPackage"):
                raise EncryptedFileError("This Excel workbook is password protected.")
            if container.exists("Workbook") or container.exists("Book"):
                return FileType.XLS
    except OSError as exc:
        raise CorruptedFileError("The file is a damaged Excel workbook.") from exc
    raise UnsupportedFileError("This Office file is not an Excel workbook.")


def _looks_like_text(head: bytes) -> bool:
    if head.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return True
    if b"\x00" in head:
        return False
    control = sum(1 for byte in head if byte < 32 and byte not in (9, 10, 12, 13))
    return control <= len(head) * 0.01


def sniff_file_type(path: Path, head: bytes) -> FileType | None:
    """Identify a file from its content. Returns None if it is not a supported type."""
    if head.startswith(b"\xff\xd8\xff"):
        return FileType.JPEG
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileType.PNG
    if head.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return FileType.TIFF
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip(path)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _sniff_ole2(path)
    # The PDF specification lets readers accept a header within the first 1024 bytes.
    if b"%PDF-" in head[:1024]:
        return FileType.PDF
    if _looks_like_text(head):
        return FileType.CSV
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def validate_file(path: Path, original_filename: str, settings: Settings) -> ValidatedFile:
    """Decide from file content whether a file may enter the pipeline, or raise ProcessingError."""
    safe_filename = sanitize_filename(original_filename)
    declared = _EXTENSION_TYPES.get(PurePosixPath(safe_filename).suffix.lower())
    if declared is None:
        allowed = ", ".join(sorted(ext.lstrip(".").upper() for ext in _EXTENSION_TYPES))
        raise UnsupportedFileError(f"Unsupported file extension. Allowed types: {allowed}.")

    size_bytes = path.stat().st_size
    if size_bytes == 0:
        raise EmptyFileError("The file is empty.")
    if size_bytes > settings.max_upload_bytes:
        raise FileTooLargeError(f"The file exceeds the {settings.max_upload_mb} MB upload limit.")

    with path.open("rb") as stream:
        head = stream.read(_HEAD_BYTES)
    if not head.strip():
        raise EmptyFileError("The file contains only whitespace.")

    try:
        detected = sniff_file_type(path, head)
    except ProcessingError:
        raise
    except Exception as exc:
        raise CorruptedFileError("The file could not be read.") from exc
    if detected is None:
        raise UnsupportedFileError("The file content is not a supported document type.")

    if not any(declared in family and detected in family for family in _FAMILIES):
        raise UnsupportedFileError(
            f"The file extension says {declared.value.upper()}, "
            f"but the content is {detected.value.upper()}."
        )

    return ValidatedFile(
        file_type=detected,
        mime_type=MIME_TYPES[detected],
        sha256=_sha256(path),
        size_bytes=size_bytes,
        safe_filename=safe_filename,
    )
