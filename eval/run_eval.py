"""Measure the running system against the labels, through its real API, with nothing faked.

Needs a stack with an empty database. See eval/README.md.

Run: python eval/run_eval.py --api http://127.0.0.1:8001 --name default
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from generate_dataset import CATALOG
from scoring import IssueScore, Score, actual_issue_codes, match_invoices

FINISHED = {"completed", "failed"}
POLL_SECONDS = 1.0
TIMEOUT_SECONDS = 3600

# Worded so as not to share key words with the invoices, because that is what semantic search is for.
MEANING_QUERIES = {
    "machinery": "replacement parts for factory equipment",
    "software": "IT services and cloud licences",
    "catering": "food and drinks for the office",
    "logistics": "shipping and transport costs",
    "printing": "marketing materials and stationery",
}
# The words of the invoices themselves, which keyword search should handle.
WORD_QUERIES = {
    "machinery": "hydraulic bearing",
    "software": "hosting subscription",
    "catering": "coffee platter",
    "logistics": "freight container",
    "printing": "brochure printing",
}


def percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * share), len(ordered) - 1)]


def summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean": statistics.mean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def score_dict(score: Score) -> dict[str, Any]:
    return {
        "field_accuracy": score.field_accuracy,
        "value_accuracy": score.value_accuracy,
        "outcomes": dict(score.fields),
        "by_field": {name: dict(counts) for name, counts in score.by_field.items()},
        "line_precision": score.line_precision,
        "line_recall": score.line_recall,
        "cell_accuracy": score.cell_accuracy,
        "expected_rows": score.expected_rows,
        "found_rows": score.found_rows,
        "exact_rows": score.exact_rows,
        "invoices_expected": score.invoices_expected,
        "invoices_found": score.invoices_found,
        "invoices_extra": score.invoices_extra,
        "invoices_all_fields_right": score.invoices_all_fields_right,
    }


class Run:
    def __init__(self, api: str) -> None:
        self.base = f"{api.rstrip('/')}/api/v1"
        # No reused connections: after a long wait, the server can close an idle one just as the
        # client sends on it, which fails the request for no reason to do with the system measured.
        self.http = httpx.Client(
            timeout=120, limits=httpx.Limits(max_keepalive_connections=0)
        )
        self.documents: dict[str, dict[str, Any]] = {}

    def upload(self, label: dict[str, Any]) -> None:
        path = ROOT / "dataset" / label["file"]
        with path.open("rb") as stream:
            response = self.http.post(
                f"{self.base}/documents", files=[("files", (path.name, stream))]
            )
        response.raise_for_status()
        outcome = response.json()["results"][0]
        self.documents[label["file"]] = {
            "label": label,
            "accepted": outcome["accepted"],
            "id": outcome["document"]["id"] if outcome["document"] else None,
            "upload_error": outcome["error"],
        }

    def wait(self, files: list[str]) -> None:
        pending = {file for file in files if self.documents[file]["id"] is not None}
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while pending and time.monotonic() < deadline:
            for file in list(pending):
                status = self.http.get(
                    f"{self.base}/documents/{self.documents[file]['id']}/status"
                ).json()
                if status["status"] in FINISHED:
                    pending.discard(file)
            print(f"  {len(pending)} still processing", flush=True)
            if pending:
                time.sleep(POLL_SECONDS * max(1, min(len(pending), 5)))
        if pending:
            print(f"TIMED OUT waiting for: {sorted(pending)}", flush=True)

    def collect(self) -> None:
        for entry in self.documents.values():
            if entry["id"] is None:
                entry["status"] = "rejected"
                entry["results"] = []
                continue
            detail = self.http.get(f"{self.base}/documents/{entry['id']}").json()
            entry["detail"] = detail
            entry["status"] = detail["status"]
            entry["results"] = self.http.get(
                f"{self.base}/documents/{entry['id']}/results"
            ).json()
            entry["history"] = self.http.get(
                f"{self.base}/documents/{entry['id']}/history"
            ).json()


def processing_seconds(detail: dict[str, Any]) -> float | None:
    started, finished = (
        detail.get("processing_started_at"),
        detail.get("processing_finished_at"),
    )
    if not started or not finished:
        return None
    return (
        datetime.fromisoformat(finished) - datetime.fromisoformat(started)
    ).total_seconds()


def score_extraction(run: Run) -> dict[str, Any]:
    overall = Score()
    per_category: dict[str, Score] = {}
    for file, entry in run.documents.items():
        label = entry["label"]
        if label["expected_outcome"] != "completed":
            continue
        category = per_category.setdefault(label["category"], Score())
        pairs, extra = match_invoices(label["invoices"], entry["results"])
        for score in (overall, category):
            score.invoices_extra += len(extra)
        for invoice_label, result in pairs:
            for score in (overall, category):
                # Errors are kept once, on the overall score.
                before = len(score.errors)
                score.add_invoice(file, invoice_label, result)
                if score is category:
                    del score.errors[before:]
    return {
        "overall": score_dict(overall),
        "by_category": {
            name: score_dict(score) for name, score in sorted(per_category.items())
        },
        "errors": overall.errors,
    }


def score_validation(run: Run) -> dict[str, Any]:
    score = IssueScore()
    for file, entry in run.documents.items():
        label = entry["label"]
        if label["expected_outcome"] != "completed" or entry["status"] != "completed":
            continue
        expected = set(label["expected_issues"])
        actual = actual_issue_codes(entry["results"])
        # Being verified is good news, so only the other findings should flag it for a person.
        expected_review = bool(expected - {"cross_document_verified"})
        score.add(
            file, expected, actual, expected_review, entry["detail"]["needs_review"]
        )
    return {
        "precision": score.precision,
        "recall": score.recall,
        "true_positives": dict(score.true_positives),
        "false_positives": dict(score.false_positives),
        "false_negatives": dict(score.false_negatives),
        "review": {
            "precision": score.review_precision,
            "recall": score.review_recall,
            "true_positive": score.review_true_positive,
            "false_positive": score.review_false_positive,
            "false_negative": score.review_false_negative,
            "true_negative": score.review_true_negative,
        },
        "mistakes": score.mistakes,
    }


def score_outcomes(run: Run) -> dict[str, Any]:
    rows = []
    for file, entry in run.documents.items():
        expected = entry["label"]["expected_outcome"]
        rows.append(
            {
                "file": file,
                "category": entry["label"]["category"],
                "expected": expected,
                "actual": entry["status"],
                "reason": (
                    entry.get("detail", {}).get("failure_reason")
                    or (entry["upload_error"] or {}).get("reason")
                ),
            }
        )
    valid = [row for row in rows if row["expected"] == "completed"]
    bad = [row for row in rows if row["expected"] != "completed"]
    completed = sum(row["actual"] == "completed" for row in valid)
    handled = sum(row["actual"] == row["expected"] for row in bad)
    return {
        "total_documents": len(rows),
        "valid_documents": len(valid),
        "valid_completed": completed,
        "success_rate": completed / len(valid) if valid else None,
        "failure_rate": (len(valid) - completed) / len(valid) if valid else None,
        "bad_documents": len(bad),
        "bad_handled_as_expected": handled,
        "bad_all_have_reasons": all(row["reason"] for row in bad),
        "unexpected": [row for row in rows if row["actual"] != row["expected"]],
        "bad_rows": bad,
    }


def score_timing(run: Run, wall_seconds: float) -> dict[str, Any]:
    by_category: dict[str, list[float]] = {}
    per_page: dict[str, list[float]] = {}
    stages: dict[str, list[float]] = {}
    pages = 0
    for entry in run.documents.values():
        if entry["status"] != "completed":
            continue
        seconds = processing_seconds(entry["detail"])
        page_count = entry["detail"]["page_count"] or 1
        pages += page_count
        category = entry["label"]["category"]
        if seconds is not None:
            by_category.setdefault(category, []).append(seconds)
            per_page.setdefault(category, []).append(seconds / page_count)
        for event in entry["history"]:
            if event["event"] == "stage_completed" and event["duration_ms"] is not None:
                stages.setdefault(event["stage"], []).append(
                    event["duration_ms"] / 1000
                )
    all_docs = [value for values in by_category.values() for value in values]
    all_pages = [value for values in per_page.values() for value in values]
    return {
        "wall_seconds": wall_seconds,
        "pages_completed": pages,
        "seconds_per_document": summary(all_docs),
        "seconds_per_page": summary(all_pages),
        "seconds_per_document_by_category": {
            k: summary(v) for k, v in sorted(by_category.items())
        },
        "seconds_per_page_by_category": {
            k: summary(v) for k, v in sorted(per_page.items())
        },
        "seconds_per_stage": {k: summary(v) for k, v in sorted(stages.items())},
    }


def subjects_of(label: dict[str, Any]) -> set[str]:
    """A document is about every subject that one of its line items comes from."""
    return {
        subject
        for invoice in label["invoices"]
        for item in invoice["line_items"]
        for subject, entries in CATALOG.items()
        if any(item["description"] == entry[0] for entry in entries)
    }


def score_search(run: Run) -> dict[str, Any]:
    by_id = {entry["id"]: entry for entry in run.documents.values() if entry["id"]}

    def ask(query: str, mode: str) -> list[dict[str, Any]]:
        response = run.http.get(
            f"{run.base}/search", params={"q": query, "mode": mode, "limit": 10}
        )
        response.raise_for_status()
        return response.json()["results"]

    def subject_scores(queries: dict[str, str], mode: str) -> dict[str, Any]:
        first, three, returned, nothing = 0, 0.0, 0, 0
        for subject, query in queries.items():
            hits = ask(query, mode)
            right = [
                subject in subjects_of(by_id[hit["document_id"]]["label"])
                for hit in hits
            ]
            returned += len(hits)
            nothing += not hits
            first += bool(right and right[0])
            three += sum(right[:3]) / 3
        return {
            "queries": len(queries),
            "top_result_right": first / len(queries),
            "precision_at_3": three / len(queries),
            "queries_with_no_results": nothing,
            "mean_results": returned / len(queries),
        }

    # Exact numbers, written differently from the document, for every completed invoice with one.
    numbers = [
        (invoice["fields"]["invoice_number"], entry["id"])
        for entry in run.documents.values()
        if entry["status"] == "completed"
        for invoice in entry["label"]["invoices"]
        if invoice["fields"]["invoice_number"]
    ]

    def number_scores(mode: str) -> dict[str, Any]:
        right = 0
        for number, document_id in numbers:
            hits = ask(re.sub(r"[-/]", " ", number).lower(), mode)
            same_number = {
                entry["id"]
                for entry in run.documents.values()
                for invoice in entry["label"]["invoices"]
                if invoice["fields"]["invoice_number"] == number
            }
            right += bool(hits) and hits[0]["document_id"] in same_number | {
                document_id
            }
        return {
            "queries": len(numbers),
            "top_result_right": right / len(numbers) if numbers else None,
        }

    latencies = []
    for mode in ("keyword", "semantic", "hybrid"):
        for query in MEANING_QUERIES.values():
            started = time.perf_counter()
            ask(query, mode)
            latencies.append(time.perf_counter() - started)

    return {
        "by_meaning": {
            mode: subject_scores(MEANING_QUERIES, mode)
            for mode in ("keyword", "semantic", "hybrid")
        },
        "by_words": {
            mode: subject_scores(WORD_QUERIES, mode)
            for mode in ("keyword", "semantic", "hybrid")
        },
        "by_number": {
            mode: number_scores(mode) for mode in ("keyword", "semantic", "hybrid")
        },
        "unrelated_query_results": {
            mode: len(ask("zebrafish astronomy telescope", mode))
            for mode in ("keyword", "semantic", "hybrid")
        },
        "latency_seconds": summary(latencies),
    }


def git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "describe", "--always", "--dirty"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    # 127.0.0.1, not localhost: on Windows, localhost tries IPv6 first and waits two seconds.
    parser.add_argument("--api", default="http://127.0.0.1:8001")
    parser.add_argument("--name", default="default")
    args = parser.parse_args()

    labels = json.loads((ROOT / "labels.json").read_text(encoding="utf-8"))["documents"]
    run = Run(args.api)
    started = time.perf_counter()

    independent = [label for label in labels if not label["depends_on"]]
    dependent = [label for label in labels if label["depends_on"]]
    # Two rounds: a copy has to be checked against an original that has finished.
    for name, batch in (("first", independent), ("second", dependent)):
        print(f"Uploading {len(batch)} documents ({name} round)", flush=True)
        for label in batch:
            run.upload(label)
        run.wait([label["file"] for label in batch])
    wall_seconds = time.perf_counter() - started
    run.collect()

    models = sorted(
        {
            result["llm_model"]
            for entry in run.documents.values()
            for result in entry["results"]
            if result["llm_model"]
        }
    )
    output = {
        "name": args.name,
        "measured_at": datetime.now(UTC).isoformat(),
        "commit": git_commit(),
        "models": models,
        "extraction": score_extraction(run),
        "validation": score_validation(run),
        "outcomes": score_outcomes(run),
        "timing": score_timing(run, wall_seconds),
        "search": score_search(run),
        "metrics_endpoint": run.http.get(f"{run.base}/metrics").json(),
        # The raw answers, so that the scores above can be checked or recomputed.
        "documents": {
            file: {"status": entry["status"], "results": entry["results"]}
            for file, entry in run.documents.items()
        },
    }
    folder = ROOT / "results"
    folder.mkdir(exist_ok=True)
    (folder / f"{args.name}.json").write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8"
    )

    overall = output["extraction"]["overall"]
    print(f"\nModels: {models}")
    print(
        f"Value accuracy: {overall['value_accuracy']:.3f}, field accuracy {overall['field_accuracy']:.3f}"
    )
    print(f"Invented values: {overall['outcomes'].get('invented', 0)}")
    print(
        f"Line items: precision {overall['line_precision']}, recall {overall['line_recall']}"
    )
    print(
        f"Validation: precision {output['validation']['precision']}, recall {output['validation']['recall']}"
    )
    print(
        f"Success rate: {output['outcomes']['success_rate']}, unexpected: {len(output['outcomes']['unexpected'])}"
    )
    print(f"Wall time: {wall_seconds:.0f} s")


if __name__ == "__main__":
    main()
