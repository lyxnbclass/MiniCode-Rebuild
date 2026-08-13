"""Permission-gated, atomic workspace file mutation tools."""

from __future__ import annotations

from collections.abc import Mapping

from minicode_rebuild.core import JsonValue
from minicode_rebuild.file_changes import apply_file_change, read_existing_text
from minicode_rebuild.tooling import ToolContext, ToolDefinition, ToolResult
from minicode_rebuild.workspace import WorkspacePathError, resolve_workspace_path

MAX_FILE_CONTENT = 1_000_000


def _resolve(context: ToolContext, path: str):
    try:
        return resolve_workspace_path(context.cwd, path), None
    except WorkspacePathError as exc:
        return None, ToolResult.error(exc.error_code, str(exc))


def _write_file(
    arguments: Mapping[str, JsonValue], context: ToolContext
) -> ToolResult:
    target, error = _resolve(context, str(arguments["path"]))
    if error is not None:
        return error
    assert target is not None
    return apply_file_change(
        target, str(arguments["content"]), context, operation="write_file"
    )


def _load_edit_target(arguments: Mapping[str, JsonValue], context: ToolContext):
    target, error = _resolve(context, str(arguments["path"]))
    if error is not None:
        return None, None, error
    assert target is not None
    content, read_error = read_existing_text(target)
    if read_error is not None:
        return None, None, read_error
    if content is None:
        return None, None, ToolResult.error(
            "path_not_found", "The path does not exist."
        )
    return target, content, None


def _replace_exact(
    content: str, search: str, replace: str, replace_all: bool
) -> tuple[str | None, ToolResult | None]:
    matches = content.count(search)
    if matches == 0:
        return None, ToolResult.error(
            "replacement_not_found", "The requested text was not found."
        )
    if matches > 1 and not replace_all:
        return None, ToolResult.error(
            "ambiguous_match",
            "The requested text occurs more than once; provide more context or set replace_all.",
        )
    count = -1 if replace_all else 1
    return content.replace(search, replace, count), None


def _edit_file(
    arguments: Mapping[str, JsonValue], context: ToolContext
) -> ToolResult:
    target, content, error = _load_edit_target(arguments, context)
    if error is not None:
        return error
    assert target is not None and content is not None
    next_content, replace_error = _replace_exact(
        content,
        str(arguments["old"]),
        str(arguments["new"]),
        bool(arguments.get("replace_all", False)),
    )
    if replace_error is not None:
        return replace_error
    assert next_content is not None
    return apply_file_change(target, next_content, context, operation="edit_file")


def _patch_file(
    arguments: Mapping[str, JsonValue], context: ToolContext
) -> ToolResult:
    target, content, error = _load_edit_target(arguments, context)
    if error is not None:
        return error
    assert target is not None and content is not None
    next_content = content
    replacements = arguments["replacements"]
    assert isinstance(replacements, list)
    for item in replacements:
        assert isinstance(item, Mapping)
        updated, replace_error = _replace_exact(
            next_content,
            str(item["search"]),
            str(item["replace"]),
            bool(item.get("replace_all", False)),
        )
        if replace_error is not None:
            return replace_error
        assert updated is not None
        next_content = updated
    return apply_file_change(target, next_content, context, operation="patch_file")


_PATH_SCHEMA: dict[str, JsonValue] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 4_096,
}
_CONTENT_SCHEMA: dict[str, JsonValue] = {
    "type": "string",
    "maxLength": MAX_FILE_CONTENT,
}
_REPLACEMENT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "search": {"type": "string", "minLength": 1, "maxLength": MAX_FILE_CONTENT},
        "replace": _CONTENT_SCHEMA,
        "replace_all": {"type": "boolean"},
    },
    "required": ["search", "replace"],
    "additionalProperties": False,
}

write_file_tool = ToolDefinition(
    name="write_file",
    description="Create or atomically replace one UTF-8 workspace file after permission.",
    input_schema={
        "type": "object",
        "properties": {"path": _PATH_SCHEMA, "content": _CONTENT_SCHEMA},
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    handler=_write_file,
)

edit_file_tool = ToolDefinition(
    name="edit_file",
    description="Replace one exact text occurrence in a workspace file after permission.",
    input_schema={
        "type": "object",
        "properties": {
            "path": _PATH_SCHEMA,
            "old": {"type": "string", "minLength": 1, "maxLength": MAX_FILE_CONTENT},
            "new": _CONTENT_SCHEMA,
            "replace_all": {"type": "boolean"},
        },
        "required": ["path", "old", "new"],
        "additionalProperties": False,
    },
    handler=_edit_file,
)

patch_file_tool = ToolDefinition(
    name="patch_file",
    description="Atomically apply validated exact replacements to one workspace file.",
    input_schema={
        "type": "object",
        "properties": {
            "path": _PATH_SCHEMA,
            "replacements": {
                "type": "array",
                "items": _REPLACEMENT_SCHEMA,
                "minItems": 1,
                "maxItems": 100,
            },
        },
        "required": ["path", "replacements"],
        "additionalProperties": False,
    },
    handler=_patch_file,
)

WRITE_TOOLS = (write_file_tool, edit_file_tool, patch_file_tool)

__all__ = [
    "MAX_FILE_CONTENT",
    "WRITE_TOOLS",
    "edit_file_tool",
    "patch_file_tool",
    "write_file_tool",
]
