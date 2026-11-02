"""Build the labeled dataset: synthetic invoices in every format, with known right answers.

Deterministic, so that anyone gets the same files. Layouts, wording, and number and date formats
vary on purpose, and each page carries distractors a careless extractor would pick up: due dates,
purchase orders, customer and tax IDs.

Run: python eval/generate_dataset.py
"""

import csv
import io
import json
import random
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import openpyxl
import pymupdf
import xlwt
from PIL import Image

ROOT = Path(__file__).parent
DATASET = ROOT / "dataset"
SEED = 20261102
CENTS = Decimal("0.01")

SUPPLIERS = [
    ("Northwind Traders Ltd", "12 Harbour Road, Bristol BS1 4QA"),
    ("Fabrikam Software GmbH", "Hauptstrasse 44, 10115 Berlin"),
    ("Adventure Foods Inc", "880 Market Street, Portland OR 97205"),
    ("Tailspin Logistics Pvt Ltd", "Plot 7, Sector 5, Gurugram 122001"),
    ("Wide World Importers", "455 Dock Lane, Rotterdam 3011"),
    ("Proseware Consulting LLC", "2100 Lake Avenue, Chicago IL 60601"),
    ("Blue Yonder Printing", "17 Mill Street, Leeds LS1 5AB"),
    ("Coho Industrial Supplies", "90 Foundry Way, Sheffield S1 2HE"),
]
CUSTOMERS = [
    "Contoso Retail Inc",
    "Litware Holdings",
    "Trey Research Group",
    "Alpine Ski House",
    "Graphic Design Institute",
    "Woodgrove Bank",
    "City Power and Light",
    "Humongous Insurance",
]
# Distinct subjects, so that search can be evaluated on what each invoice is about.
CATALOG = {
    "machinery": [
        ("Industrial Bearing Kit", "312.50"),
        ("Hydraulic Pump Seal", "18.75"),
        ("Stainless Steel Bolts", "0.85"),
        ("Conveyor Belt Section", "145.00"),
    ],
    "software": [
        ("Annual Cloud Hosting Subscription", "1200.00"),
        ("Database Support Plan", "450.00"),
        ("Security Audit Service", "2300.00"),
        ("User Licence Pack", "89.99"),
    ],
    "catering": [
        ("Office Lunch Platter", "65.00"),
        ("Conference Coffee Service", "120.50"),
        ("Fresh Fruit Basket", "24.00"),
        ("Bottled Water Crate", "14.25"),
    ],
    "logistics": [
        ("Freight Container Transport", "1850.00"),
        ("Warehouse Pallet Storage", "22.50"),
        ("Express Courier Delivery", "38.00"),
        ("Customs Clearance Fee", "175.00"),
    ],
    "printing": [
        ("Glossy Brochure Printing", "0.42"),
        ("Business Card Boxes", "19.90"),
        ("Vinyl Banner Production", "210.00"),
        ("Letterhead Paper Reams", "11.75"),
    ],
}


def money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def us(value: Decimal) -> str:
    return f"{value:,.2f}"


def european(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def spaced(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ")


def date_iso(day: date) -> str:
    return day.isoformat()


def date_long(day: date) -> str:
    return f"{day.day} {day.strftime('%B %Y')}"


def date_us_long(day: date) -> str:
    return f"{day.strftime('%B')} {day.day}, {day.year}"


def date_short(day: date) -> str:
    return f"{day.day:02d}-{day.strftime('%b-%Y')}"


def date_dmy(day: date) -> str:
    return day.strftime("%d/%m/%Y")


@dataclass
class Invoice:
    number: str
    day: date
    supplier: str
    address: str
    customer: str
    currency: str
    rate: Decimal
    lines: list[tuple[str, Decimal, Decimal, Decimal]]
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    hide: set[str] = field(default_factory=set)

    def label(self) -> dict[str, Any]:
        fields = {
            "invoice_number": self.number,
            "invoice_date": self.day.isoformat(),
            "supplier_name": self.supplier,
            "customer_name": self.customer,
            "subtotal": f"{self.subtotal:.2f}",
            "tax": f"{self.tax:.2f}",
            "total": f"{self.total:.2f}",
            "currency": self.currency,
        }
        # The PDF layouts print the currency only beside the total, so hiding one hides both.
        hidden = self.hide | ({"currency"} if "total" in self.hide else set())
        return {
            "fields": {
                key: (None if key in hidden else value) for key, value in fields.items()
            },
            "line_items": [
                {
                    "description": description,
                    "quantity": f"{quantity}",
                    "unit_price": f"{price:.2f}",
                    "amount": f"{amount:.2f}",
                }
                for description, quantity, price, amount in self.lines
            ],
        }


class Maker:
    def __init__(self) -> None:
        self.random = random.Random(SEED)
        self.counter = 1000
        self.documents: list[dict[str, Any]] = []
        self.subjects = list(CATALOG)

    def invoice(
        self,
        *,
        prefix: str = "INV",
        currency: str = "USD",
        subject: str | None = None,
        rows: int | None = None,
    ) -> Invoice:
        self.counter += 1
        supplier, address = self.random.choice(SUPPLIERS)
        items = CATALOG[subject or self.random.choice(self.subjects)]
        count = rows if rows is not None else self.random.randint(2, 4)
        lines = []
        for offset in range(count):
            description, price = items[offset % len(items)]
            quantity = Decimal(self.random.randint(1, 12))
            unit = Decimal(price)
            lines.append((description, quantity, unit, money(quantity * unit)))
        subtotal = sum((line[3] for line in lines), Decimal(0))
        rate = Decimal(self.random.choice(["0.05", "0.10", "0.15", "0.20"]))
        tax = money(subtotal * rate)
        return Invoice(
            number=f"{prefix}-{2026}-{self.counter}",
            # Day above 12, so that day and month cannot be swapped unless a test wants that.
            day=date(2026, self.random.randint(1, 9), self.random.randint(13, 28)),
            supplier=supplier,
            address=address,
            customer=self.random.choice(CUSTOMERS),
            currency=currency,
            rate=rate,
            lines=lines,
            subtotal=subtotal,
            tax=tax,
            total=subtotal + tax,
        )

    def add(
        self,
        name: str,
        category: str,
        data: bytes,
        *,
        invoices: list[Invoice] | None = None,
        outcome: str = "completed",
        issues: list[str] | None = None,
        depends_on: list[str] | None = None,
        notes: str = "",
        pages: int = 1,
        subject: str | None = None,
    ) -> str:
        path = DATASET / category / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        relative = f"{category}/{name}"
        self.documents.append(
            {
                "file": relative,
                "category": category,
                "expected_outcome": outcome,
                "invoices": [invoice.label() for invoice in invoices or []],
                "expected_issues": sorted(issues or []),
                "depends_on": depends_on or [],
                "pages": pages,
                "subject": subject,
                "notes": notes,
            }
        )
        return relative


# ---- Layouts. Each draws one invoice on one page, with its own wording. ----


def _text(
    page: pymupdf.Page,
    x: float,
    y: float,
    text: str,
    size: float = 10,
    bold: bool = False,
):
    page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv")


def classic(page: pymupdf.Page, inv: Invoice, fmt=us, day=date_iso) -> None:
    _text(page, 50, 60, "INVOICE", 22, True)
    _text(page, 50, 90, inv.supplier, 11, True)
    _text(page, 50, 104, inv.address, 9)
    _text(page, 50, 118, "Tax ID: 44-8810276", 9)
    _text(page, 360, 60, f"Invoice Number: {inv.number}", 10)
    if "invoice_date" not in inv.hide:
        _text(page, 360, 76, f"Invoice Date: {day(inv.day)}", 10)
    _text(page, 360, 92, f"Due Date: {day(inv.day + timedelta(days=30))}", 10)
    _text(page, 360, 108, "PO Number: PO-77120", 10)
    if "customer_name" not in inv.hide:
        _text(page, 50, 150, "Bill To:", 10, True)
        _text(page, 50, 165, inv.customer, 10)
    _table(page, inv, 205, fmt)
    y = 215 + 20 * len(inv.lines) + 25
    _totals(
        page,
        inv,
        y,
        fmt,
        ("Subtotal", f"Tax ({inv.rate * 100:.0f}%)", "Total"),
        inv.currency,
    )


def _table(page, inv: Invoice, y: float, fmt) -> None:
    columns = (50, 310, 370, 465)
    for x, heading in zip(
        columns, ("Description", "Qty", "Unit Price", "Amount"), strict=True
    ):
        _text(page, x, y, heading, 10, True)
    page.draw_line((50, y + 6), (545, y + 6))
    y += 25
    for description, quantity, price, amount in inv.lines:
        for x, cell in zip(
            columns, (description, f"{quantity}", fmt(price), fmt(amount)), strict=True
        ):
            _text(page, x, y, cell, 10)
        y += 20


def _totals(page, inv: Invoice, y: float, fmt, labels, currency: str | None) -> None:
    rows = [
        ("subtotal", labels[0], inv.subtotal),
        ("tax", labels[1], inv.tax),
        ("total", labels[2], inv.total),
    ]
    for key, label, value in rows:
        if key in inv.hide:
            continue
        shown = fmt(value)
        if key == "total" and currency and "currency" not in inv.hide:
            shown = f"{shown} {currency}"
        _text(page, 370, y, label, 10, key == "total")
        _text(page, 465, y, shown, 10, key == "total")
        y += 18


def modern(page: pymupdf.Page, inv: Invoice, fmt=us, day=date_long) -> None:
    page.draw_rect(
        pymupdf.Rect(0, 0, 595, 70), color=(0.15, 0.2, 0.4), fill=(0.15, 0.2, 0.4)
    )
    page.insert_text(
        (50, 45), inv.supplier, fontsize=18, fontname="hebo", color=(1, 1, 1)
    )
    _text(page, 50, 95, inv.address, 9)
    _text(page, 50, 125, "BILLED TO", 8, True)
    if "customer_name" not in inv.hide:
        _text(page, 50, 140, inv.customer, 11)
    _text(page, 330, 125, "INVOICE #", 8, True)
    _text(page, 330, 140, inv.number, 11)
    _text(page, 450, 125, "DATE OF ISSUE", 8, True)
    if "invoice_date" not in inv.hide:
        _text(page, 450, 140, day(inv.day), 10)
    _text(page, 330, 165, "ORDER REF", 8, True)
    _text(page, 330, 180, "ORD-5581", 10)
    _table(page, inv, 215, fmt)
    y = 225 + 20 * len(inv.lines) + 25
    _totals(page, inv, y, fmt, ("Net amount", "VAT", "Amount Due"), inv.currency)
    _text(page, 50, 760, "Payment terms: 30 days. Late payment interest applies.", 8)


def compact(page: pymupdf.Page, inv: Invoice, fmt=us, day=date_short) -> None:
    _text(page, 50, 55, f"TAX INVOICE  {inv.supplier}", 13, True)
    _text(page, 50, 72, inv.address + "  Tel: +1 555 0100", 8)
    _text(page, 50, 100, f"Inv No: {inv.number}", 10)
    if "invoice_date" not in inv.hide:
        _text(page, 200, 100, f"Dated: {day(inv.day)}", 10)
    _text(page, 360, 100, "Cust ID: C-20044", 10)
    if "customer_name" not in inv.hide:
        _text(page, 50, 118, f"Customer: {inv.customer}", 10)
    y = 150
    for description, quantity, price, amount in inv.lines:
        _text(page, 50, y, f"{quantity} x {description} @ {fmt(price)}", 10)
        _text(page, 465, y, fmt(amount), 10)
        y += 18
    page.draw_line((50, y - 6), (545, y - 6))
    y += 14
    _totals(page, inv, y, fmt, ("Sub-total", "Sales tax", "Grand Total"), inv.currency)


LAYOUTS = (classic, modern, compact)


def pdf_of(draw, inv: Invoice, **kwargs) -> bytes:
    with pymupdf.open() as document:
        draw(document.new_page(width=595, height=842), inv, **kwargs)
        return document.tobytes()


def render(data: bytes, page: int = 0, dpi: int = 200) -> np.ndarray:
    with pymupdf.open("pdf", data) as document:
        pix = document[page].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        samples = np.frombuffer(pix.samples, dtype=np.uint8)
        return samples.reshape(pix.height, pix.stride)[:, : pix.width].copy()


# ---- Damage, as from a scanner or a phone. ----


def rotate(image: np.ndarray, degrees: float) -> np.ndarray:
    height, width = image.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), degrees, 1.0)
    return cv2.warpAffine(
        image, matrix, (width, height), borderValue=255, flags=cv2.INTER_CUBIC
    )


def noise(image: np.ndarray, sigma: float, seed: int) -> np.ndarray:
    grain = np.random.default_rng(seed).normal(0, sigma, image.shape)
    return np.clip(image.astype(np.float64) + grain, 0, 255).astype(np.uint8)


def blur(image: np.ndarray, size: int) -> np.ndarray:
    return cv2.GaussianBlur(image, (size, size), 0)


def shadow(image: np.ndarray, strength: float) -> np.ndarray:
    gradient = np.linspace(1 - strength, 1, image.shape[1])
    return (image.astype(np.float64) * gradient[np.newaxis, :]).astype(np.uint8)


def faded(image: np.ndarray) -> np.ndarray:
    return (120 + image.astype(np.float64) * 70 / 255).astype(np.uint8)


def smaller(image: np.ndarray, factor: float) -> np.ndarray:
    return cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)


def jpeg(image: np.ndarray, quality: int = 70) -> bytes:
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buffer.tobytes()


def png(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return buffer.tobytes()


def tiff(images: list[np.ndarray]) -> bytes:
    buffer = io.BytesIO()
    frames = [Image.fromarray(image) for image in images]
    frames[0].save(
        buffer,
        format="TIFF",
        save_all=True,
        append_images=frames[1:],
        compression="tiff_lzw",
    )
    return buffer.getvalue()


def scanned_pdf(images: list[np.ndarray]) -> bytes:
    with pymupdf.open() as document:
        for image in images:
            page = document.new_page(width=595, height=842)
            page.insert_image(page.rect, stream=jpeg(image))
        return document.tobytes()


# ---- Spreadsheets. ----


def flat_rows(invoices: list[Invoice], headers=None) -> list[list[str]]:
    rows = [
        headers
        or [
            "Invoice No",
            "Invoice Date",
            "Supplier",
            "Customer",
            "Subtotal",
            "Tax",
            "Total",
            "Currency",
        ]
    ]
    for inv in invoices:
        rows.append(
            [
                inv.number,
                inv.day.isoformat(),
                inv.supplier,
                inv.customer,
                f"{inv.subtotal:.2f}",
                f"{inv.tax:.2f}",
                f"{inv.total:.2f}",
                inv.currency,
            ]
        )
    return rows


def as_csv(
    rows: list[list[str]], delimiter: str = ",", encoding: str = "utf-8"
) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=delimiter, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode(encoding)


def number_or_text(cell: str) -> float | str:
    try:
        return float(cell)
    except ValueError:
        return cell


def as_xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append([number_or_text(cell) if cell else None for cell in row])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def as_xls(rows: list[list[str]]) -> bytes:
    book = xlwt.Workbook()
    sheet = book.add_sheet("Invoices")
    for y, row in enumerate(rows):
        for x, cell in enumerate(row):
            sheet.write(y, x, number_or_text(cell))
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def single_invoice_rows(inv: Invoice) -> list[list[str]]:
    """Details above a table, and totals below it, as an accountant's export looks."""
    rows = [
        ["Invoice No:", inv.number],
        ["Invoice Date:", inv.day.isoformat()],
        ["Supplier:", inv.supplier],
        ["Bill To:", inv.customer],
        ["Currency:", inv.currency],
        [],
        ["Description", "Qty", "Unit Price", "Amount"],
    ]
    for description, quantity, price, amount in inv.lines:
        rows.append([description, f"{quantity}", f"{price:.2f}", f"{amount:.2f}"])
    rows.append(["Subtotal", "", "", f"{inv.subtotal:.2f}"])
    rows.append(["Tax", "", "", f"{inv.tax:.2f}"])
    rows.append(["Total", "", "", f"{inv.total:.2f}"])
    return rows


def no_line_labels(inv: Invoice) -> Invoice:
    """A row with no line items has none to label."""
    inv.lines = []
    return inv


# ---- The dataset. ----


def build() -> None:
    if DATASET.exists():
        shutil.rmtree(DATASET)
    make = Maker()

    # Digital PDFs, each layout with several number and date formats.
    for index, (draw, fmt, day, currency) in enumerate(
        [
            (classic, us, date_iso, "USD"),
            (classic, european, date_iso, "EUR"),
            (modern, us, date_long, "USD"),
            (modern, spaced, date_us_long, "EUR"),
            (compact, us, date_short, "CAD"),
            (compact, european, date_dmy, "EUR"),
        ]
    ):
        subject = make.subjects[index % len(make.subjects)]
        inv = make.invoice(currency=currency, subject=subject)
        make.add(
            f"digital-{index + 1:02d}-{draw.__name__}.pdf",
            "digital_pdf",
            pdf_of(draw, inv, fmt=fmt, day=day),
            invoices=[inv],
            subject=subject,
            notes=f"{draw.__name__} layout",
        )

    # A batch: several invoices in one PDF, the assignment's central scenario.
    batch = [make.invoice(prefix="BTC") for _ in range(4)]
    with pymupdf.open() as document:
        for inv, draw in zip(batch, (classic, modern, compact, classic), strict=True):
            draw(document.new_page(width=595, height=842), inv)
        data = document.tobytes()
    make.add(
        "batch-4-invoices.pdf",
        "digital_pdf",
        data,
        invoices=batch,
        pages=4,
        notes="four invoices in one file",
    )

    # One long invoice running across pages, with "Page x of y".
    long_inv = make.invoice(prefix="LNG", subject="machinery", rows=4)
    extra = make.invoice(prefix="XXX", subject="logistics", rows=4)
    long_inv.lines = long_inv.lines + extra.lines
    long_inv.subtotal = sum((line[3] for line in long_inv.lines), Decimal(0))
    long_inv.tax = money(long_inv.subtotal * long_inv.rate)
    long_inv.total = long_inv.subtotal + long_inv.tax
    with pymupdf.open() as document:
        first = document.new_page(width=595, height=842)
        _text(first, 50, 60, "INVOICE", 22, True)
        _text(first, 50, 90, long_inv.supplier, 11, True)
        _text(first, 360, 60, f"Invoice Number: {long_inv.number}", 10)
        _text(first, 360, 76, f"Invoice Date: {long_inv.day.isoformat()}", 10)
        _text(first, 50, 130, f"Bill To: {long_inv.customer}", 10)
        half = Invoice(**{**long_inv.__dict__, "lines": long_inv.lines[:4]})
        _table(first, half, 170, us)
        _text(first, 50, 800, "Page 1 of 2", 8)
        second = document.new_page(width=595, height=842)
        _text(second, 50, 60, f"Invoice Number: {long_inv.number} (continued)", 10)
        rest = Invoice(**{**long_inv.__dict__, "lines": long_inv.lines[4:]})
        _table(second, rest, 90, us)
        _totals(
            second, long_inv, 240, us, ("Subtotal", "Tax", "Total"), long_inv.currency
        )
        _text(second, 50, 800, "Page 2 of 2", 8)
        data = document.tobytes()
    make.add(
        "multi-page-invoice.pdf",
        "digital_pdf",
        data,
        invoices=[long_inv],
        pages=2,
        notes="one invoice on two pages",
    )

    # Scans of ordinary quality.
    for index, damage in enumerate(
        [
            lambda image, _: image,
            lambda image, seed: noise(image, 8, seed),
            lambda image, seed: rotate(noise(image, 6, seed), -2.5),
            lambda image, _: blur(image, 3),
            lambda image, _: rotate(image, 4),
        ]
    ):
        inv = make.invoice(currency=make.random.choice(["USD", "EUR", "GBP"]))
        image = damage(render(pdf_of(LAYOUTS[index % 3], inv)), 100 + index)
        make.add(
            f"scan-{index + 1:02d}.pdf",
            "scanned_pdf",
            scanned_pdf([image]),
            invoices=[inv],
        )

    # Images, in each format.
    for index, (name, encode) in enumerate(
        [
            ("photo-01.png", png),
            ("photo-02.jpg", jpeg),
            ("photo-03.jpg", lambda image: jpeg(rotate(image, -1.5), 60)),
            ("photo-04.tiff", lambda image: tiff([image])),
        ]
    ):
        inv = make.invoice()
        image = render(pdf_of(LAYOUTS[index % 3], inv))
        make.add(name, "image", encode(image), invoices=[inv])

    # A multi-page TIFF: one invoice, with a page of terms after it.
    inv = make.invoice(currency="EUR")
    with pymupdf.open() as document:
        terms = document.new_page(width=595, height=842)
        _text(terms, 50, 80, "Terms and conditions of supply", 12, True)
        _text(
            terms,
            50,
            110,
            "Goods remain the property of the seller until paid in full.",
            10,
        )
        terms_image = render(document.tobytes())
    make.add(
        "multi-page.tiff",
        "image",
        tiff([render(pdf_of(classic, inv)), terms_image]),
        invoices=[inv],
        pages=2,
    )

    # Poor quality, which real OCR should struggle with.
    for index, (name, damage) in enumerate(
        [
            ("heavy-noise", lambda image: noise(image, 40, 11)),
            ("heavy-blur", lambda image: blur(image, 9)),
            ("faded", faded),
            ("low-resolution", lambda image: smaller(image, 0.4)),
            ("sideways", lambda image: np.rot90(image).copy()),
            (
                "phone-photo",
                lambda image: blur(noise(shadow(rotate(image, 3), 0.5), 14, 12), 3),
            ),
        ]
    ):
        inv = make.invoice(currency=make.random.choice(["USD", "EUR"]))
        image = damage(render(pdf_of(LAYOUTS[index % 3], inv)))
        make.add(f"{name}.png", "low_quality", png(image), invoices=[inv], notes=name)

    # Spreadsheets: rule-based mapping where the columns are recognizable.
    inv = no_line_labels(make.invoice())
    make.add(
        "flat.csv",
        "spreadsheet",
        as_csv(flat_rows([inv])),
        invoices=[inv],
        notes="one row, recognized",
    )
    invs = [no_line_labels(make.invoice(currency="EUR")) for _ in range(3)]
    make.add(
        "flat-semicolon.csv",
        "spreadsheet",
        as_csv(flat_rows(invs), delimiter=";"),
        invoices=invs,
        notes="three invoices, semicolons",
    )
    invs = [make.invoice(prefix="SHT") for _ in range(2)]
    make.add(
        "two-sheets.xlsx",
        "spreadsheet",
        as_xlsx(
            {
                "March": single_invoice_rows(invs[0]),
                "April": single_invoice_rows(invs[1]),
            }
        ),
        invoices=invs,
        pages=2,
        notes="one invoice per sheet",
    )
    invs = [no_line_labels(make.invoice()) for _ in range(2)]
    make.add(
        "old-format.xls",
        "spreadsheet",
        as_xls(flat_rows(invs)),
        invoices=invs,
        notes="XLS",
    )
    inv = make.invoice(currency="EUR")
    make.add(
        "cp1252.csv",
        "spreadsheet",
        as_csv(single_invoice_rows(inv), encoding="cp1252"),
        invoices=[inv],
        notes="not UTF-8, details and table",
    )
    # Column names the rules do not know, so these need the LLM.
    inv = no_line_labels(make.invoice())
    make.add(
        "odd-columns.csv",
        "spreadsheet",
        as_csv(
            flat_rows(
                [inv],
                [
                    "Doc Ref",
                    "Issued On",
                    "Sold By",
                    "Sold To",
                    "Before Tax",
                    "Levy",
                    "To Pay",
                    "Ccy",
                ],
            )
        ),
        invoices=[inv],
        notes="unknown columns, needs the LLM",
    )

    # Seeded problems: validation should catch each of these, and nothing else.
    inv = make.invoice(prefix="BAD")
    inv.total = inv.total + Decimal("50.00")
    make.add(
        "wrong-total.pdf",
        "seeded_error",
        pdf_of(classic, inv),
        invoices=[inv],
        issues=["total_mismatch"],
        notes="subtotal + tax does not equal total",
    )
    inv = make.invoice(prefix="BAD", rows=3)
    inv.subtotal = inv.subtotal + Decimal("25.00")
    inv.tax = money(inv.subtotal * inv.rate)
    inv.total = inv.subtotal + inv.tax
    make.add(
        "wrong-subtotal.pdf",
        "seeded_error",
        pdf_of(modern, inv),
        invoices=[inv],
        issues=["line_items_subtotal_mismatch"],
        notes="line items do not add up to the subtotal",
    )
    inv = make.invoice(prefix="BAD", rows=3)
    description, quantity, price, amount = inv.lines[0]
    inv.lines[0] = (description, quantity, price + Decimal("3.00"), amount)
    make.add(
        "wrong-line.pdf",
        "seeded_error",
        pdf_of(classic, inv),
        invoices=[inv],
        issues=["line_item_amount_mismatch"],
        notes="quantity times price does not equal amount",
    )
    inv = make.invoice(prefix="BAD")
    inv.hide = {"invoice_date"}
    make.add(
        "no-date.pdf",
        "seeded_error",
        pdf_of(classic, inv),
        invoices=[inv],
        issues=["missing_required_field"],
        notes="no invoice date printed",
    )
    inv = make.invoice(prefix="BAD")
    inv.hide = {"total", "tax"}
    make.add(
        "no-total.pdf",
        "seeded_error",
        pdf_of(classic, inv),
        invoices=[inv],
        issues=["missing_field", "missing_required_field"],
        notes="no total or tax printed, which must not be calculated",
    )
    inv = make.invoice(prefix="BAD")
    inv.day = date(2031, 6, 15)
    make.add(
        "future-date.pdf",
        "seeded_error",
        pdf_of(classic, inv),
        invoices=[inv],
        issues=["date_in_future"],
        notes="dated years ahead",
    )
    inv = make.invoice(prefix="DUP")
    first = make.add(
        "duplicate-digital.pdf",
        "seeded_error",
        pdf_of(modern, inv),
        invoices=[inv],
        notes="the original",
    )
    make.add(
        "duplicate-scan.pdf",
        "seeded_error",
        scanned_pdf([noise(render(pdf_of(modern, inv)), 6, 21)]),
        invoices=[inv],
        issues=["duplicate_invoice"],
        depends_on=[first],
        notes="a scan of the same invoice",
    )
    # Cross-checks between formats.
    inv = make.invoice(prefix="CRS")
    first = make.add(
        "match-original.pdf",
        "seeded_error",
        pdf_of(classic, inv),
        invoices=[inv],
        notes="PDF of a pair that agree",
    )
    copy = Invoice(**{**inv.__dict__, "lines": []})
    make.add(
        "match-copy.csv",
        "seeded_error",
        as_csv(flat_rows([copy])),
        invoices=[copy],
        issues=["cross_document_verified"],
        depends_on=[first],
        notes="the same invoice as a spreadsheet",
    )
    inv = make.invoice(prefix="CRS")
    first = make.add(
        "mismatch-original.pdf",
        "seeded_error",
        pdf_of(classic, inv),
        invoices=[inv],
        notes="PDF of a pair that disagree",
    )
    copy = Invoice(**{**inv.__dict__, "lines": []})
    copy.total = copy.total + Decimal("40.00")
    make.add(
        "mismatch-copy.xlsx",
        "seeded_error",
        as_xlsx({"Invoices": flat_rows([copy])}),
        invoices=[copy],
        issues=["pdf_excel_mismatch", "total_mismatch"],
        depends_on=[first],
        notes="the spreadsheet disagrees on the total",
    )
    inv = make.invoice(prefix="CRS")
    first = make.add(
        "pdf-pair-original.pdf",
        "seeded_error",
        pdf_of(classic, inv),
        invoices=[inv],
        notes="first PDF of a pair",
    )
    copy = Invoice(**inv.__dict__)
    copy.day = copy.day + timedelta(days=1)
    make.add(
        "pdf-pair-copy.pdf",
        "seeded_error",
        pdf_of(compact, copy),
        invoices=[copy],
        issues=["pdf_pdf_mismatch"],
        depends_on=[first],
        notes="second PDF disagrees on the date",
    )

    # Files that must fail clearly, or be refused.
    good = pdf_of(classic, make.invoice(prefix="CUT"))
    make.add(
        "not-really.pdf",
        "corrupted",
        b"%PDF-1.7\nthis file is damaged and has no content",
        outcome="failed",
        notes="PDF header, garbage body",
    )
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), "secret")
        make.add(
            "password.pdf",
            "corrupted",
            document.tobytes(
                encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o"
            ),
            outcome="failed",
            notes="password protected",
        )
    with pymupdf.open() as document:
        document.new_page(width=595, height=842)
        make.add(
            "blank.pdf",
            "corrupted",
            document.tobytes(),
            outcome="failed",
            notes="one blank page",
        )
    make.add(
        "truncated.png",
        "corrupted",
        png(render(good))[:2000],
        outcome="failed",
        notes="cut short",
    )
    make.add("empty.csv", "corrupted", b"", outcome="failed", notes="zero bytes")
    make.add(
        "program.pdf",
        "corrupted",
        b"MZ\x90\x00\x03\x00\x00\x00" + bytes(100),
        outcome="rejected",
        notes="an executable named .pdf",
    )

    labels = ROOT / "labels.json"
    labels.write_text(
        json.dumps({"seed": SEED, "documents": make.documents}, indent=2) + "\n",
        encoding="utf-8",
    )
    by_category: dict[str, int] = {}
    for document in make.documents:
        by_category[document["category"]] = by_category.get(document["category"], 0) + 1
    size = sum(path.stat().st_size for path in DATASET.rglob("*") if path.is_file())
    print(
        f"{len(make.documents)} documents, {size / 1024 / 1024:.1f} MB: {by_category}"
    )


if __name__ == "__main__":
    sys.exit(build())
