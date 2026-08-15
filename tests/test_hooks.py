from __future__ import annotations

from pathlib import Path

import pytest

from minicode_rebuild.hooks import HookEvent, HookManager
from minicode_rebuild.tooling import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)


def _tool(handler=None) -> ToolDefinition:
    return ToolDefinition(
        name="sample",
        description="Sample.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=handler or (lambda arguments, context: ToolResult.success("done")),
    )


def test_hooks_run_in_registration_order_with_read_only_snapshots() -> None:
    hooks = HookManager()
    calls: list[str] = []

    def first(context) -> None:  # type: ignore[no-untyped-def]
        calls.append(context.data["value"])
        with pytest.raises(TypeError):
            context.data["changed"] = True

    hooks.register(HookEvent.AGENT_START, first, name="first")
    hooks.register(
        HookEvent.AGENT_START,
        lambda context: calls.append("second"),
        name="second",
    )

    report = hooks.emit(HookEvent.AGENT_START, value="start")

    assert calls == ["start", "second"]
    assert report.handlers == 2
    assert report.failures == ()


def test_hook_failure_is_reported_and_does_not_skip_later_hook() -> None:
    hooks = HookManager()
    calls: list[str] = []
    hooks.register(
        HookEvent.AGENT_STOP,
        lambda context: (_ for _ in ()).throw(RuntimeError("boom")),
        name="broken",
    )
    hooks.register(
        HookEvent.AGENT_STOP,
        lambda context: calls.append("healthy"),
        name="healthy",
    )

    report = hooks.emit(HookEvent.AGENT_STOP)

    assert calls == ["healthy"]
    assert report.failures[0].name == "broken"
    assert report.failures[0].error == "RuntimeError: boom"
    assert hooks.reports() == (report,)


def test_tool_boundary_emits_before_and_after_hooks(tmp_path: Path) -> None:
    hooks = HookManager()
    events: list[tuple[HookEvent, object]] = []
    hooks.register(
        HookEvent.BEFORE_TOOL,
        lambda context: events.append((context.event, context.data["tool_name"])),
    )
    hooks.register(
        HookEvent.AFTER_TOOL,
        lambda context: events.append((context.event, context.data["result"])),
    )

    result = ToolRegistry([_tool()]).execute(
        "sample", {"value": "x"}, ToolContext(tmp_path, hooks=hooks)
    )

    assert result.ok is True
    assert events[0] == (HookEvent.BEFORE_TOOL, "sample")
    assert events[1][0] is HookEvent.AFTER_TOOL
    assert events[1][1]["ok"] is True  # type: ignore[index]


def test_failing_tool_hook_does_not_block_tool_and_is_observable(tmp_path: Path) -> None:
    hooks = HookManager()
    reports = []
    hooks.register(
        HookEvent.BEFORE_TOOL,
        lambda context: (_ for _ in ()).throw(ValueError("bad extension")),
        name="bad-hook",
    )
    context = ToolContext(tmp_path, hooks=hooks, hook_observer=reports.append)

    result = ToolRegistry([_tool()]).execute("sample", {"value": "x"}, context)

    assert result == ToolResult.success("done")
    assert reports[0].failures[0].name == "bad-hook"
