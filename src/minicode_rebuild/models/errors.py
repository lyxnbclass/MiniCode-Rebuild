"""Exceptions raised by model adapters."""


class ModelError(RuntimeError):
    """Base class for model-layer failures."""


class ModelTransportError(ModelError):
    """Raised when a request cannot reach the provider."""


class ModelResponseError(ModelError):
    """Raised when a provider response cannot be normalized safely."""
