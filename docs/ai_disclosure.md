# AI-assisted development log

This project is built with **Claude Code** (model: Claude Opus 5.5), directed by the candidate. The instructions given to the assistant, including the master prompt and the assignment text, are in [CLAUDE.md](../CLAUDE.md). Prompts used by the application itself at runtime are in [/prompts](../prompts).

## Log

### Phase 0: scaffold, schema, infrastructure

- **Assistant generated:** repository structure, docker-compose, Dockerfiles, Makefile, configuration, logging, health endpoint, Celery setup, database models, tests, and documentation. A subagent generated the Next.js scaffold.
- **Alembic generated:** the initial migration, by autogenerate. The assistant then edited it to add the `vector` extension, fix a missing import, and add an index.
- **Verified by running:** backend tests on real PostgreSQL 16.2 with pgvector 0.6.2 (embedded via `pgserver`), the Alembic CLI, and a real uvicorn boot.
- **Not verified:** the Docker images and compose stack, because Docker was not available on the development machine during this phase.
