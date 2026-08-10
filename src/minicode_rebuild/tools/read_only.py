"""Bounded read-only tools for inspecting one workspace."""

from __future__ import annotations

import fnmatch
import heapq
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath

from minicode_rebuild.core import JsonValue
from minicode_rebuild.tooling import ToolContext, ToolDefinition, ToolResult
from minicode_rebuild.workspace import WorkspacePathError, resolve_workspace_path

DEFAULT_READ_LIMIT = 8_000
MAX_READ_LIMIT = 16_000
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 1_000
DEFAULT_SEARCH_LIMIT = 200
MAX_SEARCH_LIMIT = 500
MAX_GLOB_CANDIDATES = 5_000
MAX_GREP_FILES = 5_000
MAX_GREP_FILE_BYTES = 1_048_576
MAX_GREP_LINE_CHARS = 500
TOOL_OUTPUT_LIMIT = 20_000
_READ_SKIP_CHUNK = 8_192
_LINE_TRUNCATION_MARKER = "... [line truncated]"
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)


def _resolve(
    context: ToolContext, input_path: str
) -> tuple[Path | None, ToolResult | None]:
    try:
        return resolve_workspace_path(context.cwd, input_path), None
    except WorkspacePathError as exc:
        return None, ToolResult.error(exc.error_code, str(exc))


def _workspace_root(context: ToolContext) -> Path:
    return resolve_workspace_path(context.cwd, ".")


def _relative_path(path: Path, workspace_root: Path) -> str:
    relative = path.relative_to(workspace_root).as_posix()
    return relative or "."


def _is_safe_candidate(path: Path, workspace_root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(workspace_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _path_type_error(target: Path, *, expected: str) -> ToolResult | None:
    try:
        exists = target.exists()
        is_expected = target.is_file() if expected == "file" else target.is_dir()
    except OSError:
        return ToolResult.error("path_error", "The path cannot be inspected.")
    if not exists:
        return ToolResult.error("path_not_found", "The path does not exist.")
    if not is_expected:
        error_code = "not_a_file" if expected == "file" else "not_a_directory"
        return ToolResult.error(error_code, f"The path is not a {expected}.")
    return None


def _read_file(
    arguments: Mapping[str, JsonValue], context: ToolContext
) -> ToolResult:
    target, error = _resolve(context, str(arguments["path"]))
    if error is not None:
        return error
    assert target is not None
    type_error = _path_type_error(target, expected="file")
    if type_error is not None:
        return type_error

    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", DEFAULT_READ_LIMIT))
    try:
        with target.open("r", encoding="utf-8", newline="") as stream:
            remaining = offset
            while remaining:
                skipped = stream.read(min(remaining, _READ_SKIP_CHUNK))
                if not skipped:
                    break
                remaining -= len(skipped)
            window = stream.read(limit + 1)
    except UnicodeDecodeError:
        return ToolResult.error(
            "invalid_utf8", "The file is not valid UTF-8 text."
        )
    except OSError:
        return ToolResult.error("read_error", "The file could not be read.")

    truncated = len(window) > limit
    content = window[:limit]
    end = offset + len(content)
    workspace_root = _workspace_root(context)
    lines = [
        f"FILE: {_relative_path(target, workspace_root)}",
        f"OFFSET: {offset}",
        f"END: {end}",
        f"TRUNCATED: {'yes' if truncated else 'no'}",
    ]
    if truncated:
        lines.append(f"NEXT_OFFSET: {end}")
    return ToolResult.success("\n".join(lines) + "\n\n" + content)


def _list_files(
    arguments: Mapping[str, JsonValue], context: ToolContext
) -> ToolResult:
    target, error = _resolve(context, str(arguments.get("path", ".")))
    if error is not None:
        return error
    assert target is not None
    type_error = _path_type_error(target, expected="directory")
    if type_error is not None:
        return type_error

    limit = int(arguments.get("limit", DEFAULT_LIST_LIMIT))
    workspace_root = _workspace_root(context)
    try:
        entries = heapq.nsmallest(
            limit + 1,
            target.iterdir(),
            key=lambda item: (item.name.casefold(), item.name),
        )
    except OSError:
        return ToolResult.error(
            "list_error", "The directory could not be listed."
        )

    safe_entries = [
        entry for entry in entries if _is_safe_candidate(entry, workspace_root)
    ]
    truncated = len(entries) > limit
    shown = safe_entries[:limit]
    output_lines: list[str] = []
    for entry in shown:
        relative = _relative_path(entry, workspace_root)
        try:
            is_directory = entry.is_dir()
        except OSError:
            continue
        output_lines.append(
            f"{'dir' if is_directory else 'file'} {relative}"
            + ("/" if is_directory else "")
        )
    if not output_lines:
        output_lines.append("(empty)")
    output_lines.extend(
        [
            "",
            f"SHOWN: {len(shown)}",
            f"TRUNCATED: {'yes' if truncated else 'no'}",
        ]
    )
    return ToolResult.success("\n".join(output_lines))


def _validate_glob_pattern(pattern: str) -> str | None:
    if not pattern.strip():
        return "The glob pattern must not be empty."
    normalized = pattern.replace("\\", "/")
    if (
        normalized.startswith("/")
        or normalized.startswith("//")
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        return "The glob pattern must be relative."
    if ".." in PurePosixPath(normalized).parts:
        return "The glob pattern must not contain parent traversal."
    return None


def _is_ignored(path: Path, search_root: Path) -> bool:
    try:
        parts = path.relative_to(search_root).parts
    except ValueError:
        return True
    return any(part in _SKIP_DIRS for part in parts)


def _glob_search(
    arguments: Mapping[str, JsonValue], context: ToolContext
) -> ToolResult:
    pattern = str(arguments["pattern"])
    pattern_error = _validate_glob_pattern(pattern)
    if pattern_error is not None:
        return ToolResult.error("invalid_glob", pattern_error)

    search_root, error = _resolve(context, str(arguments.get("path", ".")))
    if error is not None:
        return error
    assert search_root is not None
    type_error = _path_type_error(search_root, expected="directory")
    if type_error is not None:
        return type_error

    limit = int(arguments.get("limit", DEFAULT_SEARCH_LIMIT))
    workspace_root = _workspace_root(context)
    matches: list[Path] = []
    candidate_count = 0
    scan_limited = False
    try:
        for candidate in search_root.glob(pattern):
            candidate_count += 1
            if candidate_count > MAX_GLOB_CANDIDATES:
                scan_limited = True
                break
            if _is_ignored(candidate, search_root):
                continue
            if not _is_safe_candidate(candidate, workspace_root):
                continue
            matches.append(candidate)
            if len(matches) > limit:
                break
    except (OSError, ValueError):
        return ToolResult.error("glob_error", "The glob search failed.")

    matches.sort(
        key=lambda item: (
            _relative_path(item, workspace_root).casefold(),
            _relative_path(item, workspace_root),
        )
    )
    truncated = len(matches) > limit or scan_limited
    shown = matches[:limit]
    output_lines = [_relative_path(item, workspace_root) for item in shown]
    if not output_lines:
        output_lines.append("No matches found.")
    output_lines.extend(
        [
            "",
            f"MATCHES: {len(shown)}",
            f"TRUNCATED: {'yes' if truncated else 'no'}",
        ]
    )
    return ToolResult.success("\n".join(output_lines))


def _matches_include(path: Path, pattern: str | None) -> bool:
    if pattern is None:
        return True
    posix_path = path.as_posix()
    return PurePosixPath(posix_path).match(pattern) or fnmatch.fnmatchcase(
        path.name, pattern
    )


def _iter_search_files(root: Path) -> Iterator[Path]:
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(
            (name for name in directory_names if name not in _SKIP_DIRS),
            key=lambda name: (name.casefold(), name),
        )
        for file_name in sorted(file_names, key=lambda name: (name.casefold(), name)):
            yield Path(directory) / file_name


def _line_preview(line: str) -> str:
    if len(line) <= MAX_GREP_LINE_CHARS:
        return line
    available = MAX_GREP_LINE_CHARS - len(_LINE_TRUNCATION_MARKER)
    return line[:available] + _LINE_TRUNCATION_MARKER


def _grep_files(
    arguments: Mapping[str, JsonValue], context: ToolContext
) -> ToolResult:
    flags = 0 if arguments.get("case_sensitive", False) else re.IGNORECASE
    try:
        expression = re.compile(str(arguments["pattern"]), flags)
    except re.error:
        return ToolResult.error(
            "invalid_regex", "The regular expression is invalid."
        )

    include = arguments.get("include")
    include_pattern = str(include) if include is not None else None
    if include_pattern is not None:
        pattern_error = _validate_glob_pattern(include_pattern)
        if pattern_error is not None:
            return ToolResult.error("invalid_glob", pattern_error)

    search_root, error = _resolve(context, str(arguments.get("path", ".")))
    if error is not None:
        return error
    assert search_root is not None
    type_error = _path_type_error(search_root, expected="directory")
    if type_error is not None:
        return type_error

    limit = int(arguments.get("limit", DEFAULT_SEARCH_LIMIT))
    workspace_root = _workspace_root(context)
    results: list[str] = []
    files_scanned = 0
    skipped_large = 0
    skipped_non_text = 0
    skipped_unreadable = 0
    scan_limited = False
    match_limited = False

    try:
        file_iterator = _iter_search_files(search_root)
        for file_path in file_iterator:
            if files_scanned >= MAX_GREP_FILES:
                scan_limited = True
                break
            relative_to_search = file_path.relative_to(search_root)
            if not _matches_include(relative_to_search, include_pattern):
                continue
            if not _is_safe_candidate(file_path, workspace_root):
                skipped_unreadable += 1
                continue
            files_scanned += 1
            try:
                if file_path.stat().st_size > MAX_GREP_FILE_BYTES:
                    skipped_large += 1
                    continue
                file_matches: list[str] = []
                with file_path.open("r", encoding="utf-8", newline="") as stream:
                    for line_number, raw_line in enumerate(stream, start=1):
                        line = raw_line.rstrip("\r\n")
                        if expression.search(line):
                            display_path = _relative_path(file_path, workspace_root)
                            file_matches.append(
                                f"{display_path}:{line_number}:{_line_preview(line)}"
                            )
                            if len(results) + len(file_matches) > limit:
                                match_limited = True
                                break
                results.extend(file_matches)
            except UnicodeDecodeError:
                skipped_non_text += 1
                continue
            except OSError:
                skipped_unreadable += 1
                continue
            if match_limited:
                break
    except OSError:
        return ToolResult.error("grep_error", "The content search failed.")

    shown = results[:limit]
    output_lines = shown or ["No matches found."]
    output_lines.extend(
        [
            "",
            f"MATCHES: {len(shown)}",
            f"FILES_SCANNED: {files_scanned}",
            f"SKIPPED_LARGE: {skipped_large}",
            f"SKIPPED_NON_TEXT: {skipped_non_text}",
            f"SKIPPED_UNREADABLE: {skipped_unreadable}",
            f"TRUNCATED: {'yes' if match_limited or scan_limited else 'no'}",
        ]
    )
    return ToolResult.success("\n".join(output_lines))


read_file_tool = ToolDefinition(
    name="read_file",
    description="Read one bounded UTF-8 text window inside the workspace.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 4_096},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_READ_LIMIT,
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    handler=_read_file,
    output_limit=TOOL_OUTPUT_LIMIT,
)

list_files_tool = ToolDefinition(
    name="list_files",
    description="List bounded direct children of a workspace directory.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 4_096},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LIST_LIMIT,
            },
        },
        "additionalProperties": False,
    },
    handler=_list_files,
    output_limit=TOOL_OUTPUT_LIMIT,
)

glob_search_tool = ToolDefinition(
    name="glob_search",
    description="Find bounded workspace paths matching a relative glob.",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1, "maxLength": 1_024},
            "path": {"type": "string", "minLength": 1, "maxLength": 4_096},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SEARCH_LIMIT,
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    handler=_glob_search,
    output_limit=TOOL_OUTPUT_LIMIT,
)

grep_files_tool = ToolDefinition(
    name="grep_files",
    description="Search bounded UTF-8 workspace files with a regular expression.",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1, "maxLength": 1_024},
            "path": {"type": "string", "minLength": 1, "maxLength": 4_096},
            "include": {"type": "string", "minLength": 1, "maxLength": 1_024},
            "case_sensitive": {"type": "boolean"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SEARCH_LIMIT,
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    handler=_grep_files,
    output_limit=TOOL_OUTPUT_LIMIT,
)

READ_ONLY_TOOLS = (
    read_file_tool,
    list_files_tool,
    glob_search_tool,
    grep_files_tool,
)

__all__ = [
    "READ_ONLY_TOOLS",
    "glob_search_tool",
    "grep_files_tool",
    "list_files_tool",
    "read_file_tool",
]
