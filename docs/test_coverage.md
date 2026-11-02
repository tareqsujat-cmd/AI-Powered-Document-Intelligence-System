# Tests and coverage

Measured on 2026-11-03, at the Phase 5 commit.

| Suite | Tests | Result | Coverage |
|---|---|---|---|
| Backend (`pytest backend/tests`), in the Docker test image with real PostgreSQL + pgvector, Redis, Tesseract and the embedding model | 1432 | 1432 passed, none skipped, in 14 min 7 s | 98% of statements (4084, 65 missed) |
| Frontend (`npm run coverage` in `frontend`), Vitest with jsdom | 407 | 407 passed | 97.7% of statements, 95.5% of branches |
| Evaluation scorer (`pytest eval/test_scoring.py`) | 40 | 40 passed | not measured |

The LLM is mocked in the automated tests, so they never use quota. The real LLM is exercised by the evaluation ([eval/report.md](../eval/report.md)) and by `python -m tests.llm_smoke`. The browser test (`npm run e2e` in `frontend`) drives the real app in Chrome against a running stack. It was last run in Phase 4, and the frontend has not changed since, apart from its test configuration.

## What the backend tests cover

Grouped by the assignment's areas:

| Area | Test files | Tests |
|---|---|---|
| Document processing and file validation | `test_file_validation`, `test_pdf`, `test_extract`, `test_text`, `test_large_pdf` | 118 |
| OCR and images | `test_ocr`, `test_images` | 71 |
| Spreadsheet parsing | `test_spreadsheets` | 198 |
| AI extraction | `test_llm_client`, `test_extractor`, `test_guards`, `test_segmentation`, `test_spreadsheet_mapper`, `test_normalize`, `test_persistence`, `test_pacing`, `test_extraction_end_to_end` | 334 |
| Validation | `test_validation_rules`, `test_cross_document` | 247 |
| API endpoints | `test_api_documents`, `test_api_corrections`, `test_api_review_and_search`, `test_api_preview`, `test_api_security`, `test_rate_limit`, `test_api_metrics`, `test_health` | 315 |
| Background pipeline | `test_pipeline`, `test_tasks`, `test_celery`, `test_preload` | 55 |
| Search | `test_search` | 42 |
| Database, migrations, config, logging | `test_models`, `test_migrations`, `test_config`, `test_logging`, `test_session` | 52 |

Failure scenarios are tested within each area, among them:
- **Damaged files:** corrupted, encrypted, truncated, empty and disguised files.
- **Upload limits:** oversized uploads and rate limiting.
- **LLM failures:** timeouts, malformed output and invented values.
- **Worker problems:** crashed workers, stale tasks and retries.

`test_large_pdf` streams a 1000-page PDF and checks that memory does not grow with the page count.

## Where backend coverage is lowest

| Module | Coverage | What is not covered |
|---|---|---|
| `app/worker/celery_app.py` | 76% | Signal handlers that run only inside a real worker process: logging set-up, and resetting connections and preloading the model after fork |
| `app/worker/tasks.py` | 88% | Building the real LLM client from settings, since tests inject a fake one, and the task-level path for a superseded task |
| `app/db/session.py` | 89% | Creating the app's own engine and session factory, since tests create their own |
| `app/pipeline/processor.py` | 95% | The periodic progress commit during long text extraction, an embedding batch filling up, and a document that yields no invoice at all |

The worker's start-up runs every time the stack starts, including in the evaluation. Recovery from a worker killed mid-job was tested on the running stack in Phase 3.

In the frontend, the four route files under `src/app` show 0%, because each only renders a component that is tested on its own. The browser test covers them.

## Reproduce

```bash
docker compose --profile test run --rm tests pytest tests -q --cov=app --cov-report=term
cd frontend && npm ci && npm run coverage
pytest eval/test_scoring.py -q
```
