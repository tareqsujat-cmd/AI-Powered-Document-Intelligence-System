FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMP_THREAD_LIMIT=1 \
    HF_HOME=/opt/huggingface
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# CPU-only torch, because the default build bundles gigabytes of GPU libraries. The embedding model
# is included in the image, so that containers start without needing to download it.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.9.1 \
    && pip install -r requirements.txt \
    && python -c "from sentence_transformers import SentenceTransformer as S; S('sentence-transformers/all-MiniLM-L6-v2')" \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data/uploads \
    && chown -R app:app /data /app
ENV HF_HUB_OFFLINE=1

FROM base AS runtime
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY --from=prompts . ./prompts
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY . .
COPY --from=prompts . ./prompts
RUN chown -R app:app /app
USER app
CMD ["pytest", "tests", "-q"]
