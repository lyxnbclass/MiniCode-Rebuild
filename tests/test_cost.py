from __future__ import annotations

import pytest

from minicode_rebuild.core import Message, MessageRole, ModelRequest, ModelTool
from minicode_rebuild.cost import (
    TokenBudgetPolicy,
    estimate_request_tokens,
    evaluate_budget,
    format_budget,
)


def request() -> ModelRequest:
    return ModelRequest(
        messages=(Message(role=MessageRole.USER, content="inspect README"),),
        tools=(
            ModelTool(
                name="read_file",
                description="Read one file.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            ),
        ),
    )


def test_request_estimate_includes_tool_protocol() -> None:
    without_tools = ModelRequest(messages=request().messages)

    assert estimate_request_tokens(request()) > estimate_request_tokens(without_tools)


def test_budget_caps_output_to_remaining_session_allowance() -> None:
    model_request = request()
    estimate = estimate_request_tokens(model_request)
    policy = TokenBudgetPolicy(
        session_tokens=estimate + 25,
        max_output_tokens=100,
    )

    decision = evaluate_budget(model_request, policy, used_tokens=5)

    assert decision.allowed is True
    assert decision.remaining_tokens == estimate + 20
    assert decision.max_output_tokens == 20


def test_budget_fails_closed_before_an_oversized_request() -> None:
    model_request = request()
    estimate = estimate_request_tokens(model_request)

    decision = evaluate_budget(
        model_request,
        TokenBudgetPolicy(session_tokens=estimate),
        used_tokens=0,
    )

    assert decision.allowed is False
    assert decision.max_output_tokens is None
    assert "estimated input" in (decision.reason or "")


@pytest.mark.parametrize("value", [0, -1, True, "10"])
def test_policy_rejects_invalid_limits(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        TokenBudgetPolicy(session_tokens=value)  # type: ignore[arg-type]


def test_budget_status_distinguishes_disabled_and_bounded_sessions() -> None:
    assert format_budget(TokenBudgetPolicy(), used_tokens=3) == (
        "token-budget=disabled used=3 max-output=unbounded"
    )
    assert format_budget(
        TokenBudgetPolicy(session_tokens=10, max_output_tokens=4),
        used_tokens=7,
    ) == "token-budget=10 used=7 remaining=3 max-output=4"
