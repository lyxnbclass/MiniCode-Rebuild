"""Permission-gated execution of structured foreground commands."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from minicode_rebuild.core import JsonValue
from minicode_rebuild.permissions import (
    PermissionDeniedError,
    PermissionManager,
    PermissionRequest,
    classify_command_risk,
)
from minicode_rebuild.tooling import ToolContext, ToolDefinition, ToolResult
from minicode_rebuild.workspace import WorkspacePathError, resolve_workspace_path

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
_COMMAND_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _format_output(
    stdout: str | bytes | None, stderr: str | bytes | None, code: int | str
) -> str:
    return (
        f"STDOUT:\n{_as_text(stdout)}\n"
        f"STDERR:\n{_as_text(stderr)}\n"
        f"EXIT_CODE: {code}"
    )


def _command_cwd(
    context: ToolContext, raw_cwd: str
) -> tuple[Path | None, ToolResult | None]:
    try:
        target = resolve_workspace_path(context.cwd, raw_cwd)
    except WorkspacePathError as exc:
        return None, ToolResult.error(exc.error_code, str(exc))
    try:
        if not target.exists():
            return None, ToolResult.error("path_not_found", "The path does not exist.")
        if not target.is_dir():
            return None, ToolResult.error("not_a_directory", "The path is not a directory.")
    except OSError:
        return None, ToolResult.error("path_error", "The path cannot be inspected.")
    return target, None


def _run_command(
    arguments: Mapping[str, JsonValue], context: ToolContext
) -> ToolResult:
    command = str(arguments["command"])
    if not _COMMAND_PATTERN.fullmatch(command) or command in {".", ".."}:
        return ToolResult.error(
            "invalid_command",
            "The command must be a single executable name without a path or shell syntax.",
        )
    raw_args = arguments.get("args", [])
    assert isinstance(raw_args, list)
    command_args = [str(item) for item in raw_args]
    cwd, cwd_error = _command_cwd(context, str(arguments.get("cwd", ".")))
    if cwd_error is not None:
        return cwd_error
    assert cwd is not None
    timeout = int(arguments.get("timeout", DEFAULT_TIMEOUT))
    assessment = classify_command_risk(command, command_args)
    vector = [command, *command_args]
    signature = json.dumps(vector, ensure_ascii=False, separators=(",", ":"))
    request = PermissionRequest(
        operation="run_command",
        risk=assessment.level,
        summary=f"Run {command}",
        scope=f"command:{cwd}:{signature}",
        details=(assessment.reason, f"cwd: {cwd}", f"argv: {signature}"),
    )
    try:
        (context.permissions or PermissionManager()).authorize(request)
    except PermissionDeniedError as exc:
        return ToolResult.error(exc.error_code, str(exc))

    try:
        completed = subprocess.run(
            vector,
            shell=False,
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return ToolResult.error(
            "command_timeout", _format_output(exc.output, exc.stderr, "timeout")
        )
    except FileNotFoundError:
        return ToolResult.error("command_not_found", f"Command not found: {command}")
    except OSError as exc:
        return ToolResult.error("command_error", f"Command could not start: {exc}")

    output = _format_output(completed.stdout, completed.stderr, completed.returncode)
    if completed.returncode:
        return ToolResult.error("command_failed", output)
    return ToolResult.success(output)


run_command_tool = ToolDefinition(
    name="run_command",
    description="Run a structured foreground command inside the workspace after permission.",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1, "maxLength": 256},
            "args": {
                "type": "array",
                "items": {"type": "string", "maxLength": 16_384},
                "maxItems": 256,
            },
            "cwd": {"type": "string", "minLength": 1, "maxLength": 4_096},
            "timeout": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    handler=_run_command,
)

__all__ = ["DEFAULT_TIMEOUT", "MAX_TIMEOUT", "run_command_tool"]
