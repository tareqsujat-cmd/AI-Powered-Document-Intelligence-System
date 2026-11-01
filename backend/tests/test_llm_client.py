import json
from collections.abc import Callable

import httpx
import pytest

from app.core.config import Settings
from app.extraction.errors import (
    LLMInvalidResponseError,
    LLMNotConfiguredError,
    LLMRejectedError,
    LLMUnavailableError,
)
from app.extraction.llm_client import (
    GeminiClient,
    OpenAICompatibleClient,
    create_llm_client,
    to_gemini_schema,
)

SECRET = "super-secret-key-value"
SCHEMA = {
    "type": "object",
    "properties": {"total": {"type": ["string", "null"]}},
    "required": ["total"],
}


def gemini_ok(text: str = '{"total": "10.00"}', **extra: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": text}]}, "finishReason": "STOP", **extra}
            ],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7},
        },
    )


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "llm_api_key": SECRET,
        "llm_model": "test-model",
        "llm_requests_per_minute": 0,
        "llm_max_retries": 2,
    }
    return Settings(_env_file=None, **{**values, **overrides})


def make_client(
    handler: Callable[[httpx.Request], httpx.Response], **overrides: object
) -> tuple[GeminiClient, list[float]]:
    sleeps: list[float] = []
    client = GeminiClient(
        make_settings(**overrides),
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    return client, sleeps


def test_gemini_sends_schema_prompt_and_key_in_a_header() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return gemini_ok()

    client, _ = make_client(handler)
    response = client.generate_json("the prompt", SCHEMA)

    request = seen["request"]
    body = json.loads(request.content)
    assert request.url.path.endswith("/models/test-model:generateContent")
    assert request.headers["x-goog-api-key"] == SECRET
    assert SECRET not in str(request.url)
    assert body["contents"][0]["parts"][0]["text"] == "the prompt"
    assert body["generationConfig"]["temperature"] == 0
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseSchema"]["properties"]["total"] == {
        "type": "STRING",
        "nullable": True,
    }
    assert response.data == {"total": "10.00"}
    assert (response.input_tokens, response.output_tokens) == (11, 7)
    assert client.model == "test-model"


def test_to_gemini_schema_handles_nesting_and_drops_unsupported_keys() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"n": {"type": "number"}}},
            }
        },
    }

    assert to_gemini_schema(schema) == {
        "type": "OBJECT",
        "properties": {
            "items": {
                "type": "ARRAY",
                "items": {"type": "OBJECT", "properties": {"n": {"type": "NUMBER"}}},
            }
        },
    }


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_status_is_retried_until_it_succeeds(status: int) -> None:
    responses = [httpx.Response(status), gemini_ok()]
    client, sleeps = make_client(lambda _request: responses.pop(0))

    assert client.generate_json("p", SCHEMA).data == {"total": "10.00"}
    assert len(sleeps) == 1


def test_gives_up_after_the_retry_limit_with_a_transient_error() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client, sleeps = make_client(handler, llm_max_retries=3)

    with pytest.raises(LLMUnavailableError, match="HTTP 503") as caught:
        client.generate_json("p", SCHEMA)

    assert calls == 4
    assert len(sleeps) == 3
    assert caught.value.transient is True


def test_backoff_grows_exponentially() -> None:
    client, sleeps = make_client(lambda _request: httpx.Response(503), llm_max_retries=3)

    with pytest.raises(LLMUnavailableError):
        client.generate_json("p", SCHEMA)

    assert 2 <= sleeps[0] < 3
    assert 4 <= sleeps[1] < 5
    assert 8 <= sleeps[2] < 9


def test_retry_after_header_is_respected() -> None:
    responses = [httpx.Response(429, headers={"retry-after": "37"}), gemini_ok()]
    client, sleeps = make_client(lambda _request: responses.pop(0))

    client.generate_json("p", SCHEMA)

    assert sleeps == [37.0]


def test_gemini_retry_delay_in_the_body_is_respected() -> None:
    quota_error = {
        "error": {
            "code": 429,
            "details": [
                {"@type": "type.googleapis.com/google.rpc.Help"},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"},
            ],
        }
    }
    responses = [httpx.Response(429, json=quota_error), gemini_ok()]
    client, sleeps = make_client(lambda _request: responses.pop(0))

    client.generate_json("p", SCHEMA)

    assert sleeps == [21.0]


def test_server_retry_hint_is_capped() -> None:
    responses = [httpx.Response(429, headers={"retry-after": "86400"}), gemini_ok()]
    client, sleeps = make_client(lambda _request: responses.pop(0))

    client.generate_json("p", SCHEMA)

    assert sleeps == [90.0]


@pytest.mark.parametrize("error", [httpx.ConnectError("x"), httpx.ReadTimeout("x")])
def test_network_problems_are_retried(error: Exception) -> None:
    attempts = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise error
        return gemini_ok()

    client, _ = make_client(handler)

    assert client.generate_json("p", SCHEMA).data == {"total": "10.00"}


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_rejection_fails_without_retrying(status: int) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": f"key {SECRET} is bad"}})

    client, sleeps = make_client(handler)

    with pytest.raises(LLMRejectedError) as caught:
        client.generate_json("p", SCHEMA)

    assert calls == 1
    assert sleeps == []
    assert caught.value.transient is False
    assert SECRET not in caught.value.reason


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"candidates": []}, "no candidates"),
        ({"promptFeedback": {"blockReason": "SAFETY"}}, "SAFETY"),
        ({"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}, "SAFETY"),
        (
            {
                "candidates": [
                    {"content": {"parts": [{"text": '{"a'}]}, "finishReason": "MAX_TOKENS"}
                ]
            },
            "cut off",
        ),
        ({"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}, "valid JSON"),
        ({"candidates": [{"content": {"parts": [{"text": "[1, 2]"}]}}]}, "JSON object"),
    ],
)
def test_unusable_gemini_answers_raise_invalid_response(payload: dict, match: str) -> None:
    client, _ = make_client(lambda _request: httpx.Response(200, json=payload))

    with pytest.raises(LLMInvalidResponseError, match=match):
        client.generate_json("p", SCHEMA)


def test_non_json_http_body_raises_invalid_response() -> None:
    client, _ = make_client(lambda _request: httpx.Response(200, text="<html>proxy error</html>"))

    with pytest.raises(LLMInvalidResponseError):
        client.generate_json("p", SCHEMA)


def test_pacing_waits_between_requests() -> None:
    client, sleeps = make_client(lambda _request: gemini_ok(), llm_requests_per_minute=6)

    client.generate_json("p", SCHEMA)
    client.generate_json("p", SCHEMA)

    assert len(sleeps) == 1
    assert 9 < sleeps[0] <= 10


def test_missing_api_key_is_a_clear_error() -> None:
    with pytest.raises(LLMNotConfiguredError, match="LLM_API_KEY"):
        GeminiClient(Settings(_env_file=None))
    with pytest.raises(LLMNotConfiguredError, match="LLM_API_KEY"):
        GeminiClient(Settings(_env_file=None, llm_api_key=""))


def test_openai_compatible_client_uses_the_standard_shape() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"total": "5"}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    client = OpenAICompatibleClient(
        make_settings(llm_provider="openai_compatible", llm_base_url="https://llm.test/v1/"),
        transport=httpx.MockTransport(handler),
    )
    response = client.generate_json("the prompt", SCHEMA)

    request = seen["request"]
    body = json.loads(request.content)
    assert str(request.url) == "https://llm.test/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert body["model"] == "test-model"
    assert body["messages"][0]["content"] == "the prompt"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert response.data == {"total": "5"}
    assert response.input_tokens == 3


def test_openai_compatible_works_without_a_key_for_local_servers() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = OpenAICompatibleClient(
        make_settings(
            llm_provider="openai_compatible", llm_base_url="http://ollama.test/v1", llm_api_key=None
        ),
        transport=httpx.MockTransport(handler),
    )

    client.generate_json("p", SCHEMA)

    assert "authorization" not in seen["request"].headers


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"choices": []}, "no answer"),
        ({"choices": [{"message": {"content": '{"a'}, "finish_reason": "length"}]}, "cut off"),
        ({"choices": [{"message": {"content": "nope"}}]}, "valid JSON"),
    ],
)
def test_openai_compatible_unusable_answers(payload: dict, match: str) -> None:
    client = OpenAICompatibleClient(
        make_settings(llm_provider="openai_compatible", llm_base_url="https://llm.test/v1"),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
    )

    with pytest.raises(LLMInvalidResponseError, match=match):
        client.generate_json("p", SCHEMA)


def test_openai_compatible_requires_a_base_url() -> None:
    with pytest.raises(LLMNotConfiguredError, match="LLM_BASE_URL"):
        OpenAICompatibleClient(make_settings(llm_provider="openai_compatible"))


def test_factory_selects_the_provider_from_settings() -> None:
    assert isinstance(create_llm_client(make_settings()), GeminiClient)
    assert isinstance(
        create_llm_client(
            make_settings(llm_provider="openai_compatible", llm_base_url="https://llm.test/v1")
        ),
        OpenAICompatibleClient,
    )


def test_secret_is_not_in_the_client_repr() -> None:
    assert SECRET not in repr(make_settings())
