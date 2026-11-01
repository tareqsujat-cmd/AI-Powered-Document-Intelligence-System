"""Compare OCR strategies on degraded invoices. Run: python -m tests.ocr_benchmark"""

import time

import pytesseract

from app.core.config import Settings
from app.processing import ocr
from tests.document_fixtures import DEGRADATIONS, INVOICE_TOKENS, render_invoice


def recall(text: str) -> float:
    return sum(token in text for token in INVOICE_TOKENS) / len(INVOICE_TOKENS)


def raw(image, psm: int) -> str:
    return pytesseract.image_to_string(image, config=f"--oem 1 --psm {psm}")


def main() -> None:
    settings = Settings(_env_file=None)
    base = render_invoice(300)
    print(
        f"{'degradation':20} {'raw_psm3':>9} {'raw_psm6':>9} {'pipeline':>9} {'conf':>6} {'sec':>6}"
    )
    totals = {"raw3": 0.0, "raw6": 0.0, "pipeline": 0.0}
    for name, degrade in DEGRADATIONS.items():
        image = degrade(base)
        r3, r6 = recall(raw(image, 3)), recall(raw(image, 6))
        started = time.perf_counter()
        result = ocr.ocr_image(image, settings)
        seconds = time.perf_counter() - started
        score = recall(result.text)
        totals["raw3"] += r3
        totals["raw6"] += r6
        totals["pipeline"] += score
        confidence = f"{result.confidence:.2f}" if result.confidence is not None else "-"
        print(f"{name:20} {r3:9.2f} {r6:9.2f} {score:9.2f} {confidence:>6} {seconds:6.1f}")
    count = len(DEGRADATIONS)
    print(
        f"{'MEAN':20} {totals['raw3'] / count:9.2f} {totals['raw6'] / count:9.2f} "
        f"{totals['pipeline'] / count:9.2f}"
    )


if __name__ == "__main__":
    main()
