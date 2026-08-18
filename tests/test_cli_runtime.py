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
from minicode_rebuild.cost import TokenBudgetPolicy
from minicode_rebuild.hooks import HookEvent, HookManager
from minicode_rebuild.memory import MemoryStore
from minicode_rebuild.models import MockModel
from minicode_rebuild.permissions import (
    PermissionDecision,
    PermissionRequest,
    RiskLevel,
)
from minicode_rebuild.session import SessionStore
from minicode_rebuild.skills import SkillCatalog
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


def test_session_budget_uses_persisted_reported_usage(tmp_path: Path) -> None:
    settings = RuntimeSettings(
        max_steps=3,
        system_prompt="Be precise",
        token_budget_policy=TokenBudgetPolicy(session_tokens=100),
    )
    session = AgentSession(
        model=MockModel(
            [ModelResponse(content="one", usage=TokenUsage(60, 20))]
        ),
        tools=ToolRegistry(),
        context=ToolContext(tmp_path),
        settings=settings,
        output=StringIO(),
    )

    first = session.run("first")
    second = session.run("second")

    assert first.completed is True
    assert second.stop_reason is AgentStopReason.BUDGET_EXHAUSTED
    assert session.stats.input_tokens == 60
    assert session.stats.output_tokens == 20
    assert session.budget_status() == (
        "token-budget=100 used=80 remaining=20 max-output=unbounded"
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


def test_session_injects_skill_catalog_but_not_full_content(tmp_path: Path) -> None:
    skill_path = tmp_path / ".minicode" / "skills" / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: review\ndescription: Review changes.\n---\n\nPRIVATE STEPS",
        encoding="utf-8",
    )
    model = MockModel([ModelResponse(content="Done")])
    session = AgentSession(
        model=model,
        tools=ToolRegistry(),
        context=ToolContext(tmp_path),
        settings=RuntimeSettings(max_steps=3, system_prompt="Be precise"),
        output=StringIO(),
        skill_catalog=SkillCatalog(tmp_path),
    )

    session.run("review")

    prompt = model.requests[0].messages[0].content
    assert "Be precise" in prompt
    assert "review: Review changes." in prompt
    assert "PRIVATE STEPS" not in prompt


def test_session_exposes_memory_policy_without_eager_memory_content(
    tmp_path: Path,
) -> None:
    store = MemoryStore(tmp_path)
    store.add("PRIVATE REMEMBERED FACT")
    model = MockModel([ModelResponse(content="Done")])
    session = AgentSession(
        model=model,
        tools=ToolRegistry(),
        context=ToolContext(tmp_path),
        settings=RuntimeSettings(max_steps=3, system_prompt="Be precise"),
        output=StringIO(),
        memory_store=store,
    )

    session.run("hello")

    prompt = model.requests[0].messages[0].content
    assert "Long-term memory is scoped to this workspace" in prompt
    assert "untrusted" in prompt
    assert "PRIVATE REMEMBERED FACT" not in prompt


def test_session_lifecycle_hook_failure_is_visible_and_nonfatal(tmp_path: Path) -> None:
    hooks = HookManager()
    hooks.register(
        HookEvent.AGENT_START,
        lambda context: (_ for _ in ()).throw(RuntimeError("observer failed")),
        name="broken",
    )
    output = StringIO()
    session = AgentSession(
        model=MockModel([ModelResponse(content="Still works")]),
        tools=ToolRegistry(),
        context=ToolContext(tmp_path),
        settings=RuntimeSettings(max_steps=3),
        output=output,
        hooks=hooks,
    )

    result = session.run("hello")

    assert result.content == "Still works"
    assert "[hook:error] agent_start/broken: RuntimeError: observer failed" in output.getvalue()
