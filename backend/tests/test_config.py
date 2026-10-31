import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "APP_ENV",
        "DATABASE_URL",
        "CORS_ORIGINS",
        "LOG_LEVEL",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
    ):
        monkeypatch.delenv(name, raising=False)


def test_database_url_escapes_special_characters_in_password() -> None:
    url = make_url(Settings(_env_file=None, postgres_password="p@ss/w:rd#1").database_url)

    assert url.drivername == "postgresql+psycopg"
    assert url.password == "p@ss/w:rd#1"
    assert url.host == "localhost"


def test_database_url_env_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")

    assert Settings(_env_file=None).database_url == "postgresql+psycopg://u:p@db:5432/x"


def test_cors_origins_parsed_from_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://a.test, http://b.test ,")

    assert Settings(_env_file=None).cors_origins == ["http://a.test", "http://b.test"]


def test_secrets_are_masked_in_repr() -> None:
    settings = Settings(_env_file=None, postgres_password="hunter2", llm_api_key="key-123")

    assert "hunter2" not in repr(settings)
    assert "key-123" not in repr(settings)


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid log level"):
        Settings(_env_file=None, log_level="LOUD")


def test_log_level_normalized() -> None:
    assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"


def test_max_upload_bytes() -> None:
    assert Settings(_env_file=None, max_upload_mb=2).max_upload_bytes == 2 * 1024 * 1024


def test_max_upload_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_upload_mb=0)


def test_production_rejects_placeholder_password() -> None:
    with pytest.raises(ValidationError, match="POSTGRES_PASSWORD"):
        Settings(_env_file=None, app_env="production")


def test_production_rejects_wildcard_cors() -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        Settings(_env_file=None, app_env="production", postgres_password="s3cret", cors_origins="*")


def test_production_accepts_safe_config() -> None:
    settings = Settings(_env_file=None, app_env="production", postgres_password="s3cret")

    assert settings.app_env == "production"
