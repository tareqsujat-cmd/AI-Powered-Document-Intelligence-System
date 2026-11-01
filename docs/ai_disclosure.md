# AI-assisted development log

This project is built with **Claude Code** (model: Claude Opus 5.5), directed by the candidate. The instructions given to the assistant, including the master prompt and the assignment text, are in [CLAUDE.md](../CLAUDE.md). Prompts used by the application itself at runtime are in [/prompts](../prompts).

## Log

### Phase 0: scaffold, schema, infrastructure

- **Assistant generated:** repository structure, docker-compose, Dockerfiles, Makefile, configuration, logging, health endpoint, Celery setup, database models, tests, and documentation. A subagent generated the Next.js scaffold.
- **Alembic generated:** the initial migration, by autogenerate. The assistant then edited it to add the `vector` extension, fix a missing import, and add an index.
- **Verified by running:** the full compose stack under Docker, including migrations, a task through a live Celery worker and Redis, the page in headless Chrome, and backend tests inside the test container. The first test runs used PostgreSQL 16.2 with pgvector 0.6.2, embedded via `pgserver`, before Docker was installed.

### Phase 1: processing pipelines

- **Assistant generated:** file validation, PDF text and OCR handling, image handling, OCR preprocessing, extraction dispatch, test fixtures, tests and benchmarks. A subagent generated the spreadsheet module and its tests; the assistant then read that module and made a small change to it.
- **Changed because of measurements:** the first OCR design made uneven lighting worse than raw Tesseract, took 56 seconds on a noisy page, and could not read sideways pages. Benchmarks exposed each of these and led to the redesign described in [architecture.md](architecture.md).
- **Verified by running:** the full backend test suite in Docker, with real Tesseract and real Postgres; all file types through the runtime worker image as its non-root user; and 1000 scanned pages through real OCR. A deliberate memory leak confirmed that the memory test can fail.
- **Not verified:** real-world scans. All test documents are synthetic, so real accuracy is measured in Phase 5. An encrypted XLS test uses a hand-built file, not one encrypted by Excel itself.
