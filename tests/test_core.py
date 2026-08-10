"""Tests for provider-independent model types."""

from __future__ import annotations

import pytest

from minicode_rebuild.core import (
    Message,
    MessageRole,
    ModelRequest,
    ModelTool,
    ToolCall,
)


def test_assistant_message_can_carry_tool_calls() -> None:
    tool_call = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "README.md"},
    )

    message = Message(
        role=MessageRole.ASSISTANT,
        tool_calls=(tool_call,),
    )

    assert message.tool_calls == (tool_call,)


def test_tool_message_requires_tool_call_id() -> None:
    with pytest.raises(ValueError, match="tool_call_id"):
        Message(role=MessageRole.TOOL, content="result")


def test_non_tool_message_rejects_tool_call_id() -> None:
    with pytest.raises(ValueError, match="only valid"):
        Message(
            role=MessageRole.USER,
            content="hello",
            tool_call_id="call-1",
        )


def test_non_assistant_message_rejects_tool_calls() -> None:
    with pytest.raises(ValueError, match="assistant"):
        Message(
            role=MessageRole.USER,
            tool_calls=(ToolCall(id="call-1", name="read_file"),),
        )


@pytest.mark.parametrize("name", ["contains space", "", "x" * 65])
def test_model_tool_rejects_invalid_function_name(name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        ModelTool(name=name, description="Invalid")


def test_model_request_requires_at_least_one_message() -> None:
    with pytest.raises(ValueError, match="message"):
        ModelRequest(messages=())
