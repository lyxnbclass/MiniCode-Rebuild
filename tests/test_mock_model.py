"""Tests for the deterministic mock model adapter."""

from __future__ import annotations

import pytest

from minicode_rebuild.core import (
    Message,
    MessageRole,
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from minicode_rebuild.models import MockModel
from minicode_rebuild.models.errors import ModelResponseError


def make_request(content: str = "hello") -> ModelRequest:
    return ModelRequest(
        messages=(Message(role=MessageRole.USER, content=content),),
    )


def test_mock_model_returns_scripted_responses_and_records_requests() -> None:
    text_response = ModelResponse(content="first", finish_reason="stop")
    tool_response = ModelResponse(
        tool_calls=(ToolCall(id="call-1", name="read_file"),),
        finish_reason="tool_calls",
    )
    model = MockModel([text_response, tool_response])
    first_request = make_request("first request")
    second_request = make_request("second request")

    assert model.complete(first_request) == text_response
    assert model.complete(second_request) == tool_response
    assert model.requests == (first_request, second_request)
    assert isinstance(model, ModelAdapter)


def test_mock_model_can_script_an_exception() -> None:
    expected = RuntimeError("provider unavailable")
    model = MockModel([expected])

    with pytest.raises(RuntimeError, match="provider unavailable") as caught:
        model.complete(make_request())

    assert caught.value is expected
    assert model.requests == (make_request(),)


def test_mock_model_reports_exhausted_script() -> None:
    model = MockModel([])

    with pytest.raises(ModelResponseError, match="exhausted"):
        model.complete(make_request())
