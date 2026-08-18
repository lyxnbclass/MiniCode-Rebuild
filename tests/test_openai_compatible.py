"""Tests for the non-streaming OpenAI-compatible adapter."""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass, field
from typing import Any

import pytest

from minicode_rebuild.config import ModelSettings
from minicode_rebuild.core import (
    Message,
    MessageRole,
    ModelRequest,
    ModelTool,
    TokenUsage,
    ToolCall,
)
from minicode_rebuild.models.errors import ModelResponseError, ModelTransportError
from minicode_rebuild.models.openai_compatible import (
    HttpResponse,
    OpenAICompatibleAdapter,
)


@dataclass
class FakeTransport:
    responses: list[HttpResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: int,
    ) -> HttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


def json_response(payload: dict[str, Any], status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
    )


def settings() -> ModelSettings:
    return ModelSettings(
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        api_key="test-secret",
        timeout_seconds=30,
    )


def request(*messages: Message, tools: tuple[ModelTool, ...] = ()) -> ModelRequest:
    return ModelRequest(messages=messages, tools=tools)


def test_adapter_normalizes_text_response_and_usage() -> None:
    transport = FakeTransport(
        [
            json_response(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                    },
                }
            )
        ]
    )
    adapter = OpenAICompatibleAdapter(settings(), transport=transport)

    response = adapter.complete(
        request(Message(role=MessageRole.USER, content="Hi"))
    )

    assert response.content == "Hello"
    assert response.finish_reason == "stop"
    assert response.usage == TokenUsage(input_tokens=7, output_tokens=3)
    call = transport.calls[0]
    assert call["url"] == "https://api.deepseek.com/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer test-secret"
    assert call["payload"] == {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    assert call["timeout"] == 30


def test_adapter_sends_normalized_output_limit() -> None:
    transport = FakeTransport(
        [
            json_response(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
        ]
    )
    adapter = OpenAICompatibleAdapter(settings(), transport=transport)

    adapter.complete(
        ModelRequest(
            messages=(Message(role=MessageRole.USER, content="Hi"),),
            max_output_tokens=321,
        )
    )

    assert transport.calls[0]["payload"]["max_tokens"] == 321


def test_adapter_serializes_tools_and_normalizes_tool_calls() -> None:
    transport = FakeTransport(
        [
            json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":"README.md"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
        ]
    )
    adapter = OpenAICompatibleAdapter(settings(), transport=transport)
    tool = ModelTool(
        name="read_file",
        description="Read a workspace file.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )

    response = adapter.complete(
        request(
            Message(role=MessageRole.USER, content="Read README"),
            tools=(tool,),
        )
    )

    assert response.tool_calls == (
        ToolCall(
            id="call-1",
            name="read_file",
            arguments={"path": "README.md"},
        ),
    )
    assert response.finish_reason == "tool_calls"
    assert transport.calls[0]["payload"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a workspace file.",
                "parameters": tool.parameters,
            },
        }
    ]


def test_adapter_serializes_assistant_calls_and_tool_results() -> None:
    transport = FakeTransport(
        [
            json_response(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "Done"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
        ]
    )
    adapter = OpenAICompatibleAdapter(settings(), transport=transport)
    tool_call = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "README.md"},
    )

    adapter.complete(
        request(
            Message(role=MessageRole.USER, content="Read README"),
            Message(
                role=MessageRole.ASSISTANT,
                tool_calls=(tool_call,),
            ),
            Message(
                role=MessageRole.TOOL,
                content="file contents",
                tool_call_id="call-1",
            ),
        )
    )

    assert transport.calls[0]["payload"]["messages"] == [
        {"role": "user", "content": "Read README"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "README.md"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": "file contents",
            "tool_call_id": "call-1",
        },
    ]


def test_adapter_surfaces_http_error_message() -> None:
    transport = FakeTransport(
        [
            json_response(
                {"error": {"message": "invalid credentials"}},
                status=401,
            )
        ]
    )
    adapter = OpenAICompatibleAdapter(settings(), transport=transport)

    with pytest.raises(ModelResponseError, match="invalid credentials"):
        adapter.complete(request(Message(role=MessageRole.USER, content="Hi")))


def test_default_transport_wraps_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_request(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", fail_request)
    adapter = OpenAICompatibleAdapter(settings())

    with pytest.raises(ModelTransportError, match="offline"):
        adapter.complete(request(Message(role=MessageRole.USER, content="Hi")))


@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(status=200, body=b"not json"),
        json_response({}),
        json_response({"choices": []}),
        json_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": "{not-json}",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ),
    ],
)
def test_adapter_rejects_malformed_success_response(response: HttpResponse) -> None:
    adapter = OpenAICompatibleAdapter(
        settings(),
        transport=FakeTransport([response]),
    )

    with pytest.raises(ModelResponseError):
        adapter.complete(request(Message(role=MessageRole.USER, content="Hi")))
