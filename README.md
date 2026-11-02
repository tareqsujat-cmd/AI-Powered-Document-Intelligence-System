# Document Intelligence System

Ingests invoices as PDF, JPG, PNG, TIFF, XLSX, XLS and CSV (PDFs up to about 1000 pages), extracts structured fields with OCR and an LLM, validates them, and makes them reviewable and searchable through an API and web UI.

- [Architecture, schema and design decisions](docs/architecture.md)
- [Evaluation report, with measured accuracy and timings](eval/report.md)
- [Test suites and coverage](docs/test_coverage.md)
- [AI prompts](prompts) and [AI-assisted development log](docs/ai_disclosure.md)
- [Walkthrough script](docs/walkthrough_script.md)
- [Delivery plan](docs/plan.md)

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold, schema, infrastructure | Done |
| 1 | Processing pipelines | Done |
| 2 | LLM extraction and validation | Done |
| 3 | REST API, background pipeline, search | Done |
| 4 | Web interface | Done |
| 5 | Evaluation and final docs | Done |

The whole system works through the web interface or the REST API: upload, live progress, results beside the original page, corrections, review, reprocessing, search, and metrics.

## Measured results

Measured on 50 synthetic labeled documents through the real API, with real OCR and the real LLM (`gemini-3.5-flash-lite`), on one laptop. The full report, with every error listed, is [eval/report.md](eval/report.md).

| Measure | Result |
|---|---|
| Field value accuracy | 99.3% (401 of 404 printed values) |
| Invented values | 0 |
| Line items exactly right: precision / recall | 99.3% / 99.3% |
| Validation findings: precision / recall | 84.6% / 91.7% |
| Success rate / failure rate, valid documents | 100% / 0% (44 of 44) |
| Corrupted or disguised files failed or refused, with a reason | 6 of 6 |
| Processing time per document, median | 11.7 s, mostly waiting for the free-tier LLM quota |
| OCR throughput, scanned pages | 19.9 per minute with one document, 35.3 with two at once |
| Search latency, median | 45 ms |

The data is synthetic, so these numbers are an upper bound for real invoices. The report explains its limits, and what was fixed after the first run.

## Run with Docker

Requires Docker with Compose v2.17 or later.

```bash
cp .env.example .env     # then change the values marked change-me, and set LLM_API_KEY
docker compose up --build
```

An LLM API key is needed for PDFs, images, and spreadsheets whose columns are not recognized. A free Gemini key comes from [Google AI Studio](https://aistudio.google.com/apikey). If it returns "free tier not available", create the key in a new project. To use another provider, see `LLM_PROVIDER` in `.env.example`. The automated tests never call a real API; `python -m tests.llm_smoke` does, and uses your quota.

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| API | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Liveness | http://localhost:8000/health |
| Readiness (Postgres, pgvector, Redis) | http://localhost:8000/health/ready |

The backend image is about 3 GB, because it includes CPU-only PyTorch and the embedding model. All ports bind to `127.0.0.1`, because the API has no login. Run `make help` for shortcuts. `make test` runs the backend tests in a container against real Postgres.

## Run tests without Docker

Requires Python 3.11 and Node.js. Tests marked `db` need a PostgreSQL server with pgvector and are skipped if `TEST_DATABASE_URL` is not set. They create and drop their own temporary databases, so they do not touch the database in the URL, but the user needs permission to create databases. Tests marked `ocr` need the Tesseract binary and are skipped if it is not installed. `make test` runs everything.

```bash
python -m venv backend/.venv
source backend/.venv/bin/activate       # Windows: backend\.venv\Scripts\activate
pip install -r backend/requirements-dev.txt

export TEST_DATABASE_URL=postgresql://user:password@localhost:5432/postgres
pytest backend/tests -q

cd frontend && npm ci && npm test
```

For coverage, add `--cov=app` to `pytest`, or run `npm run coverage` in `frontend`. The latest results are in [docs/test_coverage.md](docs/test_coverage.md).

To drive the real app in a browser, with the whole stack running and an LLM key set, run `npm run e2e` in `frontend`. It uses your installed Google Chrome.

## Layout

| Path | Contents |
|---|---|
| `backend/app` | REST API, background pipeline, Celery worker, file processing, invoice extraction, validation, search, database models |
| `backend/migrations` | Alembic migrations |
| `backend/tests` | Backend tests |
| `frontend` | Next.js app |
| `prompts` | LLM prompts |
| `docs` | Architecture, test coverage, walkthrough script, plan, AI disclosure |
| `eval` | Labeled evaluation dataset, scripts, results and report |
| `sample_data` | Synthetic documents to try by hand, described in its README |

## Configuration

All settings come from environment variables, documented in [.env.example](.env.example). Secrets belong only in `.env`, which is gitignored. With `APP_ENV=production`, the app refuses to start with the placeholder database password or wildcard CORS.

## Known limitations

- **No login.** The API has no authentication, which is why every port binds to `127.0.0.1`. The `actor` recorded on a correction is whatever the client sends. Authentication comes before any public deployment.
- **Synthetic data only.** Accuracy is measured on generated invoices. Real suppliers' layouts, handwriting and stamps are not covered.
- **LLM quota.** The LLM stage is paced to the provider's quota (`LLM_REQUESTS_PER_MINUTE`, 10 by default for the free tier), so with many documents queued, most of the processing time is waiting for that quota rather than computing.
- **A file that is not a supported type is refused at upload** rather than stored as a failed document, for example a program renamed `.pdf`.
- **Double entry across formats.** An invoice entered both as a PDF and as a spreadsheet row with the same values is reported as cross-verified, not as a duplicate. When two documents conflict, only the later one is flagged until the earlier one is checked again.
- **One edge case in reprocessing.** A full reprocess requested while the queue is down removes the old pages before returning 503.
- **Browsers.** The interface is tested in Chrome only, and drag and drop is simulated in tests.
