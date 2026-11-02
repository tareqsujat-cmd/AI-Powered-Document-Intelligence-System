"""Compare what the system extracted with the labels. No I/O, so that it can be tested alone."""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any

FIELDS = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "customer_name",
    "subtotal",
    "tax",
    "total",
    "currency",
)
AMOUNT_FIELDS = frozenset({"subtotal", "tax", "total"})
LINE_CELLS = ("description", "quantity", "unit_price", "amount")
# Reported to a person as a note, not a problem, so it is not a validation finding.
IGNORED_CODES = frozenset({"no_line_items"})
TOLERANCE = Decimal("0.01")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _text_key(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value))
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", plain).split())


def _number_key(value: Any) -> str:
    return "".join(c for c in str(value).casefold() if c.isalnum())


def values_match(name: str, expected: Any, actual: Any) -> bool:
    """Formatting does not count as an error, and a different value always does."""
    if expected is None or actual is None:
        return expected is None and actual is None
    if name in AMOUNT_FIELDS or name in ("quantity", "unit_price", "amount"):
        first, second = _decimal(expected), _decimal(actual)
        return (
            first is not None
            and second is not None
            and abs(first - second) <= TOLERANCE
        )
    if name == "invoice_number":
        return _number_key(expected) == _number_key(actual)
    if name == "currency":
        return str(expected).upper() == str(actual).upper()
    if name == "invoice_date":
        return str(expected) == str(actual)
    return _text_key(expected) == _text_key(actual)


def classify_field(name: str, expected: Any, actual: Any) -> str:
    """correct, wrong, missing (it was there and we said null), invented, or correct_null."""
    if expected is None:
        return "correct_null" if actual is None else "invented"
    if actual is None:
        return "missing"
    return "correct" if values_match(name, expected, actual) else "wrong"


def match_invoices(
    expected: list[dict[str, Any]], actual: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any] | None]], list[dict[str, Any]]]:
    """Pair by invoice number, then by order, returning the pairs and any invoices left over."""
    remaining = list(actual)
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    unmatched: list[dict[str, Any]] = []
    for label in expected:
        number = label["fields"].get("invoice_number")
        found = next(
            (
                result
                for result in remaining
                if number is not None
                and result["fields"]["invoice_number"]["value"] is not None
                and values_match(
                    "invoice_number",
                    number,
                    result["fields"]["invoice_number"]["value"],
                )
            ),
            None,
        )
        if found is None:
            unmatched.append(label)
            pairs.append((label, None))
        else:
            remaining.remove(found)
            pairs.append((label, found))
    # Whatever could not be paired by number is paired in order, since the number may be what's wrong.
    for index, (label, result) in enumerate(pairs):
        if result is None and remaining:
            pairs[index] = (label, remaining.pop(0))
    return pairs, remaining


def match_line_items(
    expected: list[dict[str, Any]], actual: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Pair each labeled row with the most similar row found, using each found row once."""
    remaining = list(actual)
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    def similarity(label: dict[str, Any], row: dict[str, Any]) -> float:
        words = SequenceMatcher(
            None,
            _text_key(label.get("description") or ""),
            _text_key(row.get("description") or ""),
        ).ratio()
        same_amount = values_match("amount", label.get("amount"), row.get("amount"))
        return words + (1.0 if same_amount else 0.0)

    for label in expected:
        best = max(remaining, key=lambda row: similarity(label, row), default=None)
        if best is not None and similarity(label, best) >= 0.5:
            remaining.remove(best)
            pairs.append((label, best))
        else:
            pairs.append((label, None))
    return pairs


@dataclass
class Score:
    """Running totals across documents."""

    fields: Counter[str] = field(default_factory=Counter)
    by_field: dict[str, Counter[str]] = field(default_factory=dict)
    expected_rows: int = 0
    found_rows: int = 0
    exact_rows: int = 0
    cells: Counter[str] = field(default_factory=Counter)
    invoices_expected: int = 0
    invoices_found: int = 0
    invoices_extra: int = 0
    invoices_all_fields_right: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def field_accuracy(self) -> float | None:
        """Share of fields handled correctly, including correctly saying a value is absent."""
        total = sum(self.fields.values())
        return (
            (self.fields["correct"] + self.fields["correct_null"]) / total
            if total
            else None
        )

    @property
    def value_accuracy(self) -> float | None:
        """Share of the values that exist which were read correctly. The harder number."""
        present = self.fields["correct"] + self.fields["wrong"] + self.fields["missing"]
        return self.fields["correct"] / present if present else None

    @property
    def line_precision(self) -> float | None:
        return self.exact_rows / self.found_rows if self.found_rows else None

    @property
    def line_recall(self) -> float | None:
        return self.exact_rows / self.expected_rows if self.expected_rows else None

    @property
    def cell_accuracy(self) -> float | None:
        total = sum(self.cells.values())
        return self.cells["correct"] / total if total else None

    def add_invoice(
        self, document: str, label: dict[str, Any], result: dict[str, Any] | None
    ) -> None:
        self.invoices_expected += 1
        all_right = result is not None
        if result is not None:
            self.invoices_found += 1
        for name in FIELDS:
            expected = label["fields"].get(name)
            actual = result["fields"][name]["value"] if result is not None else None
            outcome = classify_field(name, expected, actual)
            self.fields[outcome] += 1
            self.by_field.setdefault(name, Counter())[outcome] += 1
            if outcome not in ("correct", "correct_null"):
                all_right = False
                self.errors.append(
                    {
                        "document": document,
                        "field": name,
                        "outcome": outcome,
                        "expected": expected,
                        "actual": actual,
                    }
                )
        if all_right:
            self.invoices_all_fields_right += 1

        expected_rows = label.get("line_items", [])
        found_rows = result["line_items"] if result is not None else []
        self.expected_rows += len(expected_rows)
        self.found_rows += len(found_rows)
        for row, found in match_line_items(expected_rows, found_rows):
            cells_right = True
            for cell in LINE_CELLS:
                expected = row.get(cell)
                actual = found.get(cell) if found is not None else None
                right = values_match(cell, expected, actual)
                self.cells["correct" if right else "wrong"] += 1
                cells_right = cells_right and right
            if found is not None and cells_right:
                self.exact_rows += 1


@dataclass
class IssueScore:
    """Validation findings as a detection problem: did it flag what was seeded, and only that?"""

    true_positives: Counter[str] = field(default_factory=Counter)
    false_positives: Counter[str] = field(default_factory=Counter)
    false_negatives: Counter[str] = field(default_factory=Counter)
    review_true_positive: int = 0
    review_false_positive: int = 0
    review_false_negative: int = 0
    review_true_negative: int = 0
    mistakes: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        document: str,
        expected: set[str],
        actual: set[str],
        expected_review: bool,
        actual_review: bool,
    ) -> None:
        for code in expected & actual:
            self.true_positives[code] += 1
        for code in actual - expected:
            self.false_positives[code] += 1
            self.mistakes.append(
                {"document": document, "kind": "false alarm", "code": code}
            )
        for code in expected - actual:
            self.false_negatives[code] += 1
            self.mistakes.append({"document": document, "kind": "missed", "code": code})
        if expected_review and actual_review:
            self.review_true_positive += 1
        elif actual_review:
            self.review_false_positive += 1
        elif expected_review:
            self.review_false_negative += 1
        else:
            self.review_true_negative += 1

    @staticmethod
    def _ratio(top: int, bottom: int) -> float | None:
        return top / bottom if bottom else None

    @property
    def precision(self) -> float | None:
        found = sum(self.true_positives.values())
        return self._ratio(found, found + sum(self.false_positives.values()))

    @property
    def recall(self) -> float | None:
        found = sum(self.true_positives.values())
        return self._ratio(found, found + sum(self.false_negatives.values()))

    @property
    def review_precision(self) -> float | None:
        return self._ratio(
            self.review_true_positive,
            self.review_true_positive + self.review_false_positive,
        )

    @property
    def review_recall(self) -> float | None:
        return self._ratio(
            self.review_true_positive,
            self.review_true_positive + self.review_false_negative,
        )


def actual_issue_codes(results: list[dict[str, Any]]) -> set[str]:
    """The codes found across a document's invoices. Severity info carries no warning to a person,
    except a verified cross-check, which is a claim that can be right or wrong."""
    codes: set[str] = set()
    for result in results:
        for issue in result["validation_errors"]:
            if issue["code"] in IGNORED_CODES:
                continue
            if (
                issue["severity"] != "info"
                or issue["code"] == "cross_document_verified"
            ):
                codes.add(issue["code"])
    return codes
