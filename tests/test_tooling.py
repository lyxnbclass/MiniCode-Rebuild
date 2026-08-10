from __future__ import annotations

from pathlib import Path

import pytest

from minicode_rebuild.core import ModelTool
from minicode_rebuild.tooling import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)


def make_tool(
    *,
    name: str = "sample",
    schema: dict[str, object] | None = None,
    handler=None,
    output_limit: int | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="A sample tool.",
        input_schema=schema
        or {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=handler or (lambda arguments, context: ToolResult.success("ok")),
        output_limit=output_limit,
    )


def test_context_normalizes_cwd_and_preserves_shared_state(tmp_path: Path) -> None:
    state: dict[str, object] = {"calls": 0}

    context = ToolContext(cwd=str(tmp_path), state=state)

    assert context.cwd == tmp_path
    assert context.state is state


def test_registry_registers_finds_and_exports_model_tools() -> None:
    tool = make_tool()
    registry = ToolRegistry([tool])

    assert registry.list() == (tool,)
    assert registry.find("sample") is tool
    assert registry.find("missing") is None
    assert registry.model_tools() == (
        ModelTool(
            name="sample",
            description="A sample tool.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
    )


def test_registry_rejects_duplicate_names() -> None:
    registry = ToolRegistry([make_tool()])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_tool())


def test_definition_rejects_invalid_schema_and_small_output_limit() -> None:
    with pytest.raises(ValueError, match="root type must be 'object'"):
        make_tool(schema={"type": "string"})

    with pytest.raises(ValueError, match="at least 32"):
        make_tool(output_limit=31)


def test_execute_passes_validated_arguments_and_context(tmp_path: Path) -> None:
    seen: list[tuple[dict[str, object], ToolContext]] = []

    def handler(arguments, context):
        seen.append((dict(arguments), context))
        context.state["calls"] = 1
        return ToolResult.success(f"hello {arguments['name']}")

    tool = make_tool(
        schema={
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=handler,
    )
    context = ToolContext(tmp_path)

    result = ToolRegistry([tool]).execute("sample", {"name": "Ada"}, context)

    assert result == ToolResult.success("hello Ada")
    assert seen == [({"name": "Ada"}, context)]
    assert context.state == {"calls": 1}


def test_execute_reports_unknown_tool_without_raising(tmp_path: Path) -> None:
    result = ToolRegistry().execute("missing", {}, ToolContext(tmp_path))

    assert result.ok is False
    assert result.error_code == "unknown_tool"
    assert "missing" in result.output


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "required property 'count'"),
        ({"count": True}, "must be integer"),
        ({"count": 1, "extra": "no"}, "unexpected property 'extra'"),
        ({"count": -1}, "greater than or equal to 0"),
    ],
)
def test_execute_rejects_invalid_object_arguments(
    tmp_path: Path, arguments: dict[str, object], message: str
) -> None:
    called = False

    def handler(arguments, context):
        nonlocal called
        called = True
        return ToolResult.success("should not run")

    tool = make_tool(
        schema={
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 0},
            },
            "required": ["count"],
            "additionalProperties": False,
        },
        handler=handler,
    )

    result = ToolRegistry([tool]).execute("sample", arguments, ToolContext(tmp_path))

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
    assert message in result.output
    assert called is False


def test_execute_validates_nested_arrays_and_enum(tmp_path: Path) -> None:
    tool = make_tool(
        schema={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["read", "write"],
                            }
                        },
                        "required": ["mode"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
    )

    result = ToolRegistry([tool]).execute(
        "sample", {"items": [{"mode": "delete"}]}, ToolContext(tmp_path)
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
    assert "$.items[0].mode" in result.output
    assert "allowed values" in result.output


def test_execute_rejects_non_mapping_arguments(tmp_path: Path) -> None:
    result = ToolRegistry([make_tool()]).execute(
        "sample", ["not", "an", "object"], ToolContext(tmp_path)  # type: ignore[arg-type]
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"


def test_execute_isolates_handler_exception(tmp_path: Path) -> None:
    def handler(arguments, context):
        raise RuntimeError("boom")

    result = ToolRegistry([make_tool(handler=handler)]).execute(
        "sample", {}, ToolContext(tmp_path)
    )

    assert result.ok is False
    assert result.error_code == "execution_error"
    assert "RuntimeError: boom" in result.output


def test_execute_rejects_non_tool_result(tmp_path: Path) -> None:
    result = ToolRegistry([make_tool(handler=lambda arguments, context: "wrong")]).execute(
        "sample", {}, ToolContext(tmp_path)
    )

    assert result.ok is False
    assert result.error_code == "invalid_result"
    assert "ToolResult" in result.output


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(2)])
def test_execute_does_not_swallow_process_control_exceptions(
    tmp_path: Path, interrupt: BaseException
) -> None:
    def handler(arguments, context):
        raise interrupt

    with pytest.raises(type(interrupt)):
        ToolRegistry([make_tool(handler=handler)]).execute(
            "sample", {}, ToolContext(tmp_path)
        )


def test_execute_truncates_success_output_and_preserves_both_ends(
    tmp_path: Path,
) -> None:
    output = "HEAD" + ("x" * 200) + "TAIL"
    registry = ToolRegistry(
        [make_tool(handler=lambda arguments, context: ToolResult.success(output))],
        default_output_limit=80,
    )

    result = registry.execute("sample", {}, ToolContext(tmp_path))

    assert result.ok is True
    assert result.truncated is True
    assert result.original_length == len(output)
    assert len(result.output) <= 80
    assert result.output.startswith("HEAD")
    assert result.output.endswith("TAIL")
    assert "output truncated" in result.output


def test_tool_output_limit_overrides_registry_default(tmp_path: Path) -> None:
    tool = make_tool(
        output_limit=40,
        handler=lambda arguments, context: ToolResult.error("domain_error", "z" * 100),
    )

    result = ToolRegistry([tool], default_output_limit=100).execute(
        "sample", {}, ToolContext(tmp_path)
    )

    assert result.ok is False
    assert result.error_code == "domain_error"
    assert result.truncated is True
    assert len(result.output) <= 40


def test_oversized_validation_errors_are_also_truncated(tmp_path: Path) -> None:
    long_name = "x" * 120
    tool = make_tool(
        output_limit=64,
        schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )

    result = ToolRegistry([tool]).execute(
        "sample", {long_name: True}, ToolContext(tmp_path)
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
    assert result.truncated is True
    assert len(result.output) <= 64


def test_tool_result_factories_enforce_success_and_error_shape() -> None:
    assert ToolResult.success("done") == ToolResult(ok=True, output="done")
    assert ToolResult.error("bad_input", "no") == ToolResult(
        ok=False, output="no", error_code="bad_input"
    )

    with pytest.raises(ValueError, match="error code"):
        ToolResult.error("", "no")
