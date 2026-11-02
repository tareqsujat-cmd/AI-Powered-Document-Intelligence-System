"""Write report.md from the measured results, so that no number in it is typed by hand.

Run: python eval/build_report.py [--runs baseline other ...]
The first run is reported in full; any others are compared with it. Hand-written commentary,
if any, comes from notes.md and is included as it is.
"""

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
RESULTS = ROOT / "results"

CATEGORY_NAMES = {
    "digital_pdf": "Digital PDF",
    "scanned_pdf": "Scanned PDF",
    "image": "Image (PNG, JPG, TIFF)",
    "low_quality": "Low-quality image",
    "spreadsheet": "Spreadsheet (CSV, XLSX, XLS)",
    "seeded_error": "Seeded validation problems",
    "corrupted": "Corrupted or disguised",
}


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def sec(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} s"


def ratio(top: int, bottom: int) -> str:
    return f"{top}/{bottom}"


def table(headings: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headings) + " |",
        "|" + "|".join("---" for _ in headings) + "|",
    ]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def load(name: str) -> dict[str, Any]:
    return json.loads((RESULTS / f"{name}.json").read_text(encoding="utf-8"))


def dataset_section(labels: list[dict[str, Any]]) -> str:
    documents = Counter(label["category"] for label in labels)
    invoices = Counter()
    rows = Counter()
    for label in labels:
        invoices[label["category"]] += len(label["invoices"])
        rows[label["category"]] += sum(
            len(inv["line_items"]) for inv in label["invoices"]
        )
    body = [
        [CATEGORY_NAMES.get(name, name), documents[name], invoices[name], rows[name]]
        for name in CATEGORY_NAMES
        if documents[name]
    ]
    body.append(
        [
            "**All**",
            sum(documents.values()),
            sum(invoices.values()),
            sum(rows.values()),
        ]
    )
    return table(
        ["Category", "Documents", "Labeled invoices", "Labeled line items"], body
    )


def headline(run: dict[str, Any]) -> list[list[str]]:
    overall = run["extraction"]["overall"]
    outcomes = overall["outcomes"]
    validation = run["validation"]
    review = validation["review"]
    result = run["outcomes"]
    timing = run["timing"]
    present = (
        outcomes.get("correct", 0)
        + outcomes.get("wrong", 0)
        + outcomes.get("missing", 0)
    )
    absent = outcomes.get("correct_null", 0) + outcomes.get("invented", 0)
    return [
        [
            "Field value accuracy",
            pct(overall["value_accuracy"]),
            f"{outcomes.get('correct', 0)} of {present} printed values read correctly",
        ],
        [
            "Field accuracy, absent fields included",
            pct(overall["field_accuracy"]),
            "also counts saying null when the value is not there",
        ],
        [
            "Invented values",
            str(outcomes.get("invented", 0)),
            f"out of {absent} fields that were not printed",
        ],
        [
            "Invoices with every field right",
            ratio(overall["invoices_all_fields_right"], overall["invoices_expected"]),
            f"{overall['invoices_extra']} extra invoices reported that do not exist",
        ],
        [
            "Line items: precision / recall",
            f"{pct(overall['line_precision'])} / {pct(overall['line_recall'])}",
            (
                f"{overall['exact_rows']} rows exactly right, of {overall['found_rows']} found "
                f"and {overall['expected_rows']} labeled"
            ),
        ],
        [
            "Line-item cell accuracy",
            pct(overall["cell_accuracy"]),
            "description, quantity, unit price and amount",
        ],
        [
            "Validation findings: precision / recall",
            f"{pct(validation['precision'])} / {pct(validation['recall'])}",
            (
                f"{sum(validation['true_positives'].values())} right, "
                f"{sum(validation['false_positives'].values())} false alarms, "
                f"{sum(validation['false_negatives'].values())} missed"
            ),
        ],
        [
            "Needs-review flag: precision / recall",
            f"{pct(review['precision'])} / {pct(review['recall'])}",
            (
                f"{review['true_positive']} flagged rightly, {review['false_positive']} wrongly, "
                f"{review['false_negative']} missed"
            ),
        ],
        [
            "Success rate (valid documents completed)",
            pct(result["success_rate"]),
            ratio(result["valid_completed"], result["valid_documents"]),
        ],
        [
            "Failure rate (valid documents failed)",
            pct(result["failure_rate"]),
            ratio(
                result["valid_documents"] - result["valid_completed"],
                result["valid_documents"],
            ),
        ],
        [
            "Bad files handled as expected",
            ratio(result["bad_handled_as_expected"], result["bad_documents"]),
            "every one with a reason"
            if result["bad_all_have_reasons"]
            else "**some without a reason**",
        ],
        [
            "Processing time per document: median / p95",
            (
                f"{sec(timing['seconds_per_document']['median'])} / "
                f"{sec(timing['seconds_per_document']['p95'])}"
            ),
            (
                f"mean {sec(timing['seconds_per_document']['mean'])}, "
                f"max {sec(timing['seconds_per_document']['max'])}"
            ),
        ],
        [
            "Processing time per page: median",
            sec(timing["seconds_per_page"]["median"]),
            (
                f"mean {sec(timing['seconds_per_page']['mean'])}, "
                f"{timing['pages_completed']} pages"
            ),
        ],
        [
            "Wall time for the whole set",
            sec(timing["wall_seconds"]),
            "upload to last document finished",
        ],
    ]


def extraction_by_category(run: dict[str, Any]) -> str:
    rows = []
    for name, score in run["extraction"]["by_category"].items():
        rows.append(
            [
                CATEGORY_NAMES.get(name, name),
                ratio(score["invoices_found"], score["invoices_expected"]),
                pct(score["value_accuracy"]),
                score["outcomes"].get("invented", 0),
                pct(score["line_precision"]),
                pct(score["line_recall"]),
                ratio(score["invoices_all_fields_right"], score["invoices_expected"]),
            ]
        )
    return table(
        [
            "Category",
            "Invoices found",
            "Value accuracy",
            "Invented",
            "Line precision",
            "Line recall",
            "All fields right",
        ],
        rows,
    )


def extraction_by_field(run: dict[str, Any]) -> str:
    rows = []
    for name, counts in run["extraction"]["overall"]["by_field"].items():
        present = (
            counts.get("correct", 0) + counts.get("wrong", 0) + counts.get("missing", 0)
        )
        rows.append(
            [
                f"`{name}`",
                pct(counts.get("correct", 0) / present if present else None),
                counts.get("correct", 0),
                counts.get("wrong", 0),
                counts.get("missing", 0),
                counts.get("correct_null", 0),
                counts.get("invented", 0),
            ]
        )
    return table(
        ["Field", "Accuracy", "Correct", "Wrong", "Missed", "Rightly null", "Invented"],
        rows,
    )


def validation_by_code(run: dict[str, Any]) -> str:
    validation = run["validation"]
    codes = sorted(
        set(validation["true_positives"])
        | set(validation["false_positives"])
        | set(validation["false_negatives"])
    )
    rows = [
        [
            f"`{code}`",
            validation["true_positives"].get(code, 0),
            validation["false_positives"].get(code, 0),
            validation["false_negatives"].get(code, 0),
        ]
        for code in codes
    ]
    return table(["Code", "Found rightly", "False alarm", "Missed"], rows)


def mistakes_list(run: dict[str, Any]) -> str:
    mistakes = run["validation"]["mistakes"]
    if not mistakes:
        return "None."
    return table(
        ["Document", "Kind", "Code"],
        [[f"`{m['document']}`", m["kind"], f"`{m['code']}`"] for m in mistakes],
    )


def bad_files(run: dict[str, Any]) -> str:
    rows = [
        [
            f"`{row['file']}`",
            row["expected"],
            row["actual"],
            row["reason"] or "**none**",
        ]
        for row in run["outcomes"]["bad_rows"]
    ]
    return table(["File", "Expected", "Actual", "Reason given"], rows)


def unexpected(run: dict[str, Any]) -> str:
    rows = [
        [f"`{row['file']}`", row["expected"], row["actual"], row["reason"] or ""]
        for row in run["outcomes"]["unexpected"]
    ]
    return table(["File", "Expected", "Actual", "Reason"], rows) if rows else "None."


def timing_by_category(run: dict[str, Any]) -> str:
    timing = run["timing"]
    rows = []
    for name, summary in timing["seconds_per_document_by_category"].items():
        page = timing["seconds_per_page_by_category"][name]
        rows.append(
            [
                CATEGORY_NAMES.get(name, name),
                summary["count"],
                sec(summary["median"]),
                sec(summary["p95"]),
                sec(summary["max"]),
                sec(page["median"]),
            ]
        )
    return table(
        ["Category", "Documents", "Median", "p95", "Max", "Median per page"],
        rows,
    )


def timing_by_stage(run: dict[str, Any]) -> str:
    rows = [
        [
            f"`{name}`",
            summary["count"],
            sec(summary["median"]),
            sec(summary["p95"]),
            sec(summary["max"]),
        ]
        for name, summary in run["timing"]["seconds_per_stage"].items()
    ]
    return table(["Stage", "Runs", "Median", "p95", "Max"], rows)


def search_section(run: dict[str, Any]) -> str:
    search = run["search"]
    rows = []
    for mode in ("keyword", "semantic", "hybrid"):
        meaning, words, number = (
            search["by_meaning"][mode],
            search["by_words"][mode],
            search["by_number"][mode],
        )
        rows.append(
            [
                mode,
                pct(meaning["top_result_right"]),
                pct(meaning["precision_at_3"]),
                pct(words["top_result_right"]),
                pct(number["top_result_right"]),
                search["unrelated_query_results"][mode],
            ]
        )
    latency = search["latency_seconds"]
    return (
        table(
            [
                "Mode",
                f"By meaning: top hit right ({search['by_meaning']['hybrid']['queries']} queries)",
                "By meaning: precision@3",
                f"By the invoice's words: top hit right ({search['by_words']['hybrid']['queries']})",
                f"By invoice number: top hit right ({search['by_number']['hybrid']['queries']})",
                "Results for an unrelated query",
            ],
            rows,
        )
        + f"\n\nSearch latency over {latency['count']} queries: median "
        f"{latency['median'] * 1000:.0f} ms, p95 {latency['p95'] * 1000:.0f} ms."
    )


def extraction_errors(run: dict[str, Any]) -> str:
    errors = run["extraction"]["errors"]
    if not errors:
        return "None."
    return table(
        ["Document", "Field", "Outcome", "Expected", "Got"],
        [
            [
                f"`{error['document']}`",
                f"`{error['field']}`",
                error["outcome"],
                "" if error["expected"] is None else f"`{error['expected']}`",
                "" if error["actual"] is None else f"`{error['actual']}`",
            ]
            for error in errors
        ],
    )


def throughput_section() -> str | None:
    path = RESULTS / "throughput.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [
        [
            name.replace("_", " "),
            result["total_pages"],
            sec(result["span_seconds"]),
            f"{result['pages_per_minute']:.1f}",
            ", ".join(
                f"{value:.2f}" for value in result["seconds_per_page_per_document"]
            ),
        ]
        for name, result in data.items()
    ]
    return table(
        [
            "Load",
            "Scanned pages",
            "Text extraction span",
            "Pages per minute",
            "Seconds per page, each document",
        ],
        rows,
    )


def comparison(runs: list[dict[str, Any]]) -> str:
    names = [run["name"] for run in runs]
    rows = []
    for index, (label, *_rest) in enumerate(headline(runs[0])):
        rows.append([label, *(headline(run)[index][1] for run in runs)])
    models = [", ".join(run["models"]) or "none" for run in runs]
    return table(
        ["Measure", *(f"{n} ({m})" for n, m in zip(names, models, strict=True))], rows
    )


def build(names: list[str]) -> str:
    runs = [load(name) for name in names]
    main = runs[0]
    labels = json.loads((ROOT / "labels.json").read_text(encoding="utf-8"))["documents"]
    parts = [
        "# Evaluation report",
        "",
        "Generated by `eval/build_report.py` from "
        + ", ".join(f"`eval/results/{run['name']}.json`" for run in runs)
        + ". Every number in the tables comes from those files; none was typed by hand. "
        "The commentary is written by hand, in `eval/notes.md`.",
        "",
        (
            f"- Run: `{main['name']}`, measured {main['measured_at'][:16].replace('T', ' ')} UTC, "
            f"at commit `{main['commit']}`"
        ),
        (
            f"- LLM: {', '.join(f'`{m}`' for m in main['models']) or 'none'}; OCR: Tesseract with "
            "OpenCV preprocessing; embeddings: all-MiniLM-L6-v2"
        ),
        (
            "- Through the real API on a fresh database, with real OCR and real LLM calls. "
            "Nothing is mocked."
        ),
        (
            "- Hardware: one laptop (Intel i5-8350U, 4 cores, 16 GB), Docker Desktop on Windows, "
            "one Celery worker container running two processes."
        ),
        "",
        "## Dataset",
        "",
        (
            "Synthetic invoices from `eval/generate_dataset.py` (deterministic), with their right "
            "answers in `eval/labels.json`. Seeded-problem documents carry the validation codes they "
            "should raise; corrupted ones carry the outcome they should get."
        ),
        "",
        dataset_section(labels),
        "",
        "## Results",
        "",
        table(["Measure", "Result", "Detail"], headline(main)),
        "",
        "How it is scored (`eval/scoring.py`, tested by `eval/test_scoring.py`):",
        "",
        (
            "- A field is *correct* if it matches after formatting is ignored (amounts within 0.01, "
            "invoice numbers without punctuation, names without case or accents). A printed value "
            "that came back null is *missed*; a value returned for a field that is not printed is "
            "*invented*."
        ),
        (
            "- A line item counts only if every cell (description, quantity, unit price, amount) is "
            "right."
        ),
        (
            "- Validation is scored per document: codes raised against codes seeded. `no_line_items` "
            "is a note, not a finding, so it is not counted."
        ),
        (
            "- Success and failure rates cover the documents that should succeed. Corrupted files "
            "are counted separately, by whether they failed or were refused as expected."
        ),
    ]
    notes = ROOT / "notes.md"
    if notes.exists():
        parts += ["", notes.read_text(encoding="utf-8").strip()]
    parts += [
        "",
        "## Extraction by category",
        "",
        extraction_by_category(main),
        "",
        "## Extraction by field",
        "",
        extraction_by_field(main),
        "",
        "## Validation by code",
        "",
        validation_by_code(main),
        "",
        "Validation mistakes:",
        "",
        mistakes_list(main),
        "",
        "## Bad files",
        "",
        bad_files(main),
        "",
        "Documents whose outcome differed from the label:",
        "",
        unexpected(main),
        "",
        "## Processing time",
        "",
        timing_by_category(main),
        "",
        "By pipeline stage:",
        "",
        timing_by_stage(main),
        "",
        "Where page text came from, per the metrics endpoint: "
        + ", ".join(
            f"{source.replace('_', ' ')} {count}"
            for source, count in main["metrics_endpoint"]["pages_by_source"].items()
        )
        + ".",
        "",
        (
            "The LLM stage is paced to the provider's free-tier quota "
            "(`LLM_REQUESTS_PER_MINUTE`), so with many documents queued most of the wall time is "
            "waiting for a request slot, not computing."
        ),
    ]
    throughput = throughput_section()
    if throughput:
        parts += [
            "",
            "## OCR throughput",
            "",
            (
                "Scanned 40-page PDFs, text extraction only (`eval/throughput.py`), one document "
                "alone and then two at once."
            ),
            "",
            throughput,
        ]
    parts += ["", "## Search", "", search_section(main)]
    if len(runs) > 1:
        parts += ["", "## Compared with other runs", "", comparison(runs)]
    parts += [
        "",
        "## Every extraction error",
        "",
        extraction_errors(main),
        "",
        "## Reproduce",
        "",
        (
            "See [README.md](README.md). Results differ a little between runs, because the LLM is not "
            "fully deterministic."
        ),
        "",
    ]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", nargs="+", default=["baseline"])
    args = parser.parse_args()
    (ROOT / "report.md").write_text(build(args.runs), encoding="utf-8")
    print(f"Wrote {ROOT / 'report.md'}")


if __name__ == "__main__":
    main()
