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
from minicode_rebuild.core import ModelResponse, TokenUsage
from minicode_rebuild.models import MockModel
from minicode_rebuild.permissions import (
    PermissionDecision,
    PermissionRequest,
    RiskLevel,
)
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
        "turns=2 steps=2 tools=0 tokens=8 (input=5 output=3)"
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
