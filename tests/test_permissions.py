from __future__ import annotations

from pathlib import Path

import pytest

from minicode_rebuild.permissions import (
    PermissionDecision,
    PermissionDeniedError,
    PermissionManager,
    PermissionRequest,
    RiskLevel,
    classify_command_risk,
    classify_file_risk,
)
from minicode_rebuild.tooling import ToolContext


def make_request(scope: str = "file:/workspace/demo.txt") -> PermissionRequest:
    return PermissionRequest(
        operation="write_file",
        risk=RiskLevel.HIGH,
        summary="Overwrite demo.txt",
        scope=scope,
        details=("target: demo.txt",),
    )


def test_permission_manager_defaults_to_deny_without_prompt() -> None:
    manager = PermissionManager()

    with pytest.raises(PermissionDeniedError) as captured:
        manager.authorize(make_request())

    assert captured.value.error_code == "permission_required"


def test_allow_once_prompts_for_each_request() -> None:
    prompts: list[PermissionRequest] = []
    manager = PermissionManager(
        prompt=lambda request: prompts.append(request)
        or PermissionDecision.ALLOW_ONCE
    )

    manager.authorize(make_request())
    manager.authorize(make_request())

    assert len(prompts) == 2


def test_allow_session_reuses_only_exact_scope() -> None:
    prompts: list[PermissionRequest] = []
    manager = PermissionManager(
        prompt=lambda request: prompts.append(request) or "allow_session"
    )

    manager.authorize(make_request("file:first"))
    manager.authorize(make_request("file:first"))
    manager.authorize(make_request("file:second"))

    assert [request.scope for request in prompts] == ["file:first", "file:second"]


@pytest.mark.parametrize("decision", [PermissionDecision.DENY, "unexpected"])
def test_deny_and_invalid_decisions_fail_closed(decision: object) -> None:
    manager = PermissionManager(prompt=lambda _request: decision)  # type: ignore[arg-type]

    with pytest.raises(PermissionDeniedError) as captured:
        manager.authorize(make_request())

    assert captured.value.error_code == "permission_denied"


def test_prompt_exception_fails_closed() -> None:
    def fail(_request: PermissionRequest) -> PermissionDecision:
        raise RuntimeError("prompt unavailable")

    manager = PermissionManager(prompt=fail)

    with pytest.raises(PermissionDeniedError, match="decision failed"):
        manager.authorize(make_request())


@pytest.mark.parametrize(
    ("command", "arguments", "expected_level", "reason_fragment"),
    [
        ("git", ["status"], RiskLevel.MEDIUM, "read-only"),
        ("custom-tool", ["check"], RiskLevel.HIGH, "unknown"),
        ("git", ["reset", "--hard"], RiskLevel.CRITICAL, "discard"),
        ("git", ["push", "--force"], RiskLevel.CRITICAL, "history"),
        ("rm", ["-rf", "build"], RiskLevel.CRITICAL, "recursive"),
        ("python", ["script.py"], RiskLevel.CRITICAL, "arbitrary code"),
        ("powershell", ["-Command", "Get-Date"], RiskLevel.CRITICAL, "arbitrary code"),
        ("git.exe", ["reset", "--hard"], RiskLevel.CRITICAL, "discard"),
        ("python.exe", ["-c", "print(1)"], RiskLevel.CRITICAL, "arbitrary code"),
        ("rm.exe", ["-rf", "build"], RiskLevel.CRITICAL, "recursive"),
    ],
)
def test_command_risk_classification(
    command: str,
    arguments: list[str],
    expected_level: RiskLevel,
    reason_fragment: str,
) -> None:
    assessment = classify_command_risk(command, arguments)

    assert assessment.level is expected_level
    assert reason_fragment in assessment.reason


def test_file_risk_classification(tmp_path: Path) -> None:
    new_file = classify_file_risk(tmp_path / "new.txt", existed=False)
    existing = classify_file_risk(tmp_path / "existing.txt", existed=True)
    git_metadata = classify_file_risk(tmp_path / ".git" / "config", existed=True)

    assert new_file.level is RiskLevel.MEDIUM
    assert existing.level is RiskLevel.HIGH
    assert git_metadata.level is RiskLevel.CRITICAL


def test_tool_context_accepts_permission_manager(tmp_path: Path) -> None:
    manager = PermissionManager(prompt=lambda _request: "allow_once")

    context = ToolContext(tmp_path, permissions=manager)

    assert context.permissions is manager


@pytest.mark.parametrize(
    "field_name",
    ["operation", "summary", "scope"],
)
def test_permission_request_rejects_empty_identity_fields(field_name: str) -> None:
    values = {
        "operation": "write_file",
        "risk": RiskLevel.MEDIUM,
        "summary": "Create file",
        "scope": "file:demo.txt",
    }
    values[field_name] = ""

    with pytest.raises(ValueError, match=field_name):
        PermissionRequest(**values)  # type: ignore[arg-type]
