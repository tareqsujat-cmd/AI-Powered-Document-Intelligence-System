# Architecture

Status key: **built** exists in the repository today; **planned** is the design for a later phase (see [plan.md](plan.md)).

## System overview

```mermaid
flowchart LR
    user([Reviewer in browser])

    subgraph compose[docker-compose]
        fe[Next.js frontend]
        api[FastAPI backend]
        worker[Celery worker<br/>OCR, LLM, embeddings]
        redis[(Redis<br/>broker and results)]
        pg[(PostgreSQL + pgvector)]
        files[(Uploads volume)]
        migrate[Alembic migrate<br/>one-shot]
    end

    llm[LLM API<br/>default Gemini]

    user --> fe
    user -->|REST| api
    api -->|enqueue| redis
    redis -->|deliver| worker
    api --> pg
    api --> files
    worker --> pg
    worker --> files
    worker -->|JSON schema prompt| llm
    migrate --> pg
```

The API only validates, stores and enqueues. Everything slow runs in workers, so scaling means adding worker replicas, and the API stays responsive while a 1000-page scan is being processed.

## Processing pipeline (planned, Phases 1 to 3)

```mermaid
flowchart TD
    upload[Upload: magic-byte sniff, size limit, sanitize name, sha256] --> queue[status=queued]
    queue --> kind{File type}
    kind -->|PDF| pdf[Stream page by page]
    pdf --> layer{Text layer?}
    layer -->|yes| text[Use text layer]
    layer -->|no| ocr[OpenCV preprocess, then Tesseract]
    kind -->|JPG, PNG, TIFF frames| ocr
    kind -->|XLSX, XLS, CSV| sheet[All sheets, header detection, encoding detection]
    text --> store[Store page text]
    ocr --> store
    sheet --> rules{Columns map by rules?}
    rules -->|yes| validate
    rules -->|no| store
    store --> llm[LLM extraction: chunked, per-field confidence, null if unknown]
    llm --> guard[Hallucination guard: values must appear in the source]
    guard --> validate[Validate: arithmetic, duplicates, cross-document]
    validate --> index[Embed chunks with MiniLM]
    index --> done[completed, needs_review if flagged]
    pdf -.->|corrupt or encrypted| failed[status=failed with reason, retryable]
    sheet -.->|corrupt or empty| failed
```

## Database schema (built)

Migration: [0001_initial_schema.py](../backend/migrations/versions/0001_initial_schema.py). Models: [models.py](../backend/app/db/models.py). A test fails if the two drift apart.

```mermaid
erDiagram
    documents ||--o{ document_pages : has
    documents ||--o{ extracted_text : has
    documents ||--o{ extraction_results : has
    documents ||--o{ validation_errors : has
    documents ||--o{ processing_history : has
    documents ||--o{ embeddings : has
    document_pages ||--o| extracted_text : "text of"
    extraction_results ||--o{ line_items : has
    extraction_results ||--o{ validation_errors : has

    documents {
        uuid id PK
        string original_filename
        string storage_path
        string sha256
        string file_type
        string mime_type
        bigint size_bytes
        int page_count
        string status
        string stage
        smallint progress
        bool needs_review
        text failure_reason
        bool is_retryable
        int attempt_count
        string task_id
        timestamptz created_at
        timestamptz updated_at
        timestamptz processing_started_at
        timestamptz processing_finished_at
    }
    document_pages {
        uuid id PK
        uuid document_id FK
        int page_number
        string sheet_name
        string source
        string status
        float ocr_confidence
        int processing_ms
        text error
        timestamptz created_at
    }
    extracted_text {
        uuid id PK
        uuid document_id FK
        uuid page_id FK
        text content
        tsvector content_tsv
        timestamptz created_at
    }
    extraction_results {
        uuid id PK
        uuid document_id FK
        int invoice_index
        int version
        bool is_current
        int page_start
        int page_end
        string invoice_number
        date invoice_date
        string supplier_name
        string customer_name
        numeric subtotal
        numeric tax
        numeric total
        string currency
        jsonb field_metadata
        float overall_confidence
        string duplicate_key
        string method
        string llm_model
        string prompt_version
        jsonb raw_response
        timestamptz created_at
        timestamptz updated_at
    }
    line_items {
        uuid id PK
        uuid extraction_result_id FK
        int position
        text description
        numeric quantity
        numeric unit_price
        numeric amount
        jsonb field_metadata
    }
    validation_errors {
        uuid id PK
        uuid document_id FK
        uuid extraction_result_id FK
        uuid related_document_id FK
        string code
        string severity
        string field
        text message
        text expected_value
        text actual_value
        bool is_resolved
        timestamptz created_at
    }
    processing_history {
        bigint id PK
        uuid document_id FK
        string event
        string stage
        int attempt
        int duration_ms
        text message
        jsonb before
        jsonb after
        string actor
        timestamptz created_at
    }
    embeddings {
        uuid id PK
        uuid document_id FK
        int page_number
        int chunk_index
        text content
        vector embedding
        string model
        timestamptz created_at
    }
```

`validation_errors.related_document_id` also references `documents`, naming the other file in a duplicate or cross-document finding. It becomes NULL if that file is deleted, so the finding survives.

`field_metadata` holds the per-field evidence: `{"total": {"confidence": 0.0, "reason": "not found", "source_snippet": null, "source_page": null}}`.

## Design decisions

| Decision | Why | Trade-off accepted |
|---|---|---|
| One document has many `extraction_results` (`invoice_index`, page range) | A 1000-page PDF is usually a batch of invoices, not one invoice | Results must be fetched per invoice, not per file |
| Results are versioned; a partial unique index allows one `is_current` row per invoice | Reprocessing must not destroy earlier results or corrections | More rows; queries must filter on `is_current` |
| Invoice fields are typed nullable columns plus `field_metadata` JSONB | Columns give arithmetic and filtering; `NULL` plus a reason means values are never invented | Field evidence is not relationally queryable |
| `NUMERIC(18,4)` for money | Floats cannot hold the 0.01 validation tolerance reliably | None worth noting |
| Duplicates are a non-unique `duplicate_key` hash | The rule is to flag duplicates for review, not to reject them | Enforced by application code, not by the database |
| One text row per page, not one blob per document | Pages stream in one at a time; search hits can cite a page; retries can resume | 1000 rows for a 1000-page PDF |
| Generated `tsvector` indexes only the first 200,000 characters of a page | Without this, a large sheet fails the INSERT. Measured: 7.9 MB vector against the 1 MB limit | Keyword search misses text beyond that prefix; the full text is still stored and embedded |
| pgvector rather than a separate vector database | One system to run and back up; filters and vectors can join in one query. HNSW keeps queries fast at this scale | Would need revisiting at hundreds of millions of vectors |
| VARCHAR + CHECK rather than native Postgres enums | Adding a value needs no `ALTER TYPE` | Slightly larger storage |
| Sync SQLAlchemy with psycopg3 for both API and workers | One set of models and sessions; the API does little work per request, and FastAPI runs sync routes in a threadpool | Lower peak connection concurrency than async, addressed by adding replicas |
| Celery `acks_late`, prefetch 1, Redis visibility timeout above the task time limit | A crashed worker's document is redelivered. A timeout shorter than the longest OCR job would run the same document twice at once | Tasks must be idempotent (Phase 3) |
| One-shot `migrate` service | Backend and worker replicas never race on migrations | One extra compose service |
| Postgres and Redis ports bind to loopback only | Neither should be reachable from the network; Redis has no password in this setup | Remote deployment would need a password and TLS |
| Readiness failure details go to logs only | The response must not expose hostnames or credentials | Diagnosing requires log access |
