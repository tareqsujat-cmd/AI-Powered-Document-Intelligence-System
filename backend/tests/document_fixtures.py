"""Generates invoices as digital PDFs and as degraded scans. No binary fixtures are committed."""

from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt
import pymupdf

GrayImage = npt.NDArray[np.uint8]

INVOICE_ROWS = [
    ("Industrial Bearing Kit", "4", "312.50", "1,250.00"),
    ("Hydraulic Pump Seal", "12", "18.75", "225.00"),
    ("Stainless Steel Bolts", "150", "0.85", "127.50"),
]
# Distinctive values that an extractor must be able to read from the page.
INVOICE_TOKENS = [
    "INV-2026-0042",
    "2026-03-15",
    "Northwind",
    "Contoso",
    "Bearing",
    "Hydraulic",
    "1,250.00",
    "225.00",
    "127.50",
    "1,602.50",
    "160.25",
    "1,762.75",
]


def draw_invoice(page: pymupdf.Page, number: str = "INV-2026-0042") -> None:
    page.insert_text((50, 60), "INVOICE", fontsize=22, fontname="hebo")
    page.insert_text((50, 95), f"Invoice Number: {number}", fontsize=11)
    page.insert_text((50, 112), "Invoice Date: 2026-03-15", fontsize=11)
    page.insert_text((50, 145), "Supplier: Northwind Traders Ltd", fontsize=11)
    page.insert_text((320, 145), "Bill To: Contoso Retail Inc", fontsize=11)

    columns = (50, 300, 370, 460)
    for x, heading in zip(columns, ("Description", "Qty", "Unit Price", "Amount"), strict=True):
        page.insert_text((x, 190), heading, fontsize=11, fontname="hebo")
    page.draw_line((50, 196), (545, 196))
    y = 215
    for row in INVOICE_ROWS:
        for x, cell in zip(columns, row, strict=True):
            page.insert_text((x, y), cell, fontsize=11)
        y += 20

    y += 15
    for label, value in (("Subtotal", "1,602.50"), ("Tax (10%)", "160.25"), ("Total", "1,762.75")):
        page.insert_text((370, y), label, fontsize=11, fontname="hebo")
        page.insert_text((460, y), value, fontsize=11)
        y += 18


def render_invoice(dpi: int = 300) -> GrayImage:
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=842)
        draw_invoice(page)
        pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
        data = np.frombuffer(pixmap.samples, dtype=np.uint8)
        return data.reshape(pixmap.height, pixmap.stride)[:, : pixmap.width].copy()


def encode_png(image: GrayImage) -> bytes:
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return buffer.tobytes()


def make_digital_pdf(path: Path, pages: int = 1) -> Path:
    with pymupdf.open() as document:
        for index in range(pages):
            draw_invoice(document.new_page(width=595, height=842), f"INV-2026-{index + 42:04d}")
        document.save(path)
    return path


def insert_scan(document: pymupdf.Document, image: GrayImage) -> pymupdf.Page:
    page = document.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=encode_png(image))
    return page


def make_scanned_pdf(path: Path, pages: int = 1, dpi: int = 200) -> Path:
    image = render_invoice(dpi)
    with pymupdf.open() as document:
        for _ in range(pages):
            insert_scan(document, image)
        document.save(path)
    return path


def rotate_image(image: GrayImage, degrees: float) -> GrayImage:
    height, width = image.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), degrees, 1.0)
    return cv2.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )


def add_noise(image: GrayImage, sigma: float, seed: int = 7) -> GrayImage:
    noise = np.random.default_rng(seed).normal(0, sigma, image.shape)
    return np.clip(image.astype(np.float64) + noise, 0, 255).astype(np.uint8)


def add_salt_and_pepper(image: GrayImage, amount: float, seed: int = 7) -> GrayImage:
    random = np.random.default_rng(seed)
    result = image.copy()
    mask = random.random(image.shape)
    result[mask < amount / 2] = 0
    result[mask > 1 - amount / 2] = 255
    return result


def blur(image: GrayImage, kernel: int) -> GrayImage:
    return cv2.GaussianBlur(image, (kernel, kernel), 0)


def low_contrast(image: GrayImage, low: int = 130, high: int = 190) -> GrayImage:
    return (low + image.astype(np.float64) * (high - low) / 255).astype(np.uint8)


def uneven_lighting(image: GrayImage, strength: float = 0.75) -> GrayImage:
    """A dark shadow across one side of the page, as from a phone photo or a book spine."""
    gradient = np.linspace(1.0 - strength, 1.0, image.shape[1])
    return (image.astype(np.float64) * gradient[np.newaxis, :]).astype(np.uint8)


def downscale(image: GrayImage, factor: float) -> GrayImage:
    return cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)


def jpeg_artifacts(image: GrayImage, quality: int = 12) -> GrayImage:
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)


DEGRADATIONS = {
    "clean_300dpi": lambda image: image,
    "skew_3deg": lambda image: rotate_image(image, 3),
    "skew_-7deg": lambda image: rotate_image(image, -7),
    "rotated_90": lambda image: np.rot90(image).copy(),
    "rotated_180": lambda image: np.rot90(image, 2).copy(),
    "gaussian_noise_25": lambda image: add_noise(image, 25),
    "gaussian_noise_50": lambda image: add_noise(image, 50),
    "salt_pepper_2pct": lambda image: add_salt_and_pepper(image, 0.02),
    "blur_5": lambda image: blur(image, 5),
    "blur_9": lambda image: blur(image, 9),
    "low_contrast": low_contrast,
    "uneven_lighting": uneven_lighting,
    "downscale_40pct": lambda image: downscale(image, 0.4),
    "downscale_25pct": lambda image: downscale(image, 0.25),
    "jpeg_q12": jpeg_artifacts,
    "phone_photo": lambda image: blur(
        add_noise(uneven_lighting(rotate_image(image, 2.5), 0.5), 12), 3
    ),
}
