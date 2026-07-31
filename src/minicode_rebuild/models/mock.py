"""Deterministic scripted model for tests and demonstrations."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from minicode_rebuild.core import ModelRequest, ModelResponse
from minicode_rebuild.models.errors import ModelResponseError


class MockModel:
    """Return scripted responses or exceptions in insertion order."""

    def __init__(
        self,
        script: Iterable[ModelResponse | Exception],
    ) -> None:
        self._script = deque(script)
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        """Return an immutable snapshot of requests received so far."""
        return tuple(self._requests)

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Record a request and return the next scripted item."""
        self._requests.append(request)
        if not self._script:
            raise ModelResponseError("mock model response script is exhausted")

        item = self._script.popleft()
        if isinstance(item, Exception):
            raise item
        return item
