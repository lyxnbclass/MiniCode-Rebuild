"""Terminal-facing assembly around the provider-independent agent loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from minicode_rebuild.agent import AgentResult, run_agent_turn
from minicode_rebuild.config import RuntimeSettings
from minicode_rebuild.context import CompactionResult, ContextManager
from minicode_rebuild.core import Message, MessageRole, ModelAdapter, ToolCall
from minicode_rebuild.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionRequest,
)
from minicode_rebuild.session import (
    RewindPlan,
    SessionRecord,
    SessionStatsData,
    SessionStore,
    format_transcript,
)
from minicode_rebuild.tooling import ToolContext, ToolRegistry, ToolResult
from minicode_rebuild.tools import MUTATING_TOOLS, READ_ONLY_TOOLS


@dataclass(frozen=True, slots=True)
class SessionStats:
    """Small cumulative counters that can be persisted with a session."""

    turns: int = 0
    model_steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    compactions: int = 0


def format_stats(stats: SessionStats) -> str:
    """Render stable, script-friendly session counters."""

    total_tokens = stats.input_tokens + stats.output_tokens
    return (
        f"turns={stats.turns} steps={stats.model_steps} "
        f"tools={stats.tool_calls} tokens={total_tokens} "
        f"(input={stats.input_tokens} output={stats.output_tokens}) "
        f"compactions={stats.compactions}"
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
    """Keep history and counters, optionally persisted across processes."""

    def __init__(
        self,
        *,
        model: ModelAdapter,
        tools: ToolRegistry,
        context: ToolContext,
        settings: RuntimeSettings,
        output: TextIO,
        context_manager: ContextManager | None = None,
        session_store: SessionStore | None = None,
        session_record: SessionRecord | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.context = context
        self.settings = settings
        self.output = output
        self.context_manager = context_manager or ContextManager(
            settings.context_policy
        )
        self.session_store = session_store
        self.session_record = session_record
        self.history = session_record.messages if session_record is not None else ()
        self.stats = (
            SessionStats(
                turns=session_record.stats.turns,
                model_steps=session_record.stats.model_steps,
                tool_calls=session_record.stats.tool_calls,
                input_tokens=session_record.stats.input_tokens,
                output_tokens=session_record.stats.output_tokens,
                compactions=session_record.stats.compactions,
            )
            if session_record is not None
            else SessionStats()
        )
        self._request_compactions = 0
        if session_store is not None and session_record is not None:
            self.context.state["checkpoint_recorder"] = self._record_checkpoint
            self.context.state["checkpoint_discarder"] = self._discard_checkpoint

    @property
    def session_id(self) -> str | None:
        return None if self.session_record is None else self.session_record.session_id

    def _record_checkpoint(
        self,
        target: Path,
        previous_content: str | None,
        next_content: str,
        operation: str,
    ) -> str:
        assert self.session_store is not None and self.session_record is not None
        return self.session_store.record_checkpoint(
            self.session_record,
            target,
            previous_content,
            next_content,
            operation=operation,
        ).checkpoint_id

    def _discard_checkpoint(self, checkpoint_id: str) -> None:
        assert self.session_store is not None and self.session_record is not None
        self.session_store.discard_checkpoint(self.session_record, checkpoint_id)

    @staticmethod
    def _current_turn(messages: tuple[Message, ...], user_message: str) -> tuple[Message, ...]:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.role is MessageRole.USER and message.content == user_message:
                return messages[index:]
        return ()

    def _persist(self, *, new_transcript: tuple[Message, ...] = ()) -> None:
        if self.session_store is None or self.session_record is None:
            return
        self.session_record.messages = self.history
        if new_transcript:
            self.session_record.transcript += new_transcript
        self.session_record.stats = SessionStatsData(
            turns=self.stats.turns,
            model_steps=self.stats.model_steps,
            tool_calls=self.stats.tool_calls,
            input_tokens=self.stats.input_tokens,
            output_tokens=self.stats.output_tokens,
            compactions=self.stats.compactions,
        )
        self.session_store.save(self.session_record)

    def _observe_tool(self, call: ToolCall, result: ToolResult) -> None:
        status = "ok" if result.ok else f"error ({result.error_code})"
        self.output.write(f"[tool] {call.name} -> {status}\n")
        self.output.flush()

    def run(self, user_message: str) -> AgentResult:
        """Run one turn, retain normalized history, and update counters."""

        self._request_compactions = 0
        result = run_agent_turn(
            model=self.model,
            tools=self.tools,
            context=self.context,
            user_message=user_message,
            history=self.history,
            system_prompt=self.settings.system_prompt,
            max_steps=self.settings.max_steps,
            tool_observer=self._observe_tool,
            message_preparer=self._prepare_messages,
        )
        raw_history = tuple(
            message
            for message in result.messages
            if message.role is not MessageRole.SYSTEM
            or message.content.startswith("[Context summary]")
        )
        compaction = self.context_manager.compact(raw_history, force=False)
        self.history = compaction.messages
        self.stats = SessionStats(
            turns=self.stats.turns + 1,
            model_steps=self.stats.model_steps + result.steps,
            tool_calls=self.stats.tool_calls + result.tool_calls,
            input_tokens=self.stats.input_tokens + result.usage.input_tokens,
            output_tokens=self.stats.output_tokens + result.usage.output_tokens,
            compactions=(
                self.stats.compactions
                + self._request_compactions
                + int(compaction.compacted)
            ),
        )
        self._persist(new_transcript=self._current_turn(result.messages, user_message))
        return result

    def _prepare_messages(
        self, messages: tuple[Message, ...]
    ) -> tuple[Message, ...]:
        result = self.context_manager.compact(messages, force=False)
        self._request_compactions += int(result.compacted)
        return result.messages

    def compact_history(self) -> CompactionResult:
        """Force a manual in-memory history compaction."""

        result = self.context_manager.compact(self.history, force=True)
        self.history = result.messages
        if result.compacted:
            self.stats = SessionStats(
                turns=self.stats.turns,
                model_steps=self.stats.model_steps,
                tool_calls=self.stats.tool_calls,
                input_tokens=self.stats.input_tokens,
                output_tokens=self.stats.output_tokens,
                compactions=self.stats.compactions + 1,
            )
        self._persist()
        return result

    def transcript(self) -> str:
        messages = (
            self.session_record.transcript
            if self.session_record is not None
            else self.history
        )
        return format_transcript(messages)

    def list_sessions(self) -> list[SessionRecord]:
        return [] if self.session_store is None else self.session_store.list()

    def preview_rewind(self, checkpoint_id: str | None = None) -> RewindPlan:
        if self.session_store is None or self.session_record is None:
            raise RuntimeError("Session persistence is disabled")
        return self.session_store.preview_rewind(self.session_record, checkpoint_id)

    def apply_rewind(self, checkpoint_id: str | None = None, *, confirmed: bool) -> RewindPlan:
        if self.session_store is None or self.session_record is None:
            raise RuntimeError("Session persistence is disabled")
        return self.session_store.apply_rewind(
            self.session_record, checkpoint_id, confirmed=confirmed
        )


def build_session(
    *,
    model: ModelAdapter,
    workspace: Path,
    settings: RuntimeSettings,
    output: TextIO,
    permission_prompt: Callable[[PermissionRequest], PermissionDecision] | None,
    resume: str | None = None,
) -> AgentSession:
    """Assemble the default registry, permission boundary, and session."""

    permissions = PermissionManager(prompt=permission_prompt)
    store = SessionStore(workspace)
    record = store.load(resume) if resume is not None else store.create()
    return AgentSession(
        model=model,
        tools=create_tool_registry(),
        context=ToolContext(workspace, permissions=permissions),
        settings=settings,
        output=output,
        context_manager=ContextManager(settings.context_policy),
        session_store=store,
        session_record=record,
    )


__all__ = [
    "AgentSession",
    "SessionStats",
    "build_session",
    "create_tool_registry",
    "format_stats",
    "make_permission_prompt",
]
