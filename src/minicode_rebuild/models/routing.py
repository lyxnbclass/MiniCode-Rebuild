"""Ordered model routing with conservative transient-error failover."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from minicode_rebuild.core import ModelAdapter, ModelRequest, ModelResponse
from minicode_rebuild.models.errors import (
    ModelHTTPError,
    ModelRoutingError,
    ModelTransportError,
)

MAX_MODEL_ROUTES = 5
MAX_ROUTE_NAME_LENGTH = 256


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """One named adapter candidate in priority order."""

    name: str
    adapter: ModelAdapter

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("model route name must be text")
        name = self.name.strip()
        if not name:
            raise ValueError("model route name must not be empty")
        if len(name) > MAX_ROUTE_NAME_LENGTH:
            raise ValueError(
                f"model route name must not exceed {MAX_ROUTE_NAME_LENGTH} characters"
            )
        if not name.isprintable():
            raise ValueError("model route name must not contain control characters")
        if not isinstance(self.adapter, ModelAdapter):
            raise TypeError("model route adapter must implement ModelAdapter")
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class RoutingEvent:
    """Redacted routing evidence suitable for terminal display."""

    route: str
    attempt: int
    status: str
    error_type: str | None = None
    retrying: bool = False


RoutingObserver = Callable[[RoutingEvent], None]


def _retryable(error: Exception) -> bool:
    if isinstance(error, ModelTransportError):
        return True
    return isinstance(error, ModelHTTPError) and error.retryable


class RoutingModelAdapter:
    """Try ordered models once each, only for known transient failures."""

    def __init__(
        self,
        routes: Iterable[ModelRoute],
        *,
        observer: RoutingObserver | None = None,
    ) -> None:
        normalized = tuple(routes)
        if not normalized:
            raise ValueError("at least one model route is required")
        if len(normalized) > MAX_MODEL_ROUTES:
            raise ValueError(f"at most {MAX_MODEL_ROUTES} model routes are allowed")
        if any(not isinstance(route, ModelRoute) for route in normalized):
            raise TypeError("routes must contain only ModelRoute instances")
        names = tuple(route.name for route in normalized)
        if len(set(names)) != len(names):
            raise ValueError("model route names must be unique")
        self._routes = normalized
        self._observer = observer

    @property
    def routes(self) -> tuple[ModelRoute, ...]:
        return self._routes

    def _notify(self, event: RoutingEvent) -> None:
        if self._observer is None:
            return
        try:
            self._observer(event)
        except Exception:
            return

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return the first success without retrying permanent or malformed errors."""

        failures: list[str] = []
        for index, route in enumerate(self._routes, start=1):
            try:
                response = route.adapter.complete(request)
            except Exception as exc:
                retrying = _retryable(exc) and index < len(self._routes)
                self._notify(
                    RoutingEvent(
                        route=route.name,
                        attempt=index,
                        status="failed",
                        error_type=type(exc).__name__,
                        retrying=retrying,
                    )
                )
                if not _retryable(exc):
                    raise
                failures.append(f"{route.name}:{type(exc).__name__}")
                if not retrying:
                    raise ModelRoutingError(tuple(failures)) from exc
                continue
            if index > 1:
                self._notify(
                    RoutingEvent(
                        route=route.name,
                        attempt=index,
                        status="selected",
                    )
                )
            return response
        raise AssertionError("validated routes unexpectedly produced no result")


__all__ = [
    "MAX_MODEL_ROUTES",
    "MAX_ROUTE_NAME_LENGTH",
    "ModelRoute",
    "RoutingEvent",
    "RoutingModelAdapter",
    "RoutingObserver",
]
