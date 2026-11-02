# Sample data

Synthetic documents for trying the system by hand, copied from the evaluation set in [eval/dataset](../eval/dataset). They contain no real business data. The right answer for each one is in [eval/labels.json](../eval/labels.json).

| File | What it shows |
|---|---|
| `digital-01-classic.pdf` | A digital PDF, read from its text layer |
| `batch-4-invoices.pdf` | Four invoices in one PDF, split apart |
| `scan-02.pdf` | A scanned PDF, read with OCR |
| `photo-02.jpg` | A photo of an invoice |
| `phone-photo.png` | A poor photo: tilted, shadowed, noisy and blurred |
| `two-sheets.xlsx` | A workbook with one invoice per sheet |
| `flat.csv` | One invoice as a CSV row, mapped by rules without the LLM |
| `wrong-total.pdf` | Subtotal plus tax does not equal the total, so it needs review |
| `match-original.pdf` | Upload before `mismatch-copy.xlsx` |
| `mismatch-copy.xlsx` | The same invoice as a spreadsheet, with a different total: a PDF-vs-Excel mismatch |
| `password.pdf` | An encrypted PDF, which fails with a clear reason |

To regenerate them all: `python eval/generate_dataset.py`.
