"""Provider-independent types used at the model boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, TypeAlias, runtime_checkable

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

_FUNCTION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class MessageRole(str, Enum):
    """Roles supported by the provider-independent conversation format."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A normalized function call produced by a model."""

    id: str
    name: str
    arguments: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        call_id = self.id.strip()
        name = self.name.strip()
        if not call_id:
            raise ValueError("tool call id must not be empty")
        if not _FUNCTION_NAME.fullmatch(name):
            raise ValueError("tool call name must be 1-64 valid function-name characters")
        object.__setattr__(self, "id", call_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "arguments", dict(self.arguments))


@dataclass(frozen=True, slots=True)
class Message:
    """One normalized conversation message."""

    role: MessageRole
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if self.role is MessageRole.TOOL:
            if not self.tool_call_id or not self.tool_call_id.strip():
                raise ValueError("tool messages require a non-empty tool_call_id")
            object.__setattr__(self, "tool_call_id", self.tool_call_id.strip())
        elif self.tool_call_id is not None:
            raise ValueError("tool_call_id is only valid for tool messages")

        if self.tool_calls and self.role is not MessageRole.ASSISTANT:
            raise ValueError("tool_calls are only valid for assistant messages")


def _empty_object_schema() -> dict[str, JsonValue]:
    return {"type": "object", "properties": {}}


@dataclass(frozen=True, slots=True)
class ModelTool:
    """A function schema visible to the model, not an executable tool."""

    name: str
    description: str = ""
    parameters: Mapping[str, JsonValue] = field(default_factory=_empty_object_schema)
    strict: bool | None = None

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not _FUNCTION_NAME.fullmatch(name):
            raise ValueError("model tool name must be 1-64 valid function-name characters")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "parameters", dict(self.parameters))


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Normalized token counts returned by a provider."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token usage must not be negative")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """A complete provider-independent model request."""

    messages: tuple[Message, ...]
    tools: tuple[ModelTool, ...] = ()
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        messages = tuple(self.messages)
        if not messages:
            raise ValueError("a model request requires at least one message")
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "tools", tuple(self.tools))
        if self.max_output_tokens is not None:
            if (
                isinstance(self.max_output_tokens, bool)
                or not isinstance(self.max_output_tokens, int)
                or self.max_output_tokens < 1
            ):
                raise ValueError("max_output_tokens must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A normalized model response."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))


@runtime_checkable
class ModelAdapter(Protocol):
    """Protocol implemented by every model provider and test double."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one normalized response for one normalized request."""
        ...
