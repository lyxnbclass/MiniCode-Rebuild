"""Terminal-facing assembly around the provider-independent agent loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from minicode_rebuild.agent import AgentResult, run_agent_turn
from minicode_rebuild.config import RuntimeSettings
from minicode_rebuild.core import Message, MessageRole, ModelAdapter, ToolCall
from minicode_rebuild.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionRequest,
)
from minicode_rebuild.tooling import ToolContext, ToolRegistry, ToolResult
from minicode_rebuild.tools import MUTATING_TOOLS, READ_ONLY_TOOLS


@dataclass(frozen=True, slots=True)
class SessionStats:
    """Small cumulative counters that do not persist beyond this process."""

    turns: int = 0
    model_steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def format_stats(stats: SessionStats) -> str:
    """Render stable, script-friendly session counters."""

    total_tokens = stats.input_tokens + stats.output_tokens
    return (
        f"turns={stats.turns} steps={stats.model_steps} "
        f"tools={stats.tool_calls} tokens={total_tokens} "
        f"(input={stats.input_tokens} output={stats.output_tokens})"
    )


def make_permission_prompt(
    input_stream: TextIO,
    output_stream: TextIO,
) -> Callable[[PermissionRequest], PermissionDecision]:
    """Create a conservative line-oriented permission prompt."""

    def prompt(request: PermissionRequest) -> PermissionDecision:
        output_stream.write(
            f"\nPermission required: {request.summary} (risk={request.risk.value})\n"
        )
        for detail in request.details:
            output_stream.write(f"  - {detail}\n")
        output_stream.write("Allow? [y] once, [s] session, [n] deny: ")
        output_stream.flush()
        answer = input_stream.readline().strip().casefold()
        if answer in {"y", "yes"}:
            return PermissionDecision.ALLOW_ONCE
        if answer in {"s", "session"}:
            return PermissionDecision.ALLOW_SESSION
        return PermissionDecision.DENY

    return prompt


def create_tool_registry() -> ToolRegistry:
    """Return the complete built-in registry in a stable order."""

    return ToolRegistry((*READ_ONLY_TOOLS, *MUTATING_TOOLS))


class AgentSession:
    """Keep in-memory history and counters across terminal turns."""

    def __init__(
        self,
        *,
        model: ModelAdapter,
        tools: ToolRegistry,
        context: ToolContext,
        settings: RuntimeSettings,
        output: TextIO,
    ) -> None:
        self.model = model
        self.tools = tools
        self.context = context
        self.settings = settings
        self.output = output
        self.history: tuple[Message, ...] = ()
        self.stats = SessionStats()

    def _observe_tool(self, call: ToolCall, result: ToolResult) -> None:
        status = "ok" if result.ok else f"error ({result.error_code})"
        self.output.write(f"[tool] {call.name} -> {status}\n")
        self.output.flush()

    def run(self, user_message: str) -> AgentResult:
        """Run one turn, retain normalized history, and update counters."""

        result = run_agent_turn(
            model=self.model,
            tools=self.tools,
            context=self.context,
            user_message=user_message,
            history=self.history,
            system_prompt=self.settings.system_prompt,
            max_steps=self.settings.max_steps,
            tool_observer=self._observe_tool,
        )
        self.history = tuple(
            message
            for message in result.messages
            if message.role is not MessageRole.SYSTEM
        )
        self.stats = SessionStats(
            turns=self.stats.turns + 1,
            model_steps=self.stats.model_steps + result.steps,
            tool_calls=self.stats.tool_calls + result.tool_calls,
            input_tokens=self.stats.input_tokens + result.usage.input_tokens,
            output_tokens=self.stats.output_tokens + result.usage.output_tokens,
        )
        return result


def build_session(
    *,
    model: ModelAdapter,
    workspace: Path,
    settings: RuntimeSettings,
    output: TextIO,
    permission_prompt: Callable[[PermissionRequest], PermissionDecision] | None,
) -> AgentSession:
    """Assemble the default registry, permission boundary, and session."""

    permissions = PermissionManager(prompt=permission_prompt)
    return AgentSession(
        model=model,
        tools=create_tool_registry(),
        context=ToolContext(workspace, permissions=permissions),
        settings=settings,
        output=output,
    )


__all__ = [
    "AgentSession",
    "SessionStats",
    "build_session",
    "create_tool_registry",
    "format_stats",
    "make_permission_prompt",
]
