from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import minicode_rebuild.tools.command as command_module
from minicode_rebuild.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionRequest,
    RiskLevel,
)
from minicode_rebuild.tooling import ToolContext, ToolRegistry, ToolResult
from minicode_rebuild.tools import run_command_tool


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry([run_command_tool])


def execute(
    registry: ToolRegistry,
    workspace: Path,
    arguments: dict,
    permissions: PermissionManager | None = None,
) -> ToolResult:
    return registry.execute(
        "run_command",
        arguments,
        ToolContext(workspace, permissions=permissions),
    )


def allow_once(
    requests: list[PermissionRequest] | None = None,
) -> PermissionManager:
    def prompt(request: PermissionRequest) -> PermissionDecision:
        if requests is not None:
            requests.append(request)
        return PermissionDecision.ALLOW_ONCE

    return PermissionManager(prompt=prompt)


def test_command_requires_permission_before_starting_process(
    tmp_path: Path,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        command_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("process must not start"),
    )

    result = execute(registry, tmp_path, {"command": "git", "args": ["status"]})

    assert result.ok is False
    assert result.error_code == "permission_required"


def test_command_executes_argument_vector_with_shell_disabled(
    tmp_path: Path,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "clean\n", "warning\n")

    monkeypatch.setattr(command_module.subprocess, "run", fake_run)

    result = execute(
        registry,
        tmp_path,
        {"command": "git", "args": ["status", "--short"], "timeout": 10},
        allow_once(),
    )

    assert result.ok is True
    assert captured["command"] == ["git", "status", "--short"]
    assert captured["shell"] is False
    assert captured["cwd"] == str(tmp_path.resolve())
    assert captured["timeout"] == 10
    assert captured["capture_output"] is True
    assert "STDOUT:\nclean" in result.output
    assert "STDERR:\nwarning" in result.output
    assert "EXIT_CODE: 0" in result.output


def test_failed_command_returns_stable_error_code(
    tmp_path: Path,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        command_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 7, "", "failed"
        ),
    )

    result = execute(
        registry,
        tmp_path,
        {"command": "custom-tool", "args": ["check"]},
        allow_once(),
    )

    assert result.ok is False
    assert result.error_code == "command_failed"
    assert "EXIT_CODE: 7" in result.output
    assert "failed" in result.output


def test_command_timeout_returns_partial_output(
    tmp_path: Path,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd=["custom-tool"], timeout=3, output="partial", stderr="late error"
        )

    monkeypatch.setattr(command_module.subprocess, "run", timeout)

    result = execute(
        registry,
        tmp_path,
        {"command": "custom-tool", "timeout": 3},
        allow_once(),
    )

    assert result.ok is False
    assert result.error_code == "command_timeout"
    assert "partial" in result.output
    assert "late error" in result.output


def test_command_not_found_is_normalized(
    tmp_path: Path,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        command_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )

    result = execute(
        registry,
        tmp_path,
        {"command": "missing-tool"},
        allow_once(),
    )

    assert result.ok is False
    assert result.error_code == "command_not_found"


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git|cat",
        "powershell -Command Get-Date",
        "../tool",
        "C:\\tool.exe",
    ],
)
def test_command_rejects_shell_snippets_and_paths_before_prompt(
    tmp_path: Path,
    registry: ToolRegistry,
    command: str,
) -> None:
    permissions = PermissionManager(
        prompt=lambda _request: pytest.fail("invalid command must not prompt")
    )

    result = execute(
        registry,
        tmp_path,
        {"command": command},
        permissions,
    )

    assert result.ok is False
    assert result.error_code == "invalid_command"


def test_dangerous_command_surfaces_critical_risk_and_denial_prevents_execution(
    tmp_path: Path,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[PermissionRequest] = []
    permissions = PermissionManager(
        prompt=lambda request: requests.append(request) or "deny"
    )
    monkeypatch.setattr(
        command_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("denied command must not run"),
    )

    result = execute(
        registry,
        tmp_path,
        {"command": "git", "args": ["reset", "--hard"]},
        permissions,
    )

    assert result.ok is False
    assert result.error_code == "permission_denied"
    assert requests[0].risk is RiskLevel.CRITICAL
    assert "discard" in "\n".join(requests[0].details)


def test_command_risk_is_exposed_for_readonly_and_unknown_commands(
    tmp_path: Path,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[PermissionRequest] = []
    permissions = allow_once(requests)
    monkeypatch.setattr(
        command_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    execute(
        registry,
        tmp_path,
        {"command": "git", "args": ["status"]},
        permissions,
    )
    execute(
        registry,
        tmp_path,
        {"command": "custom-tool", "args": ["check"]},
        permissions,
    )

    assert [request.risk for request in requests] == [RiskLevel.MEDIUM, RiskLevel.HIGH]


def test_command_session_permission_uses_exact_signature(
    tmp_path: Path,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[PermissionRequest] = []
    permissions = PermissionManager(
        prompt=lambda request: requests.append(request) or "allow_session"
    )
    monkeypatch.setattr(
        command_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    execute(
        registry,
        tmp_path,
        {"command": "git", "args": ["status"]},
        permissions,
    )
    execute(
        registry,
        tmp_path,
        {"command": "git", "args": ["status"]},
        permissions,
    )
    execute(
        registry,
        tmp_path,
        {"command": "git", "args": ["diff"]},
        permissions,
    )

    assert len(requests) == 2


def test_command_rejects_outside_and_non_directory_cwd_before_prompt(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    file_path = workspace / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    permissions = PermissionManager(
        prompt=lambda _request: pytest.fail("invalid cwd must not prompt")
    )

    escaped = execute(
        registry,
        workspace,
        {"command": "git", "args": ["status"], "cwd": str(outside)},
        permissions,
    )
    wrong_type = execute(
        registry,
        workspace,
        {"command": "git", "args": ["status"], "cwd": "file.txt"},
        permissions,
    )

    assert escaped.error_code == "path_outside_workspace"
    assert wrong_type.error_code == "not_a_directory"


def test_command_output_is_capped_by_registry(
    tmp_path: Path,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        command_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "x" * 25_000, ""
        ),
    )

    result = execute(
        registry,
        tmp_path,
        {"command": "custom-tool"},
        allow_once(),
    )

    assert result.ok is True
    assert result.truncated is True
    assert result.original_length is not None
    assert len(result.output) == 20_000


def test_command_schema_rejects_timeout_above_maximum(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    result = execute(
        registry,
        tmp_path,
        {"command": "git", "args": ["status"], "timeout": 301},
        allow_once(),
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
