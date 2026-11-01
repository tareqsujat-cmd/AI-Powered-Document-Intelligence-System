from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL

PLACEHOLDER_SECRET = "change-me"  # noqa: S105


class Settings(BaseSettings):
    """Application configuration, read from the environment and .env files."""

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    app_name: str = "docintel"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = True

    postgres_user: str = "docintel"
    postgres_password: SecretStr = SecretStr(PLACEHOLDER_SECRET)
    postgres_db: str = "docintel"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    database_url_override: SecretStr | None = Field(default=None, alias="DATABASE_URL")

    redis_url: str = "redis://localhost:6379/0"

    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    storage_dir: Path = Path("/data/uploads")
    max_upload_mb: int = Field(default=200, gt=0)
    max_request_mb: int = Field(default=1024, gt=0)
    max_files_per_upload: int = Field(default=50, gt=0)

    rate_limit_uploads_per_minute: int = Field(default=30, ge=0)
    rate_limit_searches_per_minute: int = Field(default=120, ge=0)
    rate_limit_requests_per_minute: int = Field(default=600, ge=0)

    task_max_retries: int = Field(default=3, ge=0)
    task_retry_delay_seconds: int = Field(default=30, ge=0)
    # A document with no progress for this long has lost its worker. Must exceed the longest gap
    # between progress updates: one LLM call with all of its retries.
    stalled_after_seconds: int = Field(default=900, gt=0)

    task_soft_time_limit_seconds: int = Field(default=7200, gt=0)

    llm_provider: Literal["gemini", "openai_compatible"] = "gemini"
    llm_model: str = "gemini-3.5-flash-lite"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=5, ge=0)
    # Pacing per worker process, to stay inside provider quotas. 0 means no pacing.
    llm_requests_per_minute: int = Field(default=10, ge=0)
    llm_max_chunk_chars: int = Field(default=24_000, gt=0)
    llm_max_calls_per_document: int = Field(default=1500, gt=0)
    prompts_dir: Path | None = None

    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_batch_size: int = Field(default=32, gt=0)
    max_embedding_chunks_per_document: int = Field(default=5000, gt=0)

    ocr_lang: str = "eng"
    ocr_dpi: int = Field(default=300, gt=0)
    ocr_page_timeout_seconds: int = Field(default=120, gt=0)

    max_sheet_rows: int = Field(default=100_000, gt=0)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {value}")
        return level

    @model_validator(mode="after")
    def _reject_unsafe_production_config(self) -> "Settings":
        if self.app_env != "production":
            return self
        if self.database_url_override is None and (
            self.postgres_password.get_secret_value() == PLACEHOLDER_SECRET
        ):
            raise ValueError("POSTGRES_PASSWORD must be changed in production")
        if "*" in self.cors_origins:
            raise ValueError("CORS_ORIGINS must not contain '*' in production")
        return self

    @property
    def database_url(self) -> str:
        if self.database_url_override is not None:
            return self.database_url_override.get_secret_value()
        # URL.create escapes special characters in the password.
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
