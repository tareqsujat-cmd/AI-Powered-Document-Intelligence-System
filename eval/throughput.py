"""How does OCR throughput change with more workers? Measures text extraction only, since the LLM
stage is limited by provider pacing rather than by this machine.

Run: python eval/throughput.py --api http://127.0.0.1:8001 --pages 40
"""

import argparse
import io
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
import pymupdf
from PIL import Image

ROOT = Path(__file__).parent


def scanned_pdf(number: str, pages: int) -> bytes:
    with pymupdf.open() as source:
        first = source.new_page(width=595, height=842)
        for index, line in enumerate(
            [
                f"Invoice Number: {number}",
                "Invoice Date: 2026-05-15",
                "Supplier: Northwind Traders",
                "Total GBP 1,100.00",
            ]
        ):
            first.insert_text((50, 60 + index * 18), line, fontsize=11)
        for page_number in range(2, pages + 1):
            page = source.new_page(width=595, height=842)
            for row in range(35):
                page.insert_text(
                    (50, 60 + row * 18),
                    f"Item {page_number}-{row} consulting services 2 500.00 1,000.00",
                    fontsize=10,
                )
        with pymupdf.open() as scanned:
            for page in source:
                pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
                array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.stride
                )
                buffer = io.BytesIO()
                Image.fromarray(array[:, : pix.width]).save(
                    buffer, format="JPEG", quality=80
                )
                target = scanned.new_page(width=595, height=842)
                target.insert_image(target.rect, stream=buffer.getvalue())
            return scanned.tobytes()


def measure(
    http: httpx.Client, base: str, documents: int, pages: int, label: str
) -> dict:
    ids = []
    for index in range(documents):
        name = f"throughput-{label}-{index}.pdf"
        data = scanned_pdf(f"TP-{label}-{index}-{int(time.time())}", pages)
        response = http.post(f"{base}/documents", files=[("files", (name, data))])
        response.raise_for_status()
        ids.append(response.json()["results"][0]["document"]["id"])

    pending = set(ids)
    while pending:
        time.sleep(2)
        for identifier in list(pending):
            if http.get(f"{base}/documents/{identifier}/status").json()["status"] in (
                "completed",
                "failed",
            ):
                pending.discard(identifier)

    starts, ends, durations = [], [], []
    for identifier in ids:
        for event in http.get(f"{base}/documents/{identifier}/history").json():
            if event["stage"] != "text_extraction":
                continue
            moment = datetime.fromisoformat(event["created_at"])
            if event["event"] == "stage_started":
                starts.append(moment)
            elif event["event"] == "stage_completed":
                ends.append(moment)
                durations.append(event["duration_ms"] / 1000)
    span = (max(ends) - min(starts)).total_seconds()
    total_pages = documents * pages
    return {
        "documents": documents,
        "pages_each": pages,
        "total_pages": total_pages,
        "span_seconds": span,
        "seconds_per_page_per_document": [duration / pages for duration in durations],
        "pages_per_minute": total_pages / span * 60,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8001")
    parser.add_argument("--pages", type=int, default=40)
    parser.add_argument("--output", default="throughput")
    args = parser.parse_args()

    base = f"{args.api.rstrip('/')}/api/v1"
    http = httpx.Client(timeout=120, limits=httpx.Limits(max_keepalive_connections=0))
    results = {
        "one_document": measure(http, base, 1, args.pages, "one"),
        "two_documents": measure(http, base, 2, args.pages, "two"),
    }
    folder = ROOT / "results"
    folder.mkdir(exist_ok=True)
    (folder / f"{args.output}.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    for name, result in results.items():
        print(
            f"{name}: {result['pages_per_minute']:.1f} pages/min over {result['span_seconds']:.0f} s"
        )


if __name__ == "__main__":
    main()
