from __future__ import annotations

from io import StringIO
from pathlib import Path

from minicode_rebuild.agent import AgentStopReason
from minicode_rebuild.cli_runtime import (
    AgentSession,
    SessionStats,
    format_stats,
    make_permission_prompt,
)
from minicode_rebuild.config import RuntimeSettings
from minicode_rebuild.context import ContextManager, ContextPolicy
from minicode_rebuild.core import ModelResponse, TokenUsage
from minicode_rebuild.models import MockModel
from minicode_rebuild.permissions import (
    PermissionDecision,
    PermissionRequest,
    RiskLevel,
)
from minicode_rebuild.session import SessionStore
from minicode_rebuild.tooling import ToolContext, ToolRegistry


def test_session_accumulates_turn_and_usage_stats(tmp_path: Path) -> None:
    session = AgentSession(
        model=MockModel(
            [
                ModelResponse(content="one", usage=TokenUsage(2, 1)),
                ModelResponse(content="two", usage=TokenUsage(3, 2)),
            ]
        ),
        tools=ToolRegistry(),
        context=ToolContext(tmp_path),
        settings=RuntimeSettings(max_steps=3, system_prompt="Be precise"),
        output=StringIO(),
    )

    first = session.run("first")
    second = session.run("second")

    assert first.stop_reason is AgentStopReason.FINAL_RESPONSE
    assert second.stop_reason is AgentStopReason.FINAL_RESPONSE
    assert session.stats == SessionStats(
        turns=2,
        model_steps=2,
        tool_calls=0,
        input_tokens=5,
        output_tokens=3,
    )
    assert format_stats(session.stats) == (
        "turns=2 steps=2 tools=0 tokens=8 (input=5 output=3) compactions=0"
    )


def test_permission_prompt_explains_request_and_parses_choices() -> None:
    request = PermissionRequest(
        operation="write_file",
        risk=RiskLevel.HIGH,
        summary="Write README.md",
        scope="file:/workspace/README.md",
        details=("overwrite",),
    )

    for answer, expected in [
        ("y\n", PermissionDecision.ALLOW_ONCE),
        ("s\n", PermissionDecision.ALLOW_SESSION),
        ("anything-else\n", PermissionDecision.DENY),
    ]:
        output = StringIO()
        prompt = make_permission_prompt(StringIO(answer), output)

        assert prompt(request) is expected
        assert "Write README.md" in output.getvalue()
        assert "risk=high" in output.getvalue()
        assert "overwrite" in output.getvalue()


def test_session_manual_compaction_updates_history_and_stats(tmp_path: Path) -> None:
    session = AgentSession(
        model=MockModel(
            [
                ModelResponse(content="a" * 120),
                ModelResponse(content="b" * 120),
            ]
        ),
        tools=ToolRegistry(),
        context=ToolContext(tmp_path),
        settings=RuntimeSettings(max_steps=3, system_prompt="Be precise"),
        output=StringIO(),
        context_manager=ContextManager(
            ContextPolicy(max_tokens=1000, keep_recent_turns=1)
        ),
    )
    session.run("first " + "x" * 120)
    session.run("second " + "y" * 120)

    result = session.compact_history()

    assert result.compacted is True
    assert session.history[0].content.startswith("[Context summary]")
    assert session.stats.compactions == 1


def test_session_automatically_compacts_at_threshold(tmp_path: Path) -> None:
    session = AgentSession(
        model=MockModel(
            [
                ModelResponse(content="a" * 200),
                ModelResponse(content="b" * 200),
                ModelResponse(content="final"),
            ]
        ),
        tools=ToolRegistry(),
        context=ToolContext(tmp_path),
        settings=RuntimeSettings(max_steps=3, system_prompt="Be precise"),
        output=StringIO(),
        context_manager=ContextManager(
            ContextPolicy(
                max_tokens=120,
                trigger_ratio=0.5,
                keep_recent_turns=1,
            )
        ),
    )

    session.run("first " + "x" * 200)
    session.run("second " + "y" * 200)

    assert session.stats.compactions >= 1
    assert any(
        message.content.startswith("[Context summary]")
        for message in session.history
    )


def test_persisted_session_resumes_history_stats_and_transcript(tmp_path: Path) -> None:
    first = AgentSession(
        model=MockModel([ModelResponse(content="First")]),
        tools=ToolRegistry(),
        context=ToolContext(tmp_path),
        settings=RuntimeSettings(max_steps=3, system_prompt="Be precise"),
        output=StringIO(),
        session_store=SessionStore(tmp_path),
        session_record=SessionStore(tmp_path).create(),
    )
    first.run("one")

    restored_record = SessionStore(tmp_path).load(first.session_id or "")
    second_model = MockModel([ModelResponse(content="Second")])
    second = AgentSession(
        model=second_model,
        tools=ToolRegistry(),
        context=ToolContext(tmp_path),
        settings=RuntimeSettings(max_steps=3, system_prompt="Be precise"),
        output=StringIO(),
        session_store=SessionStore(tmp_path),
        session_record=restored_record,
    )
    second.run("two")

    assert second.stats.turns == 2
    assert [message.content for message in second_model.requests[0].messages[-3:]] == [
        "one",
        "First",
        "two",
    ]
    assert "user: one" in second.transcript()
    assert "assistant: Second" in second.transcript()
