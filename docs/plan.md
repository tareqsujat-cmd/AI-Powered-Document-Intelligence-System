# Delivery plan

Each phase ends with tests passing, a commit, and a stop for sign-off.

| Phase | Scope | Done when |
|---|---|---|
| 0 | Monorepo, compose, config, logging, health, schema and migration, architecture docs | Migration applies on real Postgres + pgvector; models and migration do not drift |
| 1 | File validation, PDF text/OCR hybrid, image OCR with OpenCV, multi-page TIFF, spreadsheets, corrupted-file handling | Generated fixtures of every type pass; a 1000-page PDF streams without memory growing with page count |
| 2 | LLM client, JSON schema extraction, chunking, rule-based sheet mapping, validation engine, cross-document checks | Mocked-LLM tests pass, including hallucination guards; every rule has pass and fail cases |
| 3 | REST API, Celery pipeline with per-stage progress, retries, rate limiting, keyword/semantic/hybrid search, metrics | API tests pass, including failure cases; end-to-end run from upload to search on the real stack |
| 4 | Upload, dashboard, document detail with preview and inline edit, search page | Vitest passes; the flow works in a browser against the real backend |
| 5 | 30 to 50 labeled synthetic invoices, evaluation script, report, README polish, disclosure, walkthrough script | `eval/report.md` contains only measured numbers |

## Known risks

| Risk | Plan |
|---|---|
| The development machine has no Docker or Tesseract yet | Install both before Phase 1 finishes, as OCR and the compose stack cannot be verified without them |
| LLM free-tier quotas may throttle the evaluation run | Client backs off on rate limits; the report states the model and any throttling actually seen |
| A 1000-page PDF may hold many invoices, and splitting them is the hardest extraction problem | Schema supports it now; Phase 2 detects boundaries, and the evaluation includes a multi-invoice PDF |
| sentence-transformers pulls in torch, which makes images large | Use CPU-only torch in the Docker image |
| OCR on 1000 scanned pages is slow on CPU | Page-level progress and horizontal worker scaling; report measured seconds per page |
