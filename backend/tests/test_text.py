import pytest

from app.processing.text import clean_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\x00b", "ab"),
        ("a\x07b\x1fc", "abc"),
        ("a\r\nb\rc", "a\nb\nc"),
        ("a    b", "a b"),
        ("  a  \n  b  ", "a\nb"),
        ("a\n\n\n\n\nb", "a\n\nb"),
        ("a\tb", "a\tb"),
        ("", ""),
        ("  \n\t\x00 ", ""),
        ("é", "é"),
    ],
)
def test_clean_text(raw: str, expected: str) -> None:
    assert clean_text(raw) == expected


def test_clean_text_keeps_currency_symbols_and_non_latin_text() -> None:
    text = "Total: €1.234,56 — ৳500 — 請求書"

    assert clean_text(text) == text
