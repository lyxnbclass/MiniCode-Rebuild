"""A bounded provider-independent model and tool execution loop."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from minicode_rebuild.core import (
    Message,
    MessageRole,
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from minicode_rebuild.tooling import ToolContext, ToolRegistry, ToolResult

DEFAULT_MAX_STEPS = 12


class AgentStopReason(str, Enum):
    """Every explicit reason why one agent turn can stop."""

    FINAL_RESPONSE = "final_response"
    EMPTY_RESPONSE = "empty_response"
    MAX_STEPS = "max_steps"
    MODEL_ERROR = "model_error"


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Stable outcome and complete normalized history for one agent turn."""

    content: str
    stop_reason: AgentStopReason
    messages: tuple[Message, ...]
    steps: int
    tool_calls: int
    usage: TokenUsage
    error: str | None = None

    @property
    def completed(self) -> bool:
        """Whether the model produced a non-empty final response."""

        return self.stop_reason is AgentStopReason.FINAL_RESPONSE


def _validate_max_steps(max_steps: int) -> None:
    if isinstance(max_steps, bool) or not isinstance(max_steps, int):
        raise TypeError("max_steps must be an integer")
    if max_steps < 1:
        raise ValueError("max_steps must be greater than zero")


def _initial_messages(
    *,
    user_message: str,
    history: Iterable[Message],
    system_prompt: str,
) -> list[Message]:
    if not isinstance(user_message, str) or not user_message.strip():
        raise ValueError("user_message must not be empty")
    if not isinstance(system_prompt, str):
        raise TypeError("system_prompt must be a string")

    previous = tuple(history)
    if any(not isinstance(message, Message) for message in previous):
        raise TypeError("history must contain only Message instances")

    messages: list[Message] = []
    if system_prompt.strip():
        messages.append(
            Message(role=MessageRole.SYSTEM, content=system_prompt.strip())
        )
    messages.extend(previous)
    messages.append(Message(role=MessageRole.USER, content=user_message))
    return messages


def _add_usage(total: TokenUsage, current: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=total.input_tokens + current.input_tokens,
        output_tokens=total.output_tokens + current.output_tokens,
    )


def _serialize_tool_result(result: ToolResult) -> str:
    return json.dumps(
        {
            "ok": result.ok,
            "output": result.output,
            "error_code": result.error_code,
            "truncated": result.truncated,
            "original_length": result.original_length,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _result(
    *,
    content: str,
    stop_reason: AgentStopReason,
    messages: list[Message],
    steps: int,
    tool_calls: int,
    usage: TokenUsage,
    error: str | None = None,
) -> AgentResult:
    return AgentResult(
        content=content,
        stop_reason=stop_reason,
        messages=tuple(messages),
        steps=steps,
        tool_calls=tool_calls,
        usage=usage,
        error=error,
    )


def run_agent_turn(
    *,
    model: ModelAdapter,
    tools: ToolRegistry,
    context: ToolContext,
    user_message: str,
    history: Iterable[Message] = (),
    system_prompt: str = "",
    max_steps: int = DEFAULT_MAX_STEPS,
) -> AgentResult:
    """Run one bounded turn until final text or an explicit stop condition."""

    _validate_max_steps(max_steps)
    if not isinstance(tools, ToolRegistry):
        raise TypeError("tools must be a ToolRegistry")
    if not isinstance(context, ToolContext):
        raise TypeError("context must be a ToolContext")

    messages = _initial_messages(
        user_message=user_message,
        history=history,
        system_prompt=system_prompt,
    )
    usage = TokenUsage()
    tool_call_count = 0

    for step in range(1, max_steps + 1):
        request = ModelRequest(
            messages=tuple(messages),
            tools=tools.model_tools(),
        )
        try:
            response = model.complete(request)
            if not isinstance(response, ModelResponse):
                raise TypeError(
                    "model adapter must return a ModelResponse, "
                    f"got {type(response).__name__}"
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return _result(
                content=f"Model request failed: {error}",
                stop_reason=AgentStopReason.MODEL_ERROR,
                messages=messages,
                steps=step,
                tool_calls=tool_call_count,
                usage=usage,
                error=error,
            )

        usage = _add_usage(usage, response.usage)
        assistant_message = Message(
            role=MessageRole.ASSISTANT,
            content=response.content,
            tool_calls=response.tool_calls,
        )
        messages.append(assistant_message)

        if not response.tool_calls:
            if response.content.strip():
                return _result(
                    content=response.content,
                    stop_reason=AgentStopReason.FINAL_RESPONSE,
                    messages=messages,
                    steps=step,
                    tool_calls=tool_call_count,
                    usage=usage,
                )
            return _result(
                content="Model returned an empty response; the turn was stopped.",
                stop_reason=AgentStopReason.EMPTY_RESPONSE,
                messages=messages,
                steps=step,
                tool_calls=tool_call_count,
                usage=usage,
            )

        for call in response.tool_calls:
            tool_result = tools.execute(call.name, call.arguments, context)
            tool_call_count += 1
            messages.append(
                Message(
                    role=MessageRole.TOOL,
                    content=_serialize_tool_result(tool_result),
                    tool_call_id=call.id,
                )
            )

    return _result(
        content=f"Reached the maximum step limit ({max_steps}) for this turn.",
        stop_reason=AgentStopReason.MAX_STEPS,
        messages=messages,
        steps=max_steps,
        tool_calls=tool_call_count,
        usage=usage,
    )


__all__ = [
    "AgentResult",
    "AgentStopReason",
    "DEFAULT_MAX_STEPS",
    "run_agent_turn",
]
