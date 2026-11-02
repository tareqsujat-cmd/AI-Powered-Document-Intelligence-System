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

### Phase 2: LLM extraction and validation

- **Assistant generated:** the LLM client, extraction prompt, invoice splitting, hallucination guards, spreadsheet mapping, storage functions, and tests. A subagent generated the validation engine and its tests; the assistant then read that code.
- **Runtime prompts:** see [/prompts](../prompts).
- **Found by running against the real API, after mocked tests had passed:** a batch PDF merged into one invoice because of a bug in finding invoice numbers, and the newest model took over a minute per invoice, which led to a different default. Both are fixed or changed, and the batch bug has a regression test.
- **Verified by running:** the full test suite in Docker, with real Postgres and a mocked LLM; five synthetic documents through the real Gemini API; and the subagent's 78 deliberately broken variants of the validation code, each of which made a test fail.
- **Not verified:** accuracy on real documents, or at volume against provider quotas. The OpenAI-compatible client has only been tested against a simulated server, not a real provider.

### Phase 3: REST API, background pipeline, search

- **Assistant generated:** the pipeline, crash recovery, shared LLM pacing, embeddings, search, the review endpoint, and their tests. A subagent generated the REST API, rate limiting, security middleware, and their tests; the assistant then read that code and fixed bugs the subagent reported.
- **Changed because of running it:** a guessed similarity cutoff was replaced with a measured one, and flagging an agreeing PDF and spreadsheet as duplicates was replaced with reporting them as verified, after seeing it live.
- **Verified by running:** the full test suite in Docker, with real Postgres, Redis, Tesseract, and the embedding model, and a mocked LLM; 13 files through the running stack with the real LLM; and killing a real worker container mid-job. The subagent tried 109 deliberately broken variants of the API code, and the assistant 31 of the extraction code. Each survivor became a new test or removed code.
- **Not verified:** throughput with many workers, behaviour against provider quotas at volume, chunked uploads through a real server, and real-world accuracy, which Phase 5 measures.

### Phase 4: web interface

- **Assistant generated:** the pages, components, API client, page preview route, and browser test. A subagent generated the component tests.
- **Found by driving the app in a real browser and looking at screenshots:** Clear filters did nothing, the search method buttons lagged behind clicks, retried documents counted the wait as processing time, the first document after a restart took over 30 seconds, and exact-number searches buried the answer. All are fixed.
- **Verified by running:** the whole flow in real Chrome against the running stack with the real LLM, at desktop and phone widths, plus the component tests.
- **Not verified:** browsers other than Chrome, real drag and drop, and screen readers. A person should review the design.

### Phase 5: evaluation and final docs

- **Assistant generated:** the synthetic dataset generator and its labels, the scoring code and its tests, the evaluation runner, the throughput script, the report builder, the walkthrough script, the test coverage summary, and the README updates. Every number in [eval/report.md](../eval/report.md) is written by `eval/build_report.py` from the measured results in `eval/results/`, not typed by hand.
- **Found by the evaluation, after the baseline run:** invoices that print their number under an `INVOICE #` column heading, with the value on the line below, were not recognised as new invoices, so a batch PDF merged two of them. The splitter now reads column headings, with regression tests, and the report shows the baseline run beside the run after the fix.
- **Found in the measurement itself:** on Windows, `localhost` tried IPv6 first and added about two seconds to every request from the evaluation client. This inflated the baseline's wall time and search latency, but not processing times, which the server measures. The client now uses `127.0.0.1`. The scorer also counted a search hit as wrong when a document covered more than one subject, which is fixed.
- **Verified by running:** all 50 documents through the real API, OCR and Gemini, on a fresh database, twice; the scorer's own tests; and the backend and frontend test suites with coverage.
- **Not verified:** real-world invoices. The dataset is synthetic and generated by the same assistant that built the system, so it may share its blind spots.
