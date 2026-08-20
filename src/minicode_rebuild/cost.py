"""Provider-independent token budget controls for model requests."""

from __future__ import annotations

import json
from dataclasses import dataclass

from minicode_rebuild.context import estimate_messages_tokens, estimate_text_tokens
from minicode_rebuild.core import ModelRequest


@dataclass(frozen=True, slots=True)
class TokenBudgetPolicy:
    """Optional session and per-response token limits."""

    session_tokens: int | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("session_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer or None")
            if value < 1:
                raise ValueError(f"{name} must be greater than zero")

    @property
    def enabled(self) -> bool:
        return self.session_tokens is not None or self.max_output_tokens is not None


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """One deterministic request admission decision."""

    allowed: bool
    estimated_input_tokens: int
    remaining_tokens: int | None
    max_output_tokens: int | None
    reason: str | None = None


def estimate_request_tokens(request: ModelRequest) -> int:
    """Estimate messages and tool declarations without a provider tokenizer."""

    total = estimate_messages_tokens(request.messages)
    for tool in request.tools:
        schema = json.dumps(
            dict(tool.parameters),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        total += 8
        total += estimate_text_tokens(tool.name)
        total += estimate_text_tokens(tool.description)
        total += estimate_text_tokens(schema)
    return max(1, total)


def evaluate_budget(
    request: ModelRequest,
    policy: TokenBudgetPolicy,
    *,
    used_tokens: int,
) -> BudgetDecision:
    """Fail closed when the estimated request cannot fit the remaining budget."""

    if isinstance(used_tokens, bool) or not isinstance(used_tokens, int):
        raise TypeError("used_tokens must be an integer")
    if used_tokens < 0:
        raise ValueError("used_tokens must not be negative")

    estimated_input = estimate_request_tokens(request)
    remaining = (
        None
        if policy.session_tokens is None
        else max(0, policy.session_tokens - used_tokens)
    )
    output_limit = policy.max_output_tokens
    if remaining is not None:
        available_output = remaining - estimated_input
        if available_output < 1:
            return BudgetDecision(
                allowed=False,
                estimated_input_tokens=estimated_input,
                remaining_tokens=remaining,
                max_output_tokens=None,
                reason=(
                    "Token budget exhausted before the model request: "
                    f"estimated input={estimated_input}, remaining={remaining}."
                ),
            )
        output_limit = (
            available_output
            if output_limit is None
            else min(output_limit, available_output)
        )

    return BudgetDecision(
        allowed=True,
        estimated_input_tokens=estimated_input,
        remaining_tokens=remaining,
        max_output_tokens=output_limit,
    )


def format_budget(policy: TokenBudgetPolicy, *, used_tokens: int) -> str:
    """Render stable, non-monetary budget status without claiming exact pricing."""

    output = (
        "unbounded"
        if policy.max_output_tokens is None
        else str(policy.max_output_tokens)
    )
    if policy.session_tokens is None:
        return (
            f"token-budget=disabled used={used_tokens} "
            f"max-output={output}"
        )
    remaining = max(0, policy.session_tokens - used_tokens)
    return (
        f"token-budget={policy.session_tokens} used={used_tokens} "
        f"remaining={remaining} max-output={output}"
    )


__all__ = [
    "BudgetDecision",
    "TokenBudgetPolicy",
    "estimate_request_tokens",
    "evaluate_budget",
    "format_budget",
]
