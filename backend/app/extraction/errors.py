from app.processing.errors import ProcessingError


class LLMNotConfiguredError(ProcessingError):
    code = "llm_not_configured"


class LLMUnavailableError(ProcessingError):
    """Rate limits, outages, and timeouts, after the client's own retries ran out."""

    code = "llm_unavailable"
    transient = True


class LLMRejectedError(ProcessingError):
    """The provider refused the request, for example a bad key. Retrying will not help."""

    code = "llm_rejected"


class LLMInvalidResponseError(ProcessingError):
    """The model did not return usable JSON. A later attempt may do better."""

    code = "llm_invalid_response"
    transient = True
