import re
import unicodedata

# Everything in Unicode category Cc except tab and newline. PostgreSQL TEXT rejects NUL outright.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_HORIZONTAL_SPACE = re.compile(r"[^\S\n\t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """Make extracted text safe to store and consistent to search."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_CHARS.sub("", text)
    text = _HORIZONTAL_SPACE.sub(" ", text)
    text = "\n".join(line.strip(" ") for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()
