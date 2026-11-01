import calendar
import hashlib
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

_NUMBER_BODY = re.compile(r"^\d+(?:[.,'  ]\d+)*$")
# Groups of three, or South Asian grouping such as 12,34,567. The last group always has three.
_GROUPED_DIGITS = re.compile(r"^\d{1,3}(?:[.,]\d{2,3})*[.,]\d{3}$|^\d+$")
# Fits the NUMERIC(18,4) money columns. Anything longer is not a real amount.
_MAX_INTEGER_DIGITS = 14
# Characters OCR commonly confuses. Only used to compare numbers, never to change stored values.
_OCR_CONFUSABLE = str.maketrans({"o": "0", "i": "1", "l": "1", "s": "5", "b": "8", "z": "2"})
_LEGAL_SUFFIXES = re.compile(
    r"\b(ltd|limited|llc|inc|incorporated|corp|corporation|gmbh|plc|co|company|sa|srl|bv|pvt)\b"
)

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTHS["sept"] = 9
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO_DATE = re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})\b")
_DAY_MONTH_NAME = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s\-/.]+({_MONTH_PATTERN})\.?,?[\s\-/.]+(\d{{4}}|\d{{2}})\b",
    re.IGNORECASE,
)
_MONTH_NAME_DAY = re.compile(
    rf"\b({_MONTH_PATTERN})\.?[\s\-/.]+(\d{{1,2}})(?:st|nd|rd|th)?,?[\s\-/.]+(\d{{4}}|\d{{2}})\b",
    re.IGNORECASE,
)


def _strip_amount_decoration(text: str) -> tuple[str, bool]:
    cleaned = text.strip()
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative, cleaned = True, cleaned[1:-1]
    if re.search(r"\b(cr|dr)\b\.?$", cleaned, re.IGNORECASE):
        negative = negative or cleaned.lower().rstrip(".").endswith("cr")
        cleaned = cleaned[:-3] if cleaned.endswith(".") else cleaned[:-2]
    # Currency symbols and codes on either side.
    cleaned = re.sub(r"^[^\d\-+]+|[^\d\-+]+$", "", cleaned.strip())
    if cleaned.endswith("-"):
        negative, cleaned = True, cleaned[:-1]
    if cleaned.startswith("-"):
        negative, cleaned = True, cleaned[1:]
    cleaned = cleaned.removeprefix("+")
    # A currency symbol can sit inside a sign, as in "-$100.00".
    cleaned = re.sub(r"^[^\d]+", "", cleaned)
    return cleaned, negative


def _to_decimal(digits: str, negative: bool) -> Decimal | None:
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    if len(digits.split(".")[0].lstrip("0")) > _MAX_INTEGER_DIGITS:
        return None
    return -value if negative else value


def amount_candidates(text: str) -> list[Decimal]:
    """Every reading of a printed amount, the likeliest first.

    "1.234" is 1.234 in the US and 1234 in Germany, and only the document's context can settle it,
    so callers verifying a value against a source must accept either reading.
    """
    body, negative = _strip_amount_decoration(text)
    if not body or not _NUMBER_BODY.match(body):
        return []
    grouped = re.sub(r"['  ]", "", body)
    has_comma, has_dot = "," in grouped, "." in grouped

    readings: list[str] = []
    if has_comma and has_dot:
        last = max(grouped.rfind(","), grouped.rfind("."))
        whole = grouped[:last]
        # The decimal mark appears once, and everything before it is cleanly grouped.
        if grouped[last] in whole or not _GROUPED_DIGITS.match(whole):
            return []
        readings.append(re.sub(r"[.,]", "", whole) + "." + grouped[last + 1 :])
    elif has_comma or has_dot:
        separator = "," if has_comma else "."
        parts = grouped.split(separator)
        if len(parts) > 2:
            if not _GROUPED_DIGITS.match(grouped):
                return []
            readings.append("".join(parts))
        elif len(parts[1]) == 3 and parts[0] != "0":
            decimal_reading = f"{parts[0]}.{parts[1]}"
            whole_reading = parts[0] + parts[1]
            # A comma before three digits is usually thousands; a dot is usually a decimal point.
            readings = (
                [whole_reading, decimal_reading] if has_comma else [decimal_reading, whole_reading]
            )
        else:
            readings.append(f"{parts[0]}.{parts[1]}")
    else:
        readings.append(grouped)

    values = [_to_decimal(reading, negative) for reading in readings]
    return [value for value in values if value is not None]


def parse_amount(text: str | None) -> Decimal | None:
    if not text:
        return None
    candidates = amount_candidates(text)
    return candidates[0] if candidates else None


_NUMBER_IN_TEXT = re.compile(r"\d[\d.,'  ]*\d|\d")


def amounts_in_text(text: str) -> set[Decimal]:
    """Every number a text could be read as containing, for checking a value against a source."""
    found: set[Decimal] = set()
    for match in _NUMBER_IN_TEXT.finditer(text):
        token = match.group()
        found.update(amount_candidates(token))
        # "4 312.50" may be a quantity and a price in adjacent columns, not one number.
        for part in token.split():
            found.update(amount_candidates(part))
    return found


def _full_year(year: int) -> int:
    return year + 2000 if year < 100 else year


def _build_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(_full_year(year), month, day)
    except ValueError:
        return None


def dates_in_text(text: str) -> list[tuple[date, bool]]:
    """Every date a text could contain, as (date, ambiguous).

    03/04/2026 is 3 April or 4 March depending on the country. Both are returned and marked
    ambiguous, because only a human or the wider document can tell.
    """
    results: list[tuple[date, bool]] = []
    for match in _ISO_DATE.finditer(text):
        parsed = _build_date(int(match[1]), int(match[2]), int(match[3]))
        if parsed:
            results.append((parsed, False))
    for match in _DAY_MONTH_NAME.finditer(text):
        parsed = _build_date(int(match[3]), _MONTHS[match[2].lower()], int(match[1]))
        if parsed:
            results.append((parsed, False))
    for match in _MONTH_NAME_DAY.finditer(text):
        parsed = _build_date(int(match[3]), _MONTHS[match[1].lower()], int(match[2]))
        if parsed:
            results.append((parsed, False))
    for match in _NUMERIC_DATE.finditer(text):
        first, second, year = int(match[1]), int(match[2]), int(match[3])
        readings = {_build_date(year, second, first), _build_date(year, first, second)} - {None}
        ambiguous = len(readings) > 1
        results.extend((reading, ambiguous) for reading in readings if reading)
    return results


def parse_date(text: str | None) -> date | None:
    """Parse a date only if it has exactly one meaning, or is written year first."""
    if not text:
        return None
    found = dates_in_text(text)
    if not found:
        return None
    distinct = {value for value, _ in found}
    return found[0][0] if len(distinct) == 1 else None


def _fold(text: str) -> str:
    """Lowercase, with accents removed, so that "Müller" and "Muller" compare equal."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def normalize_key(text: str | None) -> str:
    """Lowercase letters and digits only, so "INV-0042" and "inv 0042" compare equal."""
    if not text:
        return ""
    return "".join(character for character in _fold(text) if character.isalnum())


def ocr_tolerant_key(text: str | None) -> str:
    """Like normalize_key, but also equates characters that OCR commonly confuses."""
    return normalize_key(text).translate(_OCR_CONFUSABLE)


def normalize_party_name(text: str | None) -> str:
    """Compare companies by name, ignoring case, punctuation, and legal form."""
    if not text:
        return ""
    words = re.sub(r"[^\w\s]", " ", _fold(text))
    return " ".join(_LEGAL_SUFFIXES.sub(" ", words).split())


def duplicate_key(
    supplier: str | None, number: str | None, invoice_date: date | None, total: Decimal | None
) -> str | None:
    """The hash that identifies one invoice. None unless all four parts are known."""
    supplier_key, number_key = normalize_party_name(supplier), normalize_key(number)
    if not (supplier_key and number_key and invoice_date and total is not None):
        return None
    parts = (supplier_key, number_key, invoice_date.isoformat(), f"{total.normalize():f}")
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
