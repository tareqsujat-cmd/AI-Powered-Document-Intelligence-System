import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from app.core.logging import JsonFormatter, request_id_var, request_logger


def _record(message: str = "hello", **extra: object) -> logging.LogRecord:
    return logging.makeLogRecord(
        {"name": "t", "levelname": "INFO", "levelno": 20, "msg": message, **extra}
    )


def test_formatter_emits_one_json_line_with_extra_fields() -> None:
    line = JsonFormatter().format(_record(document_id="abc"))
    payload = json.loads(line)

    assert "\n" not in line
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["document_id"] == "abc"
    assert "levelno" not in payload


def test_formatter_escapes_newlines_to_prevent_log_injection() -> None:
    line = JsonFormatter().format(_record('evil\n{"level":"CRITICAL"}'))

    assert "\n" not in line
    assert json.loads(line)["message"] == 'evil\n{"level":"CRITICAL"}'


def test_formatter_includes_request_id_from_context() -> None:
    token = request_id_var.set("req-1")
    try:
        assert json.loads(JsonFormatter().format(_record()))["request_id"] == "req-1"
    finally:
        request_id_var.reset(token)


def test_formatter_includes_exception_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record(exc_info=sys.exc_info())

    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exception"]


def test_formatter_serializes_non_json_values() -> None:
    assert json.loads(JsonFormatter().format(_record(value={1})))["value"] == "{1}"


def test_valid_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "abc-123"})

    assert response.headers["x-request-id"] == "abc-123"


@pytest.mark.parametrize("bad_id", ["has spaces", "x" * 65, "semi;colon", "<script>"])
def test_unsafe_request_id_is_replaced(client: TestClient, bad_id: str) -> None:
    response = client.get("/health", headers={"X-Request-ID": bad_id})

    assert response.headers["x-request-id"] != bad_id
    assert len(response.headers["x-request-id"]) == 36


def test_request_id_generated_when_absent(client: TestClient) -> None:
    assert len(client.get("/health").headers["x-request-id"]) == 36


def test_request_completion_logged_without_query_string(client: TestClient) -> None:
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    request_logger.addHandler(handler)
    previous_level = request_logger.level
    request_logger.setLevel(logging.DEBUG)
    try:
        client.get("/health?secret=value")
    finally:
        request_logger.removeHandler(handler)
        request_logger.setLevel(previous_level)

    assert len(records) == 1
    assert records[0].path == "/health"
    assert records[0].status_code == 200
    assert records[0].duration_ms >= 0
