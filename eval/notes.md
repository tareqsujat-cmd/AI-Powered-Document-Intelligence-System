## What changed between the two runs

Two runs are reported. **baseline** was the first full run. **final** is the run after fixing what the baseline exposed. Both used the same 50 documents, the same model, and a fresh database each time. The runs record commit `fbac896-dirty` because the fix was not yet committed. The backend code measured in the final run is the Phase 5 commit, `0f81b67`.

1. **A system bug: invoice splitting.** In the baseline, `batch-4-invoices.pdf` gave three invoices instead of four. Its second invoice prints the number under an `INVOICE #` column heading, with the value on the line below, and the splitter only looked beside the label. So it treated that page as a continuation of the first invoice, and the second invoice was lost. The splitter now reads column headings, in text-layer output (tab-separated) and OCR output (space-separated), and has regression tests in `backend/tests/test_segmentation.py`.
2. **A labeling bug: one wrong label.** In the PDF layouts the currency is printed only beside the total. `no-total.pdf` hides the total, so it prints no currency, but its label still said `USD`. The system's answer, null, was right. The generator now labels it null. The baseline was scored against the wrong label, which cost it one field.
3. **A measurement bug: the client's connection.** On Windows, `localhost` tried IPv6 first and waited about two seconds before falling back, on every request from the evaluation client. This inflated the baseline's wall time and its search latency. Processing times are timestamped by the server and were not affected. The client now connects to `127.0.0.1`.
4. **A scoring bug: search subjects.** A search hit counted as right only for a document's first subject, but `multi-page-invoice.pdf` really covers two subjects. A document now counts for every subject in its line items. The baseline's search numbers used the old scoring, so they are not in the comparison.

## What is still wrong, and why

The remaining extraction errors are all OCR, listed in full at the end of this report:

- **Lost header line.** On one noisy scan, Tesseract lost the supplier's header line entirely. The LLM was given no supplier name, said so, and returned null. That is the intended behavior: the system did not invent one. Validation then raised `missing_required_field`, a correct finding about the extraction but not one of the seeded problems, so it counts as a false alarm.
- **Noise read as characters.** Speckle noise became stray characters around supplier names (`Lid` for `Ltd`, and a trailing `Ee`). The hallucination guard accepts these, because they really are in the OCR text.
- **A missed duplicate.** One of those misread names belongs to the scanned copy of an invoice, so the duplicate check missed it. That check requires supplier, number, date and total to match, ignoring only case, punctuation and legal forms such as "Ltd".
- **A warning on a blurred image.** On the heavily blurred image, the guard dropped one line-item value it could not find in the text and raised a warning. That warning is counted as a false alarm, although flagging a blurred page for a person is arguably what should happen.

## Limits of this evaluation

- **Synthetic data.** The documents are generated, in three layouts, with the damage simulated. Real invoices vary far more, so these numbers are an upper bound on what to expect from real documents, not a forecast.
- **Shared blind spots.** The dataset and the system were built with the same AI assistant, so the dataset may not probe the system's weaknesses.
- **One run of each.** The LLM is not fully deterministic, so a repeat run can differ by a field or two.
- **Timings.** They come from one laptop running one worker container with two processes, with the LLM paced to 10 requests a minute for the free tier. Per-document times include waiting for that quota. On this hardware, a 1000-page scanned PDF took 48.2 minutes (2.89 s per page) in Phase 1.
