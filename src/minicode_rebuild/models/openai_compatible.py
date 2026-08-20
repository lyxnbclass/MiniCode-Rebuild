"""Non-streaming adapter for OpenAI-compatible Chat Completions APIs."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from minicode_rebuild.config import ModelSettings
from minicode_rebuild.core import (
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelTool,
    TokenUsage,
    ToolCall,
)
from minicode_rebuild.models.errors import (
    ModelHTTPError,
    ModelResponseError,
    ModelTransportError,
)

DEFAULT_USER_AGENT = "MiniCode-Rebuild/0.1.0"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Minimal HTTP response consumed by the adapter."""

    status: int
    body: bytes


class HttpTransport(Protocol):
    """Injectable JSON transport used by real and fake HTTP clients."""

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: int,
    ) -> HttpResponse:
        """POST a JSON object and return buffered status and body."""
        ...


class UrllibHttpTransport:
    """Standard-library HTTP transport for production requests."""

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: int,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=getattr(response, "status", 200),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(status=error.code, body=error.read())
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ModelTransportError(
                f"OpenAI-compatible request failed: {error}"
            ) from error


def _serialize_tool_call(tool_call: ToolCall) -> dict[str, Any]:
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": json.dumps(
                dict(tool_call.arguments),
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    }


def _serialize_message(message: Message) -> dict[str, Any]:
    if message.role is MessageRole.TOOL:
        return {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id,
        }

    serialized: dict[str, Any] = {
        "role": message.role.value,
        "content": message.content,
    }
    if message.tool_calls:
        serialized["content"] = message.content or None
        serialized["tool_calls"] = [
            _serialize_tool_call(tool_call)
            for tool_call in message.tool_calls
        ]
    return serialized


def _serialize_tool(tool: ModelTool) -> dict[str, Any]:
    function: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.parameters),
    }
    if tool.strict is not None:
        function["strict"] = tool.strict
    return {"type": "function", "function": function}


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelResponseError(
            "OpenAI-compatible endpoint returned invalid JSON"
        ) from error


def _error_message(body: bytes, status: int) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = body.decode("utf-8", errors="replace").strip()
        return text or f"HTTP {status}"

    if isinstance(payload, Mapping):
        error_block = payload.get("error")
        if isinstance(error_block, Mapping):
            message = error_block.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return f"HTTP {status}"


def _token_count(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelResponseError(
            f"OpenAI-compatible response has invalid {field_name}"
        )
    return value


def _parse_usage(payload: Mapping[str, Any]) -> TokenUsage:
    usage = payload.get("usage")
    if usage is None:
        return TokenUsage()
    if not isinstance(usage, Mapping):
        raise ModelResponseError("OpenAI-compatible response has invalid usage")

    input_tokens = _token_count(usage.get("prompt_tokens", 0), "prompt_tokens")
    output_tokens = _token_count(
        usage.get("completion_tokens", 0),
        "completion_tokens",
    )
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _parse_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise ModelResponseError(
                "model returned invalid JSON tool arguments"
            ) from error
    elif isinstance(raw_arguments, Mapping):
        parsed = dict(raw_arguments)
    else:
        raise ModelResponseError("model returned invalid tool arguments")

    if not isinstance(parsed, dict):
        raise ModelResponseError("model tool arguments must be a JSON object")
    return parsed


def _parse_tool_calls(message: Mapping[str, Any]) -> tuple[ToolCall, ...]:
    raw_calls = message.get("tool_calls", [])
    if raw_calls is None:
        return ()
    if not isinstance(raw_calls, list):
        raise ModelResponseError("model tool_calls must be an array")

    calls: list[ToolCall] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, Mapping):
            raise ModelResponseError("model tool call must be an object")
        call_type = raw_call.get("type")
        if call_type not in (None, "function"):
            raise ModelResponseError(
                f"unsupported model tool call type: {call_type}"
            )
        function = raw_call.get("function")
        if not isinstance(function, Mapping):
            raise ModelResponseError("model function call is missing function data")
        try:
            calls.append(
                ToolCall(
                    id=str(raw_call.get("id", "")),
                    name=str(function.get("name", "")),
                    arguments=_parse_arguments(
                        function.get("arguments", "{}")
                    ),
                )
            )
        except ValueError as error:
            raise ModelResponseError(str(error)) from error
    return tuple(calls)


def _parse_response(payload: Any) -> ModelResponse:
    if not isinstance(payload, Mapping):
        raise ModelResponseError("OpenAI-compatible response must be an object")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelResponseError(
            "OpenAI-compatible response must contain at least one choice"
        )
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise ModelResponseError("OpenAI-compatible choice must be an object")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ModelResponseError(
            "OpenAI-compatible choice is missing an assistant message"
        )

    content = message.get("content")
    if content is None:
        normalized_content = ""
    elif isinstance(content, str):
        normalized_content = content
    else:
        raise ModelResponseError("model response content must be text or null")

    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ModelResponseError("model finish_reason must be text or null")

    return ModelResponse(
        content=normalized_content,
        tool_calls=_parse_tool_calls(message),
        finish_reason=finish_reason,
        usage=_parse_usage(payload),
    )


class OpenAICompatibleAdapter:
    """Translate normalized requests to and from Chat Completions JSON."""

    def __init__(
        self,
        settings: ModelSettings,
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport or UrllibHttpTransport()

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Send one non-streaming request and normalize its first choice."""
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [
                _serialize_message(message)
                for message in request.messages
            ],
        }
        if request.tools:
            payload["tools"] = [
                _serialize_tool(tool)
                for tool in request.tools
            ]
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens

        response = self._transport.post_json(
            self._settings.chat_completions_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._settings.api_key}",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            payload=payload,
            timeout=self._settings.timeout_seconds,
        )
        if response.status < 200 or response.status >= 300:
            message = _error_message(response.body, response.status)
            raise ModelHTTPError(response.status, message)
        return _parse_response(_decode_json(response.body))
