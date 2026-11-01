# Document Intelligence System

Ingests invoices as PDF, JPG, PNG, TIFF, XLSX, XLS and CSV (PDFs up to about 1000 pages), extracts structured fields with OCR and an LLM, validates them, and makes them reviewable and searchable through an API and web UI.

- [Architecture, schema and design decisions](docs/architecture.md)
- [Delivery plan](docs/plan.md)
- [AI-assisted development log](docs/ai_disclosure.md)

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold, schema, infrastructure | Done |
| 1 | Processing pipelines | Done |
| 2 | LLM extraction and validation | Not started |
| 3 | REST API, background pipeline, search | Not started |
| 4 | Frontend features | Not started |
| 5 | Evaluation and final docs | Not started |

Today the system serves health endpoints and a landing page. Text extraction works for every file type as a library in `backend/app/processing`, but it cannot be used through the API or web UI until Phase 3.

## Run with Docker

Requires Docker with Compose v2.

```bash
cp .env.example .env     # then change the values marked change-me
docker compose up --build
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| API | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Liveness | http://localhost:8000/health |
| Readiness (Postgres, pgvector, Redis) | http://localhost:8000/health/ready |

All ports bind to `127.0.0.1`, because the API has no login. Run `make help` for shortcuts. `make test` runs the backend tests in a container against real Postgres.

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

## Layout

| Path | Contents |
|---|---|
| `backend/app` | FastAPI app, configuration, database models, Celery worker, file processing |
| `backend/migrations` | Alembic migrations |
| `backend/tests` | Backend tests |
| `frontend` | Next.js app |
| `prompts` | LLM prompts |
| `docs` | Architecture, plan, AI disclosure |
| `eval` | Evaluation dataset, script and report |
| `sample_data` | Synthetic example documents |

## Configuration

All settings come from environment variables, documented in [.env.example](.env.example). Secrets belong only in `.env`, which is gitignored. With `APP_ENV=production`, the app refuses to start with the placeholder database password or wildcard CORS.
