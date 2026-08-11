from __future__ import annotations

from pathlib import Path

import pytest

import minicode_rebuild.file_changes as file_changes
from minicode_rebuild.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionRequest,
    RiskLevel,
)
from minicode_rebuild.tooling import ToolContext, ToolRegistry, ToolResult
from minicode_rebuild.tools import MUTATING_TOOLS


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(MUTATING_TOOLS)


def execute(
    registry: ToolRegistry,
    workspace: Path,
    name: str,
    arguments: dict,
    permissions: PermissionManager | None = None,
) -> ToolResult:
    return registry.execute(
        name,
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


def test_mutating_tools_export_stable_declarations(registry: ToolRegistry) -> None:
    assert [tool.name for tool in registry.model_tools()] == [
        "write_file",
        "edit_file",
        "patch_file",
        "run_command",
    ]
    assert all(
        tool.parameters.get("additionalProperties") is False
        for tool in registry.model_tools()
    )


def test_write_file_requires_permission_before_creating_file(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    result = execute(
        registry,
        tmp_path,
        "write_file",
        {"path": "nested/demo.txt", "content": "hello\n"},
    )

    assert result.ok is False
    assert result.error_code == "permission_required"
    assert not (tmp_path / "nested").exists()


def test_write_file_creates_utf8_file_after_allow_once(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    requests: list[PermissionRequest] = []

    result = execute(
        registry,
        tmp_path,
        "write_file",
        {"path": "nested/demo.txt", "content": "你好\n"},
        allow_once(requests),
    )

    assert result.ok is True
    assert (tmp_path / "nested" / "demo.txt").read_bytes() == "你好\n".encode()
    assert len(requests) == 1
    assert requests[0].risk is RiskLevel.MEDIUM
    assert requests[0].scope == f"file:{(tmp_path / 'nested' / 'demo.txt').resolve()}"


def test_write_file_overwrite_has_high_risk_and_bounded_diff(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("old\n" + "x" * 20_000, encoding="utf-8")
    requests: list[PermissionRequest] = []

    result = execute(
        registry,
        tmp_path,
        "write_file",
        {"path": "demo.txt", "content": "new\n" + "y" * 20_000},
        allow_once(requests),
    )

    assert result.ok is True
    assert requests[0].risk is RiskLevel.HIGH
    preview = "\n".join(requests[0].details)
    assert "--- a/demo.txt" in preview
    assert "+++ b/demo.txt" in preview
    assert "preview truncated" in preview
    assert len(preview) <= 12_500


def test_write_file_noop_does_not_prompt(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("same", encoding="utf-8")
    permissions = PermissionManager(
        prompt=lambda _request: pytest.fail("no-op must not prompt")
    )

    result = execute(
        registry,
        tmp_path,
        "write_file",
        {"path": "demo.txt", "content": "same"},
        permissions,
    )

    assert result.ok is True
    assert "No changes" in result.output


def test_write_file_denial_preserves_existing_file(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before", encoding="utf-8")
    permissions = PermissionManager(prompt=lambda _request: "deny")

    result = execute(
        registry,
        tmp_path,
        "write_file",
        {"path": "demo.txt", "content": "after"},
        permissions,
    )

    assert result.ok is False
    assert result.error_code == "permission_denied"
    assert target.read_text(encoding="utf-8") == "before"


def test_write_file_atomic_replace_failure_preserves_original_and_cleans_temp(
    tmp_path: Path,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before", encoding="utf-8")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(file_changes.os, "replace", fail_replace)

    result = execute(
        registry,
        tmp_path,
        "write_file",
        {"path": "demo.txt", "content": "after"},
        allow_once(),
    )

    assert result.ok is False
    assert result.error_code == "write_error"
    assert target.read_text(encoding="utf-8") == "before"
    assert not list(tmp_path.glob(".demo.txt.*.tmp"))


def test_write_file_session_permission_is_scoped_to_exact_file(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    prompts: list[PermissionRequest] = []
    permissions = PermissionManager(
        prompt=lambda request: prompts.append(request) or "allow_session"
    )

    execute(
        registry,
        tmp_path,
        "write_file",
        {"path": "first.txt", "content": "one"},
        permissions,
    )
    execute(
        registry,
        tmp_path,
        "write_file",
        {"path": "first.txt", "content": "two"},
        permissions,
    )
    execute(
        registry,
        tmp_path,
        "write_file",
        {"path": "second.txt", "content": "other"},
        permissions,
    )

    assert len(prompts) == 2


def test_git_metadata_write_is_critical_risk(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    (tmp_path / ".git").mkdir()
    requests: list[PermissionRequest] = []

    result = execute(
        registry,
        tmp_path,
        "write_file",
        {"path": ".git/config", "content": "[core]\n"},
        allow_once(requests),
    )

    assert result.ok is True
    assert requests[0].risk is RiskLevel.CRITICAL


@pytest.mark.parametrize("path", ["../outside.txt"])
def test_write_file_rejects_path_escape_before_prompt(
    tmp_path: Path, registry: ToolRegistry, path: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    permissions = PermissionManager(
        prompt=lambda _request: pytest.fail("escaped path must not prompt")
    )

    result = execute(
        registry,
        workspace,
        "write_file",
        {"path": path, "content": "secret"},
        permissions,
    )

    assert result.ok is False
    assert result.error_code == "path_outside_workspace"
    assert not (tmp_path / "outside.txt").exists()


def test_edit_file_replaces_unique_match(tmp_path: Path, registry: ToolRegistry) -> None:
    target = tmp_path / "demo.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = execute(
        registry,
        tmp_path,
        "edit_file",
        {"path": "demo.py", "old": "value = 1", "new": "value = 2"},
        allow_once(),
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_edit_file_rejects_ambiguous_match_without_prompt(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    permissions = PermissionManager(
        prompt=lambda _request: pytest.fail("ambiguous edit must not prompt")
    )

    result = execute(
        registry,
        tmp_path,
        "edit_file",
        {"path": "demo.txt", "old": "same", "new": "changed"},
        permissions,
    )

    assert result.ok is False
    assert result.error_code == "ambiguous_match"
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


def test_edit_file_replace_all_is_explicit(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("same\nsame\n", encoding="utf-8")

    result = execute(
        registry,
        tmp_path,
        "edit_file",
        {
            "path": "demo.txt",
            "old": "same",
            "new": "changed",
            "replace_all": True,
        },
        allow_once(),
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "changed\nchanged\n"


def test_edit_file_reports_missing_file_without_prompt(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    permissions = PermissionManager(
        prompt=lambda _request: pytest.fail("missing file must not prompt")
    )

    result = execute(
        registry,
        tmp_path,
        "edit_file",
        {"path": "missing.txt", "old": "a", "new": "b"},
        permissions,
    )

    assert result.ok is False
    assert result.error_code == "path_not_found"


def test_patch_file_applies_all_replacements_with_one_permission(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    requests: list[PermissionRequest] = []

    result = execute(
        registry,
        tmp_path,
        "patch_file",
        {
            "path": "demo.txt",
            "replacements": [
                {"search": "beta", "replace": "BETA"},
                {"search": "alpha", "replace": "A", "replace_all": True},
            ],
        },
        allow_once(requests),
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "A\nBETA\nA\n"
    assert len(requests) == 1


def test_patch_file_is_transactional_when_later_replacement_fails(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    permissions = PermissionManager(
        prompt=lambda _request: pytest.fail("invalid patch must not prompt")
    )

    result = execute(
        registry,
        tmp_path,
        "patch_file",
        {
            "path": "demo.txt",
            "replacements": [
                {"search": "alpha", "replace": "A"},
                {"search": "missing", "replace": "M"},
            ],
        },
        permissions,
    )

    assert result.ok is False
    assert result.error_code == "replacement_not_found"
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_write_file_schema_limits_content_size(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    result = execute(
        registry,
        tmp_path,
        "write_file",
        {"path": "large.txt", "content": "x" * 1_000_001},
        allow_once(),
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
