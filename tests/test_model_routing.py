from __future__ import annotations

import pytest

from minicode_rebuild.core import Message, MessageRole, ModelRequest, ModelResponse
from minicode_rebuild.models import MockModel, ModelRoute, RoutingModelAdapter
from minicode_rebuild.models.errors import (
    ModelHTTPError,
    ModelResponseError,
    ModelRoutingError,
    ModelTransportError,
)
from minicode_rebuild.models.routing import RoutingEvent

REQUEST = ModelRequest(
    messages=(Message(role=MessageRole.USER, content="help"),)
)


def route(name: str, *responses: ModelResponse | Exception) -> ModelRoute:
    return ModelRoute(name, MockModel(responses))


def test_primary_success_does_not_touch_fallback() -> None:
    primary_model = MockModel([ModelResponse(content="primary")])
    fallback_model = MockModel([ModelResponse(content="fallback")])
    events: list[RoutingEvent] = []
    adapter = RoutingModelAdapter(
        (
            ModelRoute("primary", primary_model),
            ModelRoute("fallback", fallback_model),
        ),
        observer=events.append,
    )

    response = adapter.complete(REQUEST)

    assert response.content == "primary"
    assert len(primary_model.requests) == 1
    assert fallback_model.requests == ()
    assert events == []


@pytest.mark.parametrize(
    "failure",
    [
        ModelTransportError("offline"),
        ModelHTTPError(408, "timeout"),
        ModelHTTPError(409, "conflict"),
        ModelHTTPError(429, "busy"),
        ModelHTTPError(503, "unavailable"),
    ],
)
def test_transient_failure_routes_to_fallback(failure: Exception) -> None:
    events: list[RoutingEvent] = []
    adapter = RoutingModelAdapter(
        (
            route("primary", failure),
            route("fallback", ModelResponse(content="recovered")),
        ),
        observer=events.append,
    )

    response = adapter.complete(REQUEST)

    assert response.content == "recovered"
    assert events == [
        RoutingEvent(
            route="primary",
            attempt=1,
            status="failed",
            error_type=type(failure).__name__,
            retrying=True,
        ),
        RoutingEvent(route="fallback", attempt=2, status="selected"),
    ]


@pytest.mark.parametrize(
    "failure",
    [
        ModelHTTPError(400, "bad request"),
        ModelHTTPError(401, "invalid key"),
        ModelHTTPError(403, "forbidden"),
        ModelResponseError("malformed response"),
        ValueError("adapter bug"),
    ],
)
def test_permanent_or_malformed_failure_does_not_fallback(
    failure: Exception,
) -> None:
    fallback = MockModel([ModelResponse(content="must not run")])
    adapter = RoutingModelAdapter(
        (
            route("primary", failure),
            ModelRoute("fallback", fallback),
        )
    )

    with pytest.raises(type(failure)):
        adapter.complete(REQUEST)

    assert fallback.requests == ()


def test_all_transient_routes_fail_with_redacted_summary() -> None:
    adapter = RoutingModelAdapter(
        (
            route("primary", ModelTransportError("secret-one")),
            route("fallback", ModelHTTPError(503, "secret-two")),
        )
    )

    with pytest.raises(ModelRoutingError) as raised:
        adapter.complete(REQUEST)

    assert raised.value.attempts == (
        "primary:ModelTransportError",
        "fallback:ModelHTTPError",
    )
    assert "secret-one" not in str(raised.value)
    assert "secret-two" not in str(raised.value)


def test_fallback_receives_the_identical_normalized_request() -> None:
    request = ModelRequest(
        messages=REQUEST.messages,
        max_output_tokens=123,
    )
    primary = MockModel([ModelTransportError("offline")])
    fallback = MockModel([ModelResponse(content="recovered")])
    adapter = RoutingModelAdapter(
        (
            ModelRoute("primary", primary),
            ModelRoute("fallback", fallback),
        )
    )

    adapter.complete(request)

    assert primary.requests == (request,)
    assert fallback.requests == (request,)


def test_route_observer_failure_does_not_break_recovery() -> None:
    adapter = RoutingModelAdapter(
        (
            route("primary", ModelTransportError("offline")),
            route("fallback", ModelResponse(content="recovered")),
        ),
        observer=lambda _event: (_ for _ in ()).throw(RuntimeError("observer")),
    )

    assert adapter.complete(REQUEST).content == "recovered"


def test_control_flow_exception_propagates_without_fallback() -> None:
    class InterruptingModel:
        def complete(self, _request: ModelRequest) -> ModelResponse:
            raise KeyboardInterrupt

    fallback = MockModel([ModelResponse(content="must not run")])
    adapter = RoutingModelAdapter(
        (
            ModelRoute("primary", InterruptingModel()),
            ModelRoute("fallback", fallback),
        )
    )

    with pytest.raises(KeyboardInterrupt):
        adapter.complete(REQUEST)

    assert fallback.requests == ()


@pytest.mark.parametrize(
    "routes",
    [
        (),
        tuple(route(str(index), ModelResponse()) for index in range(6)),
        (route("same", ModelResponse()), route("same", ModelResponse())),
    ],
)
def test_router_rejects_invalid_route_sets(routes: tuple[ModelRoute, ...]) -> None:
    with pytest.raises(ValueError):
        RoutingModelAdapter(routes)


def test_model_route_validates_name_and_adapter() -> None:
    with pytest.raises(ValueError, match="name"):
        ModelRoute(" ", MockModel([]))
    with pytest.raises(TypeError, match="adapter"):
        ModelRoute("invalid", object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="name"):
        ModelRoute(1, MockModel([]))  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["primary\nforged", "primary\x1b[31m", "x" * 257])
def test_model_route_rejects_unsafe_terminal_names(name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        ModelRoute(name, MockModel([]))
