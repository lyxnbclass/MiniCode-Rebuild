from __future__ import annotations

import json
from pathlib import Path

import pytest

from minicode_rebuild.agent import AgentStopReason, run_agent_turn
from minicode_rebuild.context import ContextManager, ContextPolicy
from minicode_rebuild.core import (
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
)
from minicode_rebuild.cost import TokenBudgetPolicy, estimate_request_tokens
from minicode_rebuild.models import MockModel
from minicode_rebuild.tooling import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)


def echo_tool(result: ToolResult | None = None) -> ToolDefinition:
    def handler(arguments, _context: ToolContext) -> ToolResult:
        return result or ToolResult.success(str(arguments["text"]))

    return ToolDefinition(
        name="echo",
        description="Echo text.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def run(
    tmp_path: Path,
    model: MockModel,
    registry: ToolRegistry | None = None,
    **kwargs,
):
    return run_agent_turn(
        model=model,
        tools=registry or ToolRegistry(),
        context=ToolContext(tmp_path),
        user_message="Help me",
        **kwargs,
    )


def test_final_text_stops_and_exposes_model_request(tmp_path: Path) -> None:
    model = MockModel(
        [ModelResponse(content="Done", usage=TokenUsage(4, 2))]
    )
    registry = ToolRegistry([echo_tool()])

    result = run(tmp_path, model, registry)

    assert result.stop_reason is AgentStopReason.FINAL_RESPONSE
    assert result.content == "Done"
    assert result.steps == 1
    assert result.tool_calls == 0
    assert result.usage == TokenUsage(4, 2)
    assert result.completed is True
    assert result.messages[-1] == Message(
        role=MessageRole.ASSISTANT, content="Done"
    )
    assert model.requests[0].messages == (
        Message(role=MessageRole.USER, content="Help me"),
    )
    assert [tool.name for tool in model.requests[0].tools] == ["echo"]


def test_tool_result_is_fed_back_before_final_response(tmp_path: Path) -> None:
    call = ToolCall(id="call-1", name="echo", arguments={"text": "hello"})
    model = MockModel(
        [
            ModelResponse(tool_calls=(call,), finish_reason="tool_calls"),
            ModelResponse(content="I saw hello", finish_reason="stop"),
        ]
    )

    result = run(tmp_path, model, ToolRegistry([echo_tool()]))

    assert result.stop_reason is AgentStopReason.FINAL_RESPONSE
    assert result.steps == 2
    assert result.tool_calls == 1
    second_messages = model.requests[1].messages
    assert second_messages[-2] == Message(
        role=MessageRole.ASSISTANT, tool_calls=(call,)
    )
    tool_message = second_messages[-1]
    assert tool_message.role is MessageRole.TOOL
    assert tool_message.tool_call_id == "call-1"
    assert json.loads(tool_message.content) == {
        "error_code": None,
        "ok": True,
        "original_length": None,
        "output": "hello",
        "truncated": False,
    }


@pytest.mark.parametrize(
    ("call", "expected_code"),
    [
        (ToolCall(id="unknown", name="missing"), "unknown_tool"),
        (
            ToolCall(id="invalid", name="echo", arguments={"extra": True}),
            "invalid_arguments",
        ),
    ],
)
def test_unknown_tool_and_invalid_arguments_return_to_model(
    tmp_path: Path, call: ToolCall, expected_code: str
) -> None:
    model = MockModel(
        [ModelResponse(tool_calls=(call,)), ModelResponse(content="Recovered")]
    )

    result = run(tmp_path, model, ToolRegistry([echo_tool()]))

    payload = json.loads(model.requests[1].messages[-1].content)
    assert payload["ok"] is False
    assert payload["error_code"] == expected_code
    assert result.content == "Recovered"


def test_failed_tool_result_does_not_abort_loop(tmp_path: Path) -> None:
    call = ToolCall(id="failed", name="echo", arguments={"text": "x"})
    model = MockModel(
        [ModelResponse(tool_calls=(call,)), ModelResponse(content="Adjusted")]
    )
    registry = ToolRegistry(
        [echo_tool(ToolResult.error("demo_failure", "could not echo"))]
    )

    result = run(tmp_path, model, registry)

    payload = json.loads(model.requests[1].messages[-1].content)
    assert payload["error_code"] == "demo_failure"
    assert payload["output"] == "could not echo"
    assert result.stop_reason is AgentStopReason.FINAL_RESPONSE


def test_multiple_tool_calls_execute_in_model_order(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(arguments, _context):
        seen.append(str(arguments["text"]))
        return ToolResult.success(str(arguments["text"]))

    tool = echo_tool()
    object.__setattr__(tool, "handler", handler)
    calls = (
        ToolCall(id="one", name="echo", arguments={"text": "first"}),
        ToolCall(id="two", name="echo", arguments={"text": "second"}),
    )
    model = MockModel(
        [ModelResponse(content="Working", tool_calls=calls), ModelResponse(content="Done")]
    )

    result = run(tmp_path, model, ToolRegistry([tool]))

    assert seen == ["first", "second"]
    assert result.tool_calls == 2
    assert model.requests[1].messages[-3].content == "Working"
    assert [message.tool_call_id for message in model.requests[1].messages[-2:]] == [
        "one",
        "two",
    ]


def test_tool_observer_receives_each_call_and_result(tmp_path: Path) -> None:
    call = ToolCall(id="call-1", name="echo", arguments={"text": "hello"})
    model = MockModel(
        [ModelResponse(tool_calls=(call,)), ModelResponse(content="Done")]
    )
    seen = []

    result = run_agent_turn(
        model=model,
        tools=ToolRegistry([echo_tool()]),
        context=ToolContext(tmp_path),
        user_message="Help me",
        tool_observer=lambda observed_call, observed_result: seen.append(
            (observed_call, observed_result)
        ),
    )

    assert result.completed is True
    assert seen == [(call, ToolResult.success("hello"))]


def test_message_preparer_compacts_current_turn_before_next_model_request(
    tmp_path: Path,
) -> None:
    call = ToolCall(id="call-1", name="echo", arguments={"text": "hello"})
    model = MockModel(
        [ModelResponse(tool_calls=(call,)), ModelResponse(content="Done")]
    )
    prepared_sizes: list[int] = []

    def prepare(messages: tuple[Message, ...]) -> tuple[Message, ...]:
        prepared_sizes.append(len(messages))
        if messages and messages[-1].role is MessageRole.TOOL:
            return (
                *messages[:-1],
                Message(
                    role=MessageRole.TOOL,
                    content='{"compacted":true}',
                    tool_call_id=messages[-1].tool_call_id,
                ),
            )
        return messages

    result = run_agent_turn(
        model=model,
        tools=ToolRegistry([echo_tool()]),
        context=ToolContext(tmp_path),
        user_message="Help me",
        message_preparer=prepare,
    )

    assert result.completed is True
    assert prepared_sizes == [1, 3]
    assert model.requests[1].messages[-1].content == '{"compacted":true}'


def test_context_manager_trims_current_tool_result_before_next_request(
    tmp_path: Path,
) -> None:
    long_result = "HEAD-" + "x" * 3000 + "-TAIL"
    call = ToolCall(id="call-1", name="echo", arguments={"text": "ignored"})
    model = MockModel(
        [ModelResponse(tool_calls=(call,)), ModelResponse(content="Done")]
    )
    manager = ContextManager(
        ContextPolicy(max_tokens=10_000, tool_result_tokens=60)
    )

    result = run_agent_turn(
        model=model,
        tools=ToolRegistry([echo_tool(ToolResult.success(long_result))]),
        context=ToolContext(tmp_path),
        user_message="Help me",
        message_preparer=lambda messages: manager.compact(messages).messages,
    )

    assert result.completed is True
    tool_payload = json.loads(model.requests[1].messages[-1].content)
    assert tool_payload["truncated"] is True
    assert "HEAD-" in tool_payload["output"]
    assert "-TAIL" in tool_payload["output"]
    assert len(tool_payload["output"]) < len(long_result)


def test_empty_response_stops_explicitly(tmp_path: Path) -> None:
    result = run(tmp_path, MockModel([ModelResponse(content="  ")]))

    assert result.stop_reason is AgentStopReason.EMPTY_RESPONSE
    assert result.completed is False
    assert "empty response" in result.content.lower()
    assert result.steps == 1


def test_model_exception_is_normalized_without_losing_history(tmp_path: Path) -> None:
    result = run(tmp_path, MockModel([RuntimeError("provider unavailable")]))

    assert result.stop_reason is AgentStopReason.MODEL_ERROR
    assert result.error == "RuntimeError: provider unavailable"
    assert "provider unavailable" in result.content
    assert result.messages == (
        Message(role=MessageRole.USER, content="Help me"),
    )


def test_invalid_model_result_is_normalized(tmp_path: Path) -> None:
    class InvalidModel:
        def complete(self, _request):
            return "not a model response"

    result = run(tmp_path, InvalidModel())  # type: ignore[arg-type]

    assert result.stop_reason is AgentStopReason.MODEL_ERROR
    assert "must return a ModelResponse" in result.content


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(2)])
def test_control_flow_exceptions_from_model_propagate(
    tmp_path: Path, failure: BaseException
) -> None:
    class InterruptingModel:
        def complete(self, _request):
            raise failure

    with pytest.raises(type(failure)):
        run(tmp_path, InterruptingModel())  # type: ignore[arg-type]


def test_max_steps_prevents_infinite_tool_loop(tmp_path: Path) -> None:
    calls = [
        ModelResponse(
            tool_calls=(
                ToolCall(
                    id=f"call-{index}",
                    name="echo",
                    arguments={"text": str(index)},
                ),
            ),
            usage=TokenUsage(1, 1),
        )
        for index in range(3)
    ]
    model = MockModel(calls)

    result = run(
        tmp_path, model, ToolRegistry([echo_tool()]), max_steps=2
    )

    assert result.stop_reason is AgentStopReason.MAX_STEPS
    assert result.steps == 2
    assert result.tool_calls == 2
    assert result.usage == TokenUsage(2, 2)
    assert len(model.requests) == 2
    assert "2" in result.content


def test_budget_sets_provider_output_limit(tmp_path: Path) -> None:
    model = MockModel(
        [ModelResponse(content="Done", usage=TokenUsage(10, 2))]
    )

    result = run(
        tmp_path,
        model,
        token_budget=TokenBudgetPolicy(
            session_tokens=10_000,
            max_output_tokens=77,
        ),
    )

    assert result.completed is True
    assert model.requests[0].max_output_tokens == 77


def test_budget_blocks_without_calling_provider(tmp_path: Path) -> None:
    model = MockModel([ModelResponse(content="must not run")])

    result = run(
        tmp_path,
        model,
        token_budget=TokenBudgetPolicy(session_tokens=1),
    )

    assert result.stop_reason is AgentStopReason.BUDGET_EXHAUSTED
    assert result.steps == 0
    assert result.usage == TokenUsage()
    assert model.requests == ()


def test_budget_uses_reported_usage_before_next_tool_step(tmp_path: Path) -> None:
    call = ToolCall(id="call-1", name="echo", arguments={"text": "hello"})
    model = MockModel(
        [
            ModelResponse(tool_calls=(call,), usage=TokenUsage(9_000, 500)),
            ModelResponse(content="must not run"),
        ]
    )
    registry = ToolRegistry([echo_tool()])
    initial = ModelRequest(
        messages=(Message(role=MessageRole.USER, content="Help me"),),
        tools=registry.model_tools(),
    )
    policy = TokenBudgetPolicy(
        session_tokens=estimate_request_tokens(initial) + 9_500
    )

    result = run(
        tmp_path,
        model,
        registry,
        token_budget=policy,
    )

    assert result.stop_reason is AgentStopReason.BUDGET_EXHAUSTED
    assert result.steps == 1
    assert len(model.requests) == 1


def test_system_prompt_and_history_precede_new_user_message(tmp_path: Path) -> None:
    history = (
        Message(role=MessageRole.USER, content="Earlier"),
        Message(role=MessageRole.ASSISTANT, content="Understood"),
    )
    model = MockModel([ModelResponse(content="Done")])

    run(tmp_path, model, history=history, system_prompt="Be precise")

    assert model.requests[0].messages == (
        Message(role=MessageRole.SYSTEM, content="Be precise"),
        *history,
        Message(role=MessageRole.USER, content="Help me"),
    )


@pytest.mark.parametrize("max_steps", [0, -1, True])
def test_max_steps_must_be_a_positive_integer(
    tmp_path: Path, max_steps: int
) -> None:
    with pytest.raises((TypeError, ValueError), match="max_steps"):
        run(tmp_path, MockModel([]), max_steps=max_steps)


def test_user_message_must_not_be_empty(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="user_message"):
        run_agent_turn(
            model=MockModel([]),
            tools=ToolRegistry(),
            context=ToolContext(tmp_path),
            user_message="  ",
        )
