import pytest

from app.db.enums import PageStatus
from app.extraction.segmentation import (
    Segment,
    chunk_segment,
    find_invoice_number,
    split_into_invoices,
    split_long_text,
)
from app.processing.results import PageResult


def page(number: int, text: str) -> PageResult:
    return PageResult(page_number=number, text=text)


def ranges(segments: list[Segment]) -> list[tuple[int, int]]:
    return [(segment.first_page, segment.last_page) for segment in segments]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Invoice Number: INV-2026-0042", "INV-2026-0042"),
        ("Invoice No. 1001", "1001"),
        ("INVOICE #A/77", "A/77"),
        ("Inv: 2026-55", "2026-55"),
        ("Tax Invoice No: T-9", "T-9"),
        ("Invoice Date: 2026-03-15\nInvoice Number: X-100", "X-100"),
        ("Invoice Date 15/03/2026", None),
        ("INVOICE\nInvoice Number: BATCH-0001", "BATCH-0001"),
        ("INVOICE\n\nInvoice No: 77", None),
        ("TAX INVOICE\nInvoice No.: 2026/77", "2026/77"),
        ("Invoice Number\nINV-500", "INV-500"),
        ("Thank you for your invoice payment", None),
        ("no numbers here", None),
        # Headings in one row, values in the next: tabs from a text layer, spaces from OCR.
        (
            "BILLED TO\tINVOICE #\tDATE OF ISSUE\nAcme Ltd\tBTC-2026-1008\t21 March 2026",
            "BTC-2026-1008",
        ),
        ("BILLED TO INVOICE # DATE OF ISSUE\nAcme Ltd INV-2026-1014 24 June 2026", "INV-2026-1014"),
        ("DATE\tINVOICE NO.\n2026-03-21\t2026/0042", "2026/0042"),
        ("DATE INVOICE NO.\n2026-03-21 X-77 Unit 4", "X-77"),
        ("INVOICE #\tDATE\nAcme Ltd\t21 March 2026", None),
        ("Invoice No: 77\nShip to Unit B-12", None),
        ("INVOICE # DATE\n2026 12 units", None),
    ],
)
def test_find_invoice_number(text: str, expected: str | None) -> None:
    assert find_invoice_number(text) == expected


def test_invoices_with_numbers_under_column_headings_are_separated() -> None:
    # Found by the evaluation: a batch PDF merged two invoices whose number sat under its heading.
    pages = [
        page(1, "INVOICE\nInvoice Number: BTC-1007\nTotal 5.00"),
        page(2, "BILLED TO\tINVOICE #\tDATE\nAcme Ltd\tBTC-1008\t21 March 2026\nAmount Due 6.00"),
        page(3, "BILLED TO INVOICE # DATE\nAcme Ltd BTC-1009 22 March 2026\nAmount Due 7.00"),
    ]

    assert ranges(split_into_invoices(pages)) == [(1, 1), (2, 2), (3, 3)]


def test_different_numbers_start_new_invoices() -> None:
    pages = [page(1, "Invoice No: A-1"), page(2, "Invoice No: A-2"), page(3, "Invoice No: A-3")]

    assert ranges(split_into_invoices(pages)) == [(1, 1), (2, 2), (3, 3)]


def test_invoices_with_a_title_above_the_number_are_still_separated() -> None:
    # Found by the real-API run: the title "INVOICE" hid the number, merging a whole batch into one.
    pages = [
        page(1, "INVOICE\nInvoice Number: BATCH-0001\nTotal 5.00"),
        page(2, "INVOICE\nInvoice Number: BATCH-0002\nTotal 6.00"),
    ]

    assert ranges(split_into_invoices(pages)) == [(1, 1), (2, 2)]


def test_repeated_number_is_one_invoice() -> None:
    pages = [page(1, "Invoice No: A-1"), page(2, "Invoice No: A-1"), page(3, "Invoice No: A-2")]

    assert ranges(split_into_invoices(pages)) == [(1, 2), (3, 3)]


def test_continuation_pages_with_no_number_stay_with_their_invoice() -> None:
    pages = [
        page(1, "Invoice No: A-1"),
        page(2, "more line items"),
        page(3, "Total 100.00"),
        page(4, "Invoice No: A-2"),
    ]

    assert ranges(split_into_invoices(pages)) == [(1, 3), (4, 4)]


def test_ocr_misread_of_a_repeated_number_does_not_split_the_invoice() -> None:
    pages = [page(1, "Invoice No: INV-0042"), page(2, "Invoice No: INV-OO42")]

    assert ranges(split_into_invoices(pages)) == [(1, 2)]


def test_consecutive_numbers_are_not_mistaken_for_ocr_errors() -> None:
    pages = [page(1, "Invoice No: INV-0042"), page(2, "Invoice No: INV-0043")]

    assert ranges(split_into_invoices(pages)) == [(1, 1), (2, 2)]


def test_page_one_marker_starts_a_new_invoice_with_no_number() -> None:
    pages = [
        page(1, "Receipt\nPage 1 of 2"),
        page(2, "more\nPage 2 of 2"),
        page(3, "Receipt\nPage 1 of 1"),
    ]

    assert ranges(split_into_invoices(pages)) == [(1, 2), (3, 3)]


def test_later_page_marker_does_not_start_a_new_invoice() -> None:
    pages = [page(1, "Invoice No: A-1\nPage 1 of 2"), page(2, "lines\nPage 2 of 2")]

    assert ranges(split_into_invoices(pages)) == [(1, 2)]


def test_number_first_seen_on_a_later_page_does_not_split() -> None:
    pages = [page(1, "Letterhead"), page(2, "Invoice No: A-1"), page(3, "Invoice No: A-1")]

    assert ranges(split_into_invoices(pages)) == [(1, 3)]


def test_blank_pages_are_skipped() -> None:
    pages = [page(1, "Invoice No: A-1"), page(2, ""), page(3, "Invoice No: A-2")]

    assert ranges(split_into_invoices(pages)) == [(1, 1), (3, 3)]


def test_failed_pages_have_no_text_and_are_skipped() -> None:
    pages = [
        page(1, "Invoice No: A-1"),
        PageResult(page_number=2, status=PageStatus.FAILED, error="bad"),
        page(3, "Total 5.00"),
    ]

    assert ranges(split_into_invoices(pages)) == [(1, 3)]


def test_no_pages_gives_no_segments() -> None:
    assert split_into_invoices([]) == []
    assert split_into_invoices([page(1, "")]) == []


def test_a_long_batch_scales_linearly() -> None:
    pages = [page(index + 1, f"Invoice No: BATCH-{index:05d}") for index in range(1000)]

    assert len(split_into_invoices(pages)) == 1000


def test_short_text_is_not_split() -> None:
    assert split_long_text("a\nb", 100) == ["a\nb"]


def test_split_long_text_never_cuts_a_line_and_loses_nothing() -> None:
    lines = [f"row {index} widget 10.00" for index in range(100)]
    text = "\n".join(lines)

    chunks = split_long_text(text, 200)

    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "\n".join(chunks) == text


def test_split_long_text_handles_a_single_enormous_line() -> None:
    chunks = split_long_text("x" * 1000, 300)

    assert all(len(chunk) <= 300 for chunk in chunks)
    assert "".join(chunks) == "x" * 1000


def test_chunk_segment_fits_one_short_invoice_in_one_chunk_with_page_markers() -> None:
    segment = Segment([page(4, "first"), page(5, "second")])

    assert chunk_segment(segment, 1000) == ["[Page 4]\nfirst\n[Page 5]\nsecond\n"]


def test_chunk_segment_splits_long_invoices_and_keeps_every_line() -> None:
    lines = [f"line {index}" for index in range(500)]
    segment = Segment([page(1, "\n".join(lines[:250])), page(2, "\n".join(lines[250:]))])

    chunks = chunk_segment(segment, 800)

    assert len(chunks) > 1
    assert all(len(chunk) <= 800 for chunk in chunks)
    joined = "\n".join(chunks)
    assert all(line in joined for line in lines)
    assert "[Page 2]" in joined
