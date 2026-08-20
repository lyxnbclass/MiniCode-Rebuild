"""Exceptions raised by model adapters."""


class ModelError(RuntimeError):
    """Base class for model-layer failures."""


class ModelTransportError(ModelError):
    """Raised when a request cannot reach the provider."""


class ModelResponseError(ModelError):
    """Raised when a provider response cannot be normalized safely."""


class ModelHTTPError(ModelResponseError):
    """HTTP response failure with retryability derived only from its status."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.provider_message = message
        super().__init__(
            f"OpenAI-compatible API returned {status}: {message}"
        )

    @property
    def retryable(self) -> bool:
        return self.status in {408, 409, 425, 429} or 500 <= self.status <= 599


class ModelRoutingError(ModelError):
    """Raised after every eligible model route failed transiently."""

    def __init__(self, attempts: tuple[str, ...]) -> None:
        self.attempts = attempts
        summary = ", ".join(attempts)
        super().__init__(f"All model routes failed ({summary})")
