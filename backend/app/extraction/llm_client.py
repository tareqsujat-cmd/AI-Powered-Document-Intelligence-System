import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.extraction.errors import (
    LLMInvalidResponseError,
    LLMNotConfiguredError,
    LLMRejectedError,
    LLMUnavailableError,
)

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_BACKOFF_BASE_SECONDS = 2.0
_MAX_BACKOFF_SECONDS = 90.0
_GEMINI_RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")


@dataclass(frozen=True, slots=True)
class LLMResponse:
    data: dict[str, Any]
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> LLMResponse:
        """Return the model's JSON answer. `schema` is standard JSON Schema."""
        ...


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini takes an OpenAPI subset: uppercase types, and `nullable` rather than type unions."""
    converted: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type":
            kinds = value if isinstance(value, list) else [value]
            concrete = [kind for kind in kinds if kind != "null"]
            converted["type"] = concrete[0].upper()
            if "null" in kinds:
                converted["nullable"] = True
        elif key == "properties":
            converted[key] = {name: to_gemini_schema(prop) for name, prop in value.items()}
        elif key == "items":
            converted[key] = to_gemini_schema(value)
        elif key != "additionalProperties":
            converted[key] = value
    return converted


class _HttpLLMClient:
    """Retries, pacing, and error handling shared by every provider."""

    provider = "llm"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        pacer: Callable[[], float | None] | None = None,
    ) -> None:
        self._model = settings.llm_model
        self._max_retries = settings.llm_max_retries
        self._sleep = sleep
        self._pacer = pacer
        self._min_interval = (
            60.0 / settings.llm_requests_per_minute if settings.llm_requests_per_minute else 0.0
        )
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._http = httpx.Client(timeout=settings.llm_timeout_seconds, transport=transport)

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._http.close()

    def _pace(self) -> None:
        if not self._min_interval:
            return
        shared_wait = self._pacer() if self._pacer is not None else None
        if shared_wait is not None:
            if shared_wait > 0:
                self._sleep(shared_wait)
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_request_at - now
            self._next_request_at = max(now, self._next_request_at) + self._min_interval
        if wait > 0:
            self._sleep(wait)

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            hinted = self._server_retry_hint(response)
            if hinted is not None:
                return float(min(hinted, _MAX_BACKOFF_SECONDS))
        backoff = _BACKOFF_BASE_SECONDS * 2**attempt
        # Jitter, so that workers throttled together do not all come back at the same moment.
        return float(min(backoff + random.uniform(0, 1), _MAX_BACKOFF_SECONDS))  # noqa: S311

    def _server_retry_hint(self, response: httpx.Response) -> float | None:
        header = response.headers.get("retry-after")
        if header:
            try:
                return float(header)
            except ValueError:
                return None
        return None

    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        """POST with retries. The API key travels in a header, so it never appears in a URL log."""
        last_problem = "no response"
        for attempt in range(self._max_retries + 1):
            self._pace()
            response: httpx.Response | None = None
            try:
                response = self._http.post(url, json=body, headers=headers)
            except httpx.TimeoutException:
                last_problem = "the request timed out"
            except httpx.TransportError as exc:
                last_problem = f"a network error ({type(exc).__name__})"
            else:
                if response.status_code < 400:
                    try:
                        parsed: dict[str, Any] = response.json()
                    except ValueError as exc:
                        raise LLMInvalidResponseError(
                            f"The {self.provider} API returned a response that is not JSON."
                        ) from exc
                    return parsed
                if response.status_code not in _RETRYABLE_STATUS:
                    logger.warning(
                        "llm request rejected",
                        extra={"status": response.status_code, "provider": self.provider},
                    )
                    raise LLMRejectedError(
                        f"The {self.provider} API rejected the request "
                        f"(HTTP {response.status_code}). Check the model name and API key."
                    )
                last_problem = f"HTTP {response.status_code}"

            if attempt < self._max_retries:
                delay = self._retry_delay(response, attempt)
                logger.warning(
                    "llm request will be retried",
                    extra={"problem": last_problem, "attempt": attempt + 1, "delay_s": delay},
                )
                self._sleep(delay)

        raise LLMUnavailableError(
            f"The {self.provider} API is unavailable after {self._max_retries + 1} "
            f"attempts ({last_problem})."
        )


def _loads_json_object(text: str, provider: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LLMInvalidResponseError(f"The {provider} model did not return valid JSON.") from exc
    if not isinstance(data, dict):
        raise LLMInvalidResponseError(f"The {provider} model did not return a JSON object.")
    return data


class GeminiClient(_HttpLLMClient):
    provider = "Gemini"

    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
            raise LLMNotConfiguredError("LLM_API_KEY is not set.")
        super().__init__(settings, **kwargs)
        self._api_key = settings.llm_api_key.get_secret_value()
        self._base_url = (settings.llm_base_url or GEMINI_BASE_URL).rstrip("/")

    def _server_retry_hint(self, response: httpx.Response) -> float | None:
        header_hint = super()._server_retry_hint(response)
        if header_hint is not None:
            return header_hint
        try:
            details = response.json()["error"]["details"]
        except (ValueError, KeyError, TypeError):
            return None
        for detail in details if isinstance(details, list) else []:
            match = _GEMINI_RETRY_DELAY.match(str(detail.get("retryDelay", "")))
            if match:
                return float(match[1])
        return None

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> LLMResponse:
        payload = self._post(
            f"{self._base_url}/models/{self._model}:generateContent",
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseSchema": to_gemini_schema(schema),
                },
            },
            {"x-goog-api-key": self._api_key},
        )
        candidates = payload.get("candidates") or []
        if not candidates:
            reason = (payload.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMInvalidResponseError(f"Gemini returned no answer ({reason}).")
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        if not text:
            reason = candidate.get("finishReason", "empty answer")
            raise LLMInvalidResponseError(f"Gemini returned no text ({reason}).")
        if candidate.get("finishReason") == "MAX_TOKENS":
            raise LLMInvalidResponseError("Gemini's answer was cut off at its token limit.")
        usage = payload.get("usageMetadata") or {}
        return LLMResponse(
            data=_loads_json_object(text, self.provider),
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )


class OpenAICompatibleClient(_HttpLLMClient):
    """OpenAI's API, and the many providers and local servers that copy its shape."""

    provider = "OpenAI-compatible"

    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        if not settings.llm_base_url:
            raise LLMNotConfiguredError("LLM_BASE_URL is required for this LLM provider.")
        super().__init__(settings, **kwargs)
        self._base_url = settings.llm_base_url.rstrip("/")
        self._headers = (
            {"Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}"}
            if settings.llm_api_key is not None and settings.llm_api_key.get_secret_value()
            else {}
        )

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> LLMResponse:
        payload = self._post(
            f"{self._base_url}/chat/completions",
            {
                "model": self._model,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "extraction", "schema": schema},
                },
            },
            self._headers,
        )
        try:
            choice = payload["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMInvalidResponseError("The model returned no answer.") from exc
        if choice.get("finish_reason") == "length":
            raise LLMInvalidResponseError("The model's answer was cut off at its token limit.")
        usage = payload.get("usage") or {}
        return LLMResponse(
            data=_loads_json_object(text or "", self.provider),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )


def create_llm_client(settings: Settings, **kwargs: Any) -> GeminiClient | OpenAICompatibleClient:
    if settings.llm_provider == "gemini":
        return GeminiClient(settings, **kwargs)
    return OpenAICompatibleClient(settings, **kwargs)
