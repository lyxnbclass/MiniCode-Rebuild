"""Executable tool definitions and the registry execution boundary."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self, TypeAlias

from minicode_rebuild.core import JsonValue, ModelTool

ToolHandler: TypeAlias = Callable[
    [Mapping[str, JsonValue], "ToolContext"], "ToolResult"
]

_MIN_OUTPUT_LIMIT = 32
_TRUNCATION_MARKER = "\n... [output truncated] ...\n"
_SCHEMA_ANNOTATIONS = {"description", "title", "default"}
_COMMON_SCHEMA_KEYS = {"type", "enum"} | _SCHEMA_ANNOTATIONS
_TYPE_SCHEMA_KEYS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength"},
    "integer": {"minimum", "maximum"},
    "number": {"minimum", "maximum"},
    "boolean": set(),
    "null": set(),
}


class ToolValidationError(ValueError):
    """Raised when tool arguments do not satisfy their declared schema."""


@dataclass(slots=True)
class ToolContext:
    """State supplied by the caller for one or more tool executions."""

    cwd: Path
    state: MutableMapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cwd = Path(self.cwd)
        if not isinstance(self.state, MutableMapping):
            raise TypeError("tool context state must be a mutable mapping")


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Normalized outcome returned by every executable tool."""

    ok: bool
    output: str
    error_code: str | None = None
    truncated: bool = False
    original_length: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.output, str):
            raise TypeError("tool result output must be a string")
        if self.error_code is not None and not self.error_code.strip():
            raise ValueError("tool result error code must not be empty")
        if self.ok and self.error_code is not None:
            raise ValueError("successful tool result must not have an error code")
        if self.truncated:
            if self.original_length is None or self.original_length <= len(self.output):
                raise ValueError(
                    "truncated tool result requires a larger original length"
                )
        elif self.original_length is not None:
            raise ValueError(
                "non-truncated tool result must not have an original length"
            )

    @classmethod
    def success(cls, output: str = "") -> Self:
        """Build a successful result."""

        return cls(ok=True, output=output)

    @classmethod
    def error(cls, error_code: str, output: str) -> Self:
        """Build a failed result with a stable machine-readable code."""

        if not error_code or not error_code.strip():
            raise ValueError("tool result error code must not be empty")
        return cls(ok=False, output=output, error_code=error_code.strip())


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Bind one model-visible tool schema to an executable handler."""

    name: str
    description: str
    input_schema: Mapping[str, JsonValue]
    handler: ToolHandler
    output_limit: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.description, str):
            raise TypeError("tool description must be a string")
        if not isinstance(self.input_schema, Mapping):
            raise TypeError("tool input schema must be a mapping")
        if not callable(self.handler):
            raise TypeError("tool handler must be callable")
        if self.output_limit is not None:
            _validate_output_limit(self.output_limit)

        schema = deepcopy(dict(self.input_schema))
        _validate_schema_definition(schema, path="$", require_object=True)
        model_tool = ModelTool(
            name=self.name,
            description=self.description,
            parameters=schema,
        )
        object.__setattr__(self, "name", model_tool.name)
        object.__setattr__(self, "input_schema", schema)

    def to_model_tool(self) -> ModelTool:
        """Return the provider-independent declaration visible to a model."""

        return ModelTool(
            name=self.name,
            description=self.description,
            parameters=deepcopy(dict(self.input_schema)),
        )


class ToolRegistry:
    """Register tools and provide one guarded execution path."""

    def __init__(
        self,
        tools: Iterable[ToolDefinition] = (),
        *,
        default_output_limit: int = 20_000,
    ) -> None:
        _validate_output_limit(default_output_limit)
        self._default_output_limit = default_output_limit
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        """Register a unique tool definition."""

        if not isinstance(tool, ToolDefinition):
            raise TypeError("registry entries must be ToolDefinition instances")
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def list(self) -> tuple[ToolDefinition, ...]:
        """Return definitions in registration order as an immutable snapshot."""

        return tuple(self._tools.values())

    def find(self, name: str) -> ToolDefinition | None:
        """Find a registered definition by its exact model-visible name."""

        return self._tools.get(name)

    def model_tools(self) -> tuple[ModelTool, ...]:
        """Return model-visible declarations in registration order."""

        return tuple(tool.to_model_tool() for tool in self._tools.values())

    def execute(
        self,
        name: str,
        arguments: Mapping[str, JsonValue],
        context: ToolContext,
    ) -> ToolResult:
        """Validate and execute one tool without leaking ordinary exceptions."""

        tool = self.find(name)
        if tool is None:
            return self._finalize(
                ToolResult.error("unknown_tool", f"Unknown tool: {name}"),
                self._default_output_limit,
            )

        limit = tool.output_limit or self._default_output_limit
        try:
            _validate_value(tool.input_schema, arguments, path="$")
        except ToolValidationError as exc:
            return self._finalize(
                ToolResult.error(
                    "invalid_arguments",
                    f"Invalid arguments for tool '{tool.name}': {exc}",
                ),
                limit,
            )

        safe_arguments = deepcopy(dict(arguments))
        try:
            result = tool.handler(safe_arguments, context)
        except Exception as exc:
            detail = str(exc)
            suffix = f": {detail}" if detail else ""
            return self._finalize(
                ToolResult.error(
                    "execution_error",
                    f"Error running tool '{tool.name}': "
                    f"{type(exc).__name__}{suffix}",
                ),
                limit,
            )

        if not isinstance(result, ToolResult):
            return self._finalize(
                ToolResult.error(
                    "invalid_result",
                    f"Tool '{tool.name}' must return ToolResult, "
                    f"got {type(result).__name__}",
                ),
                limit,
            )
        return self._finalize(result, limit)

    @staticmethod
    def _finalize(result: ToolResult, limit: int) -> ToolResult:
        if len(result.output) <= limit:
            return result

        available = limit - len(_TRUNCATION_MARKER)
        head_length = (available + 1) // 2
        tail_length = available // 2
        tail = result.output[-tail_length:] if tail_length else ""
        output = result.output[:head_length] + _TRUNCATION_MARKER + tail
        original_length = result.original_length or len(result.output)
        return replace(
            result,
            output=output,
            truncated=True,
            original_length=original_length,
        )


def _validate_output_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("tool output limit must be an integer")
    if limit < _MIN_OUTPUT_LIMIT:
        raise ValueError(
            f"tool output limit must be at least {_MIN_OUTPUT_LIMIT} characters"
        )


def _validate_schema_definition(
    schema: Mapping[str, JsonValue], *, path: str, require_object: bool = False
) -> None:
    schema_type = schema.get("type")
    if not isinstance(schema_type, str) or schema_type not in _TYPE_SCHEMA_KEYS:
        raise ValueError(f"{path} schema type must be one supported string")
    if require_object and schema_type != "object":
        raise ValueError("tool input schema root type must be 'object'")

    unsupported = set(schema) - _COMMON_SCHEMA_KEYS - _TYPE_SCHEMA_KEYS[schema_type]
    if unsupported:
        keyword = sorted(unsupported)[0]
        raise ValueError(f"{path} uses unsupported schema keyword '{keyword}'")

    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ValueError(f"{path}.enum must be a non-empty array")

    if schema_type == "object":
        _validate_object_schema(schema, path)
    elif schema_type == "array":
        _validate_array_schema(schema, path)
    elif schema_type == "string":
        _validate_size_bounds(schema, path, "Length")
    elif schema_type in {"integer", "number"}:
        _validate_number_bounds(schema, path)


def _validate_object_schema(schema: Mapping[str, JsonValue], path: str) -> None:
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError(f"{path}.properties must be an object")
    for name, child_schema in properties.items():
        if not isinstance(name, str) or not isinstance(child_schema, Mapping):
            raise ValueError(f"{path}.properties must map names to schemas")
        _validate_schema_definition(child_schema, path=f"{path}.{name}")

    required = schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(name, str) for name in required
    ):
        raise ValueError(f"{path}.required must be an array of strings")
    if len(required) != len(set(required)):
        raise ValueError(f"{path}.required must not contain duplicates")
    missing_definitions = [name for name in required if name not in properties]
    if missing_definitions:
        raise ValueError(
            f"{path}.required references undefined property "
            f"'{missing_definitions[0]}'"
        )

    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, bool):
        raise ValueError(f"{path}.additionalProperties must be a boolean")


def _validate_array_schema(schema: Mapping[str, JsonValue], path: str) -> None:
    items = schema.get("items")
    if items is not None:
        if not isinstance(items, Mapping):
            raise ValueError(f"{path}.items must be a schema object")
        _validate_schema_definition(items, path=f"{path}[]")
    _validate_size_bounds(schema, path, "Items")


def _validate_size_bounds(
    schema: Mapping[str, JsonValue], path: str, suffix: str
) -> None:
    minimum = schema.get(f"min{suffix}")
    maximum = schema.get(f"max{suffix}")
    for label, value in ((f"min{suffix}", minimum), (f"max{suffix}", maximum)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"{path}.{label} must be a non-negative integer")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"{path} minimum size must not exceed maximum size")


def _validate_number_bounds(schema: Mapping[str, JsonValue], path: str) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    for label, value in (("minimum", minimum), ("maximum", maximum)):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _is_finite_number(value)
        ):
            raise ValueError(f"{path}.{label} must be a finite number")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"{path}.minimum must not exceed maximum")


def _validate_value(
    schema: Mapping[str, JsonValue], value: object, *, path: str
) -> None:
    schema_type = schema["type"]
    if not _matches_type(schema_type, value):
        raise ToolValidationError(f"{path} must be {schema_type}")

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        raise ToolValidationError(f"{path} must be one of the allowed values")

    if schema_type == "object":
        _validate_object_value(schema, value, path)
    elif schema_type == "array":
        _validate_array_value(schema, value, path)
    elif schema_type == "string":
        _validate_sized_value(schema, value, path, "Length")
    elif schema_type in {"integer", "number"}:
        _validate_number_value(schema, value, path)


def _matches_type(schema_type: object, value: object) -> bool:
    if schema_type == "object":
        return isinstance(value, Mapping)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and _is_finite_number(value)
        )
    if schema_type == "boolean":
        return isinstance(value, bool)
    return value is None


def _is_finite_number(value: int | float) -> bool:
    return not isinstance(value, float) or math.isfinite(value)


def _validate_object_value(
    schema: Mapping[str, JsonValue], value: object, path: str
) -> None:
    assert isinstance(value, Mapping)
    properties = schema.get("properties", {})
    assert isinstance(properties, Mapping)
    required = schema.get("required", [])
    assert isinstance(required, list)

    for name in required:
        if name not in value:
            raise ToolValidationError(
                f"{path} is missing required property '{name}'"
            )

    if schema.get("additionalProperties", True) is False:
        for name in value:
            if name not in properties:
                raise ToolValidationError(
                    f"{path} contains unexpected property '{name}'"
                )

    for name, child_schema in properties.items():
        if name in value:
            assert isinstance(child_schema, Mapping)
            _validate_value(child_schema, value[name], path=f"{path}.{name}")


def _validate_array_value(
    schema: Mapping[str, JsonValue], value: object, path: str
) -> None:
    assert isinstance(value, list)
    _validate_sized_value(schema, value, path, "Items")
    items = schema.get("items")
    if items is not None:
        assert isinstance(items, Mapping)
        for index, item in enumerate(value):
            _validate_value(items, item, path=f"{path}[{index}]")


def _validate_sized_value(
    schema: Mapping[str, JsonValue], value: object, path: str, suffix: str
) -> None:
    assert isinstance(value, (str, list))
    minimum = schema.get(f"min{suffix}")
    maximum = schema.get(f"max{suffix}")
    if isinstance(minimum, int) and len(value) < minimum:
        raise ToolValidationError(
            f"{path} must contain at least {minimum} item(s)"
        )
    if isinstance(maximum, int) and len(value) > maximum:
        raise ToolValidationError(
            f"{path} must contain at most {maximum} item(s)"
        )


def _validate_number_value(
    schema: Mapping[str, JsonValue], value: object, path: str
) -> None:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(minimum, (int, float)) and value < minimum:
        raise ToolValidationError(
            f"{path} must be greater than or equal to {minimum}"
        )
    if isinstance(maximum, (int, float)) and value > maximum:
        raise ToolValidationError(f"{path} must be less than or equal to {maximum}")


__all__ = [
    "ToolContext",
    "ToolDefinition",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "ToolValidationError",
]
