import hashlib
import io
import zipfile
from pathlib import Path

import openpyxl
import pytest
import xlwt
from msoffcrypto.format.ooxml import OOXMLFile

from app.core.config import Settings
from app.db.enums import FileType
from app.processing import file_validation
from app.processing.errors import (
    CorruptedFileError,
    EmptyFileError,
    EncryptedFileError,
    FileTooLargeError,
    UnsupportedFileError,
)
from app.processing.file_validation import MIME_TYPES, sanitize_filename, validate_file
from tests.document_fixtures import encode_png, make_digital_pdf, render_invoice

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64
TIFF_LE = b"II*\x00\x08\x00\x00\x00" + b"\x00" * 64
TIFF_BE = b"MM\x00*\x00\x00\x00\x08" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / "upload.bin"
    path.write_bytes(content)
    return path


def make_xlsx(tmp_path: Path) -> Path:
    path = tmp_path / "book.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(["Description", "Amount"])
    workbook.save(path)
    return path


def make_xls(tmp_path: Path) -> Path:
    path = tmp_path / "book.xls"
    workbook = xlwt.Workbook()
    workbook.add_sheet("Sheet1").write(0, 0, "Description")
    workbook.save(str(path))
    return path


def make_zip(tmp_path: Path, files: dict[str, bytes]) -> Path:
    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("a.jpg", JPEG, FileType.JPEG),
        ("a.jpeg", JPEG, FileType.JPEG),
        ("a.JPG", JPEG, FileType.JPEG),
        ("a.png", PNG, FileType.PNG),
        ("a.tif", TIFF_LE, FileType.TIFF),
        ("a.tiff", TIFF_BE, FileType.TIFF),
        ("a.csv", b"invoice,total\nINV-1,10.00\n", FileType.CSV),
        ("a.csv", b"\xef\xbb\xbfinvoice,total\n", FileType.CSV),
        ("a.csv", "invoice,total\n".encode("utf-16"), FileType.CSV),
        ("a.csv", "Müller,€10\n".encode("cp1252"), FileType.CSV),
        ("a.pdf", b"%PDF-1.7\n%...", FileType.PDF),
        ("a.pdf", b"\n\n junk before header %PDF-1.4\n", FileType.PDF),
    ],
)
def test_detects_type_from_content(
    tmp_path: Path, settings: Settings, name: str, content: bytes, expected: FileType
) -> None:
    result = validate_file(write(tmp_path, name, content), name, settings)

    assert result.file_type is expected
    assert result.mime_type == MIME_TYPES[expected]


def test_detects_real_pdf_xlsx_and_xls(tmp_path: Path, settings: Settings) -> None:
    assert (
        validate_file(make_digital_pdf(tmp_path / "x.pdf"), "x.pdf", settings).file_type
        is FileType.PDF
    )
    assert validate_file(make_xlsx(tmp_path), "x.xlsx", settings).file_type is FileType.XLSX
    assert validate_file(make_xls(tmp_path), "x.xls", settings).file_type is FileType.XLS


def test_records_size_hash_and_safe_name(tmp_path: Path, settings: Settings) -> None:
    content = b"invoice,total\n"
    result = validate_file(write(tmp_path, "x", content), "../../evil name.csv", settings)

    assert result.size_bytes == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.safe_filename == "evil name.csv"


@pytest.mark.parametrize(
    "name", ["virus.exe", "script.sh", "page.html", "noextension", "archive.zip", "macro.xlsm"]
)
def test_rejects_unsupported_extension(tmp_path: Path, settings: Settings, name: str) -> None:
    with pytest.raises(UnsupportedFileError, match="Unsupported file extension"):
        validate_file(write(tmp_path, name, b"%PDF-1.7"), name, settings)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("invoice.pdf", b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64),  # Windows executable
        ("invoice.png", b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64),
        ("data.csv", b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64),
        ("data.csv", b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64),
        ("invoice.pdf", b"GIF89a" + b"\x00" * 64),
    ],
)
def test_rejects_disguised_binaries(
    tmp_path: Path, settings: Settings, name: str, content: bytes
) -> None:
    with pytest.raises(UnsupportedFileError, match="not a supported document type"):
        validate_file(write(tmp_path, name, content), name, settings)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("invoice.pdf", JPEG),
        ("invoice.png", b"%PDF-1.7"),
        ("invoice.csv", b"%PDF-1.7"),
        ("invoice.pdf", b"invoice,total\n"),
        ("photo.jpg", b"invoice,total\n"),
    ],
)
def test_rejects_content_from_a_different_family(
    tmp_path: Path, settings: Settings, name: str, content: bytes
) -> None:
    with pytest.raises(UnsupportedFileError, match="extension says"):
        validate_file(write(tmp_path, name, content), name, settings)


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("scan.jpg", PNG, FileType.PNG),
        ("scan.png", TIFF_LE, FileType.TIFF),
    ],
)
def test_wrong_extension_within_a_family_is_decided_by_content(
    tmp_path: Path, settings: Settings, name: str, content: bytes, expected: FileType
) -> None:
    assert validate_file(write(tmp_path, name, content), name, settings).file_type is expected


def test_excel_with_the_other_excel_extension_is_decided_by_content(
    tmp_path: Path, settings: Settings
) -> None:
    assert validate_file(make_xlsx(tmp_path), "old.xls", settings).file_type is FileType.XLSX
    assert validate_file(make_xls(tmp_path), "new.xlsx", settings).file_type is FileType.XLS


def test_real_png_passes(tmp_path: Path, settings: Settings) -> None:
    path = write(tmp_path, "a.png", encode_png(render_invoice(50)))

    assert validate_file(path, "a.png", settings).file_type is FileType.PNG


@pytest.mark.parametrize("content", [b"", b"   \n\t  \r\n"])
def test_empty_files_fail(tmp_path: Path, settings: Settings, content: bytes) -> None:
    with pytest.raises(EmptyFileError):
        validate_file(write(tmp_path, "a.csv", content), "a.csv", settings)


def test_enforces_size_limit(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, max_upload_mb=1)
    path = write(tmp_path, "a.csv", b"a,b\n" * (1024 * 1024))

    with pytest.raises(FileTooLargeError, match="1 MB"):
        validate_file(path, "a.csv", settings)


def test_size_limit_checked_before_content_is_read(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, max_upload_mb=1)
    path = write(tmp_path, "a.pdf", b"\x00" * (2 * 1024 * 1024))

    with pytest.raises(FileTooLargeError):
        validate_file(path, "a.pdf", settings)


def test_zip_that_is_not_a_workbook_is_rejected(tmp_path: Path, settings: Settings) -> None:
    path = make_zip(tmp_path, {"word/document.xml": b"<w/>", "[Content_Types].xml": b"<x/>"})

    with pytest.raises(UnsupportedFileError, match="not an Excel workbook"):
        validate_file(path, "fake.xlsx", settings)


def test_truncated_xlsx_is_corrupted(tmp_path: Path, settings: Settings) -> None:
    data = make_xlsx(tmp_path).read_bytes()
    path = write(tmp_path, "a.xlsx", data[: len(data) // 2])

    with pytest.raises(CorruptedFileError):
        validate_file(path, "a.xlsx", settings)


def test_zip_bomb_is_rejected_without_extracting(tmp_path: Path, settings: Settings) -> None:
    path = make_zip(tmp_path, {"xl/workbook.xml": b"<x/>", "xl/bomb.xml": b"\x00" * 50_000_000})

    with pytest.raises(UnsupportedFileError, match="unsafe size"):
        validate_file(path, "bomb.xlsx", settings)


def test_declared_zip_size_is_checked_against_absolute_limit(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_validation, "MAX_ZIP_UNCOMPRESSED_BYTES", 10)

    with pytest.raises(UnsupportedFileError, match="unsafe size"):
        validate_file(make_xlsx(tmp_path), "big.xlsx", settings)


def test_password_protected_xlsx_is_reported_as_encrypted(
    tmp_path: Path, settings: Settings
) -> None:
    encrypted = io.BytesIO()
    with make_xlsx(tmp_path).open("rb") as plain:
        OOXMLFile(plain).encrypt("secret", encrypted)
    path = write(tmp_path, "a.xlsx", encrypted.getvalue())

    with pytest.raises(EncryptedFileError, match="password"):
        validate_file(path, "a.xlsx", settings)


def test_damaged_ole2_container_is_corrupted(tmp_path: Path, settings: Settings) -> None:
    path = write(tmp_path, "a.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\xff" * 2048)

    with pytest.raises(CorruptedFileError):
        validate_file(path, "a.xls", settings)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("invoice.pdf", "invoice.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\windows\\system32\\evil.pdf", "evil.pdf"),
        ("C:\\Users\\me\\scan.tiff", "scan.tiff"),
        ("/absolute/path/file.csv", "file.csv"),
        ("in<vo>ice:|?.pdf", "in_vo_ice___.pdf"),
        ("a\x00b\r\nc.pdf", "a_b__c.pdf"),
        ("CON.pdf", "_CON.pdf"),
        ("lpt1.csv", "_lpt1.csv"),
        ("...", "file"),
        ("", "file"),
        (".pdf", "pdf"),
        ("file.name.with.dots.pdf", "file.name.with.dots.pdf"),
        ("請求書 2026.pdf", "請求書 2026.pdf"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_limits_length_and_keeps_extension() -> None:
    result = sanitize_filename("x" * 5000 + ".pdf")

    assert result.endswith(".pdf")
    assert len(result.encode()) <= 200


def test_sanitize_filename_does_not_split_multibyte_characters() -> None:
    result = sanitize_filename("請" * 500 + ".pdf")

    assert result.endswith(".pdf")
    assert len(result.encode()) <= 200
