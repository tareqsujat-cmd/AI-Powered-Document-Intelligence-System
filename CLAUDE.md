# AI-Powered Document Intelligence System

Ingest PDF/JPG/PNG/TIFF/XLSX/XLS/CSV (PDFs up to ~1000 pages). Extract text (PDF text layer, OCR for scans/images, pandas/openpyxl for sheets), extract invoice fields with an LLM, validate, store, search, review in a web UI.

## Stack (fixed)
- Backend: Python 3.11, FastAPI, SQLAlchemy 2 + Alembic, Pydantic v2
- Queue: Celery + Redis
- DB: PostgreSQL + pgvector
- Parsing/OCR: PyMuPDF, Tesseract (pytesseract) + OpenCV preprocessing, pandas/openpyxl/xlrd
- LLM: provider-agnostic client, env-configurable (default Gemini free tier), JSON-schema output
- Embeddings: sentence-transformers all-MiniLM-L6-v2 (local)
- Frontend: Next.js App Router + TypeScript + Tailwind
- Tests: pytest + httpx; Vitest for frontend
- Everything runs via docker-compose

## Rules
- Never invent values: unknown field = null + confidence + reason.
- Stream large PDFs page by page; OCR only pages without a text layer.
- Corrupted/encrypted/empty files -> status=failed with reason, retryable.
- Validation (tolerance 0.01): required fields, numeric sanity, duplicates (supplier+number+date+total), subtotal+tax=total, sum(line items)=subtotal, PDF-vs-PDF and PDF-vs-Excel cross checks. Flag needs_review.
- Security: magic-byte file sniffing, size limits, sanitized filenames, secrets only in .env, CORS, rate limiting, parameterized queries.
- Typed code, tests for every module, README kept current.
- Store all LLM prompts in /prompts/*.md and log AI-assisted dev in docs/ai_disclosure.md.
- After each phase: run tests, fix failures, then git commit with a clear message.

## Commands
- `docker compose up --build`
- `pytest backend/tests -q`




#Master prompt (start in Plan Mode: Shift+Tab twice)
Read CLAUDE.md and the assignment below. Plan the whole project first, then build it phase by phase, stopping after each phase for me to say "next".

Phase 0: monorepo scaffold (/backend, /frontend, /prompts, /docs, /eval, /sample_data), docker-compose (postgres+pgvector, redis, backend, worker, frontend), .env.example, Makefile, DB models + Alembic migration (documents, document_pages, extracted_text, extraction_results with per-field confidence, line_items, validation_errors, processing_history, embeddings), config, logging, health endpoint, docs/architecture.md with Mermaid architecture diagram + ERD + design decisions.
Phase 1: processing pipelines (file validation, PDF text/OCR hybrid streaming, image OCR with OpenCV preprocessing + multi-page TIFF, spreadsheets with all sheets/header detection/CSV encoding detection, corrupted-file handling) + tests with generated fixtures.
Phase 2: LLM extraction (JSON schema, per-field confidence + source snippet, null for unknown, chunking for long docs, rule-based spreadsheet mapping with LLM fallback) + validation engine with typed errors/severity + cross-document checks + tests with mocked LLM and hallucination guards.
Phase 3: REST API (upload, list/filter, status, results, PATCH corrections with before/after history, reprocess, reprocess-failed, search keyword/semantic/hybrid, metrics), Celery pipeline with per-stage progress, retries, failure reasons, rate limiting, API tests incl. failure cases.
Phase 4: Next.js frontend (multi-upload with progress, dashboard with live status + filters, document detail with preview beside fields/confidence/line items/validation errors/inline edit/reprocess, search page).
Phase 5: /eval with 30-50 synthetic labeled invoices (digital PDF, scanned PDF, image, xlsx, csv, plus corrupted/low-quality), script measuring real extraction accuracy, line-item accuracy, validation precision/recall, time per doc/page, success and failure rate -> eval/report.md. Then README polish, docs/ai_disclosure.md, test coverage summary, docs/walkthrough_script.md.

Rules: use subagents for independent work (e.g. tests vs frontend), run tests yourself and fix failures before moving on, commit after each phase, report only real measured numbers, ask me only if blocked.

<AI Engineer 
Project: AI-Powered Document Intelligence System 
Submission Deadline: 5 Days 
Scenario 
A company receives thousands of business documents daily, financial data including invoices, 
receipts, scanned PDFs, images, Excel spreadsheets, and CSV files (Pdf pages are apporx 
1000 pages). Employees manually extract invoice details, verify financial calculations, and enter 
the information into internal systems, pdf - pdf cross verification , pdf - excel cross verification.  
 
This process is time-consuming and prone to errors. The company wants an AI-powered 
document intelligence system that automates document processing, extracts structured 
information, validates results, and makes the data searchable through an API and web 
interface. 
Assignment 
Task 1 — Document Processing 
Design and implement a system that accepts multiple PDF, JPG, PNG, TIFF, XLSX, XLS, and 
CSV files. Extract text from digital PDFs, use OCR for scanned documents and images, and 
parse structured spreadsheet data. Handle multi-page PDFs, multiple sheets, poor image 
quality, and corrupted files. 
Task 2 — AI Information Extraction 
Extract invoice number, invoice date, supplier name, customer name, subtotal, tax, total, 
currency, and line items. Return structured JSON and handle missing or uncertain information 
without inventing values. 
Task 3 — Data Validation 
Implement validation rules to identify missing fields, incorrect numeric values, duplicate invoices, 
and inconsistencies between subtotal, tax, line items, and total amount. Flag records requiring 
manual review. 
Task 4 — Backend API 
Build REST APIs for document uploads, processing status, extracted results, document search, 
manual corrections, and failed-job reprocessing. Use background processing for 
time-consuming OCR and AI operations. 
Task 5 — Database and Search 
Design a database to store document metadata, extracted text, structured results, validation 
errors, and processing history. Implement keyword-based filtering and semantic search. 
Task 6 — Frontend 
Build a simple React or Next.js interface where users can upload multiple documents, monitor 
processing progress, inspect extracted information, review validation errors, correct values, and 
search previously processed documents. 
Task 7 — Testing and Evaluation 
Write automated tests for document processing, OCR, spreadsheet parsing, AI extraction, 
validation, API endpoints, and failure scenarios. Evaluate the system using a labeled dataset 
and report actual extraction accuracy, processing time, document success rate, and failure rate. 
Technology Requirements 
Suggested technologies include React/Next.js, Node.js/NestJS, Python-based OCR tools, 
pandas/openpyxl, an LLM API, PostgreSQL or MongoDB, Redis/BullMQ, and a vector 
database. Candidates may choose alternatives and explain their design decisions. 
Deliverables 
Submit within five days: 
1.  Git repository containing the source code. 
2.  README and setup instructions. 
3.  Architecture diagram and database schema. 
4.  Working backend API and frontend. 
5.  PDF, image, Excel, and CSV processing pipelines. 
6.  Automated tests and test results. 
7.  Evaluation report with actual measured metrics. 
8.  Recorded walkthrough of the working application. 
9.  AI prompts and disclosure of AI-assisted development, where applicable. 
The candidate should ensure smooth performance, high speed, scalability, reliability, and strong 
security. 
>

