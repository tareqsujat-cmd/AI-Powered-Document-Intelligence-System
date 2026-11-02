import io
from pathlib import Path

import pymupdf
import pytest
from PIL import Image
from sqlalchemy.orm import Session

from app.db.enums import FileType
from app.processing.images import MAX_IMAGE_PIXELS
from app.processing.preview import PREVIEW_WIDTH, render_page_png
from tests.api_helpers import API, Api, make_document

pytest_plugins = ["tests.api_helpers"]


def make_pdf(path: Path, pages: int = 2, width: int = 595, height: int = 842) -> Path:
    with pymupdf.open() as document:
        for number in range(pages):
            document.new_page(width=width, height=height).insert_text(
                (50, 60), f"Page {number + 1}"
            )
        document.save(path)
    return path


def make_tiff(path: Path, frames: int = 3) -> Path:
    images = [Image.new("L", (300, 400), 40 * (index + 1)) for index in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:])
    return path


def size_of(png: bytes | None) -> tuple[int, int]:
    assert png is not None
    assert png.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(png)) as image:
        return image.size


class TestRendering:
    def test_a_pdf_page_becomes_a_png_at_preview_width(self, tmp_path: Path) -> None:
        png = render_page_png(make_pdf(tmp_path / "a.pdf"), FileType.PDF, 1)

        assert size_of(png)[0] == pytest.approx(PREVIEW_WIDTH, abs=2)

    def test_each_pdf_page_is_its_own_image(self, tmp_path: Path) -> None:
        path = make_pdf(tmp_path / "a.pdf")

        assert render_page_png(path, FileType.PDF, 1) != render_page_png(path, FileType.PDF, 2)

    @pytest.mark.parametrize("page", [0, 3, 999])
    def test_a_pdf_page_that_does_not_exist_has_no_image(self, tmp_path: Path, page: int) -> None:
        assert render_page_png(make_pdf(tmp_path / "a.pdf"), FileType.PDF, page) is None

    def test_a_password_protected_pdf_has_no_image(self, tmp_path: Path) -> None:
        path = tmp_path / "a.pdf"
        with pymupdf.open() as document:
            document.new_page()
            document.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")

        assert render_page_png(path, FileType.PDF, 1) is None

    def test_a_damaged_pdf_has_no_image_and_does_not_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "a.pdf"
        path.write_bytes(b"%PDF-1.7\nnot a pdf")

        assert render_page_png(path, FileType.PDF, 1) is None

    def test_an_enormous_pdf_page_stays_within_the_pixel_limit(self, tmp_path: Path) -> None:
        path = make_pdf(tmp_path / "a.pdf", pages=1, width=14000, height=14000)

        width, height = size_of(render_page_png(path, FileType.PDF, 1))

        assert width * height <= MAX_IMAGE_PIXELS

    def test_each_tiff_frame_is_available(self, tmp_path: Path) -> None:
        path = make_tiff(tmp_path / "a.tiff")

        images = [render_page_png(path, FileType.TIFF, number) for number in (1, 2, 3)]

        assert len(set(images)) == 3
        assert render_page_png(path, FileType.TIFF, 4) is None

    def test_a_small_image_is_not_blown_up(self, tmp_path: Path) -> None:
        path = tmp_path / "a.png"
        Image.new("L", (300, 400), 255).save(path)

        assert size_of(render_page_png(path, FileType.PNG, 1)) == (300, 400)

    def test_a_large_image_is_scaled_down(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jpg"
        Image.new("RGB", (3000, 2000), "white").save(path)

        assert size_of(render_page_png(path, FileType.JPEG, 1))[0] == PREVIEW_WIDTH

    def test_a_cmyk_jpeg_renders(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jpg"
        Image.new("CMYK", (100, 100), (0, 0, 0, 0)).save(path)

        assert size_of(render_page_png(path, FileType.JPEG, 1)) == (100, 100)

    def test_exif_rotation_is_applied(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jpg"
        exif = Image.Exif()
        exif[0x0112] = 6
        Image.new("RGB", (200, 100), "white").save(path, exif=exif)

        assert size_of(render_page_png(path, FileType.JPEG, 1)) == (100, 200)

    def test_a_damaged_image_has_no_image(self, tmp_path: Path) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\ngarbage")

        assert render_page_png(path, FileType.PNG, 1) is None

    @pytest.mark.parametrize("file_type", [FileType.XLSX, FileType.XLS, FileType.CSV])
    def test_spreadsheets_have_no_page_image(self, tmp_path: Path, file_type: FileType) -> None:
        path = tmp_path / "a.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")

        assert render_page_png(path, file_type, 1) is None


@pytest.mark.db
class TestRoute:
    def stored(self, api: Api, db: Session, name: str, file_type: FileType, make) -> str:
        api.settings.storage_dir.mkdir(parents=True, exist_ok=True)
        path = make(api.settings.storage_dir / name)
        document = make_document(db, storage_path=str(path), file_type=file_type)
        return f"{API}/documents/{document.id}/pages"

    def test_serves_a_png_for_preview(self, api: Api, db: Session) -> None:
        pages = self.stored(api, db, "a.pdf", FileType.PDF, make_pdf)

        response = api.client.get(f"{pages}/1/image")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.content.startswith(b"\x89PNG")

    def test_a_tiff_frame_is_served_too(self, api: Api, db: Session) -> None:
        pages = self.stored(api, db, "a.tiff", FileType.TIFF, make_tiff)

        assert api.client.get(f"{pages}/2/image").status_code == 200

    def test_a_missing_page_is_404_with_a_plain_message(self, api: Api, db: Session) -> None:
        pages = self.stored(api, db, "a.pdf", FileType.PDF, make_pdf)

        response = api.client.get(f"{pages}/99/image")

        assert response.status_code == 404
        assert response.json()["detail"] == "This page has no preview."

    @pytest.mark.parametrize("page", ["0", "-1", "abc", str(2**31)])
    def test_an_invalid_page_number_is_422(self, api: Api, db: Session, page: str) -> None:
        pages = self.stored(api, db, "a.pdf", FileType.PDF, make_pdf)

        assert api.client.get(f"{pages}/{page}/image").status_code == 422

    def test_a_file_outside_storage_is_refused(self, api: Api, db: Session, tmp_path: Path) -> None:
        outside = make_pdf(tmp_path / "outside.pdf")
        document = make_document(db, storage_path=str(outside))

        response = api.client.get(f"{API}/documents/{document.id}/pages/1/image")

        assert response.status_code == 404
        assert "outside" not in response.text

    def test_a_missing_file_is_404(self, api: Api, db: Session) -> None:
        document = make_document(db, storage_path=str(api.settings.storage_dir / "gone.pdf"))

        assert api.client.get(f"{API}/documents/{document.id}/pages/1/image").status_code == 404

    def test_an_unknown_document_is_404(self, api: Api) -> None:
        missing = "00000000-0000-0000-0000-000000000000"

        assert api.client.get(f"{API}/documents/{missing}/pages/1/image").status_code == 404
