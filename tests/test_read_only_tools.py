from __future__ import annotations

from pathlib import Path

import pytest

from minicode_rebuild.tooling import ToolContext, ToolRegistry, ToolResult
from minicode_rebuild.tools import READ_ONLY_TOOLS
from minicode_rebuild.tools import read_only as read_only_module


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(READ_ONLY_TOOLS)


def execute(
    registry: ToolRegistry,
    workspace: Path,
    name: str,
    arguments: dict,
) -> ToolResult:
    return registry.execute(name, arguments, ToolContext(workspace))


def test_read_only_tools_export_stable_model_declarations(
    registry: ToolRegistry,
) -> None:
    assert [tool.name for tool in registry.model_tools()] == [
        "read_file",
        "list_files",
        "glob_search",
        "grep_files",
    ]
    assert all(
        tool.parameters.get("additionalProperties") is False
        for tool in registry.model_tools()
    )


def test_read_file_returns_bounded_window_with_resume_offset(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    (tmp_path / "notes.txt").write_text("0123456789", encoding="utf-8")

    result = execute(
        registry,
        tmp_path,
        "read_file",
        {"path": "notes.txt", "offset": 2, "limit": 4},
    )

    assert result.ok is True
    assert "FILE: notes.txt" in result.output
    assert "OFFSET: 2" in result.output
    assert "END: 6" in result.output
    assert "TRUNCATED: yes" in result.output
    assert "NEXT_OFFSET: 6" in result.output
    assert result.output.endswith("2345")


def test_read_file_reports_end_of_file_without_loading_unbounded_output(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    (tmp_path / "large.txt").write_text("x" * 20_000, encoding="utf-8")

    result = execute(
        registry,
        tmp_path,
        "read_file",
        {"path": "large.txt", "limit": 100},
    )

    assert result.ok is True
    assert "TRUNCATED: yes" in result.output
    assert result.output.endswith("x" * 100)
    assert len(result.output) < 500


@pytest.mark.parametrize(
    ("path", "error_code"),
    [
        ("missing.txt", "path_not_found"),
        ("folder", "not_a_file"),
        ("binary.dat", "invalid_utf8"),
    ],
)
def test_read_file_returns_specific_domain_errors(
    tmp_path: Path,
    registry: ToolRegistry,
    path: str,
    error_code: str,
) -> None:
    (tmp_path / "folder").mkdir()
    (tmp_path / "binary.dat").write_bytes(b"\xff\xfe\x00")

    result = execute(registry, tmp_path, "read_file", {"path": path})

    assert result.ok is False
    assert result.error_code == error_code


def test_list_files_is_sorted_and_uses_workspace_relative_paths(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Zulu.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "alpha.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "package").mkdir()

    result = execute(registry, tmp_path, "list_files", {"path": "src"})

    assert result.ok is True
    assert result.output.splitlines()[:3] == [
        "file src/alpha.py",
        "dir src/package/",
        "file src/Zulu.py",
    ]
    assert "SHOWN: 3" in result.output
    assert "TRUNCATED: no" in result.output


def test_list_files_limits_results_and_rejects_file_target(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    for index in range(4):
        (tmp_path / f"{index}.txt").write_text("", encoding="utf-8")

    limited = execute(registry, tmp_path, "list_files", {"limit": 2})
    wrong_type = execute(
        registry, tmp_path, "list_files", {"path": "0.txt"}
    )

    assert limited.ok is True
    assert "SHOWN: 2" in limited.output
    assert "TRUNCATED: yes" in limited.output
    assert wrong_type.ok is False
    assert wrong_type.error_code == "not_a_directory"


def test_glob_search_finds_files_and_skips_nuisance_directories(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "main.txt").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hidden.py").write_text("", encoding="utf-8")

    result = execute(
        registry,
        tmp_path,
        "glob_search",
        {"pattern": "**/*.py"},
    )

    assert result.ok is True
    assert "src/main.py" in result.output
    assert "src/main.txt" not in result.output
    assert ".git/hidden.py" not in result.output
    assert "MATCHES: 1" in result.output


@pytest.mark.parametrize("pattern", ["../*.txt", "nested/../../*.txt"])
def test_glob_search_rejects_parent_traversal_pattern(
    tmp_path: Path, registry: ToolRegistry, pattern: str
) -> None:
    result = execute(
        registry, tmp_path, "glob_search", {"pattern": pattern}
    )

    assert result.ok is False
    assert result.error_code == "invalid_glob"


def test_glob_search_rejects_absolute_pattern_and_limits_results(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    for index in range(4):
        (tmp_path / f"{index}.py").write_text("", encoding="utf-8")

    invalid = execute(
        registry,
        tmp_path,
        "glob_search",
        {"pattern": str(tmp_path / "*.py")},
    )
    limited = execute(
        registry,
        tmp_path,
        "glob_search",
        {"pattern": "*.py", "limit": 2},
    )

    assert invalid.ok is False
    assert invalid.error_code == "invalid_glob"
    assert limited.ok is True
    assert "MATCHES: 2" in limited.output
    assert "TRUNCATED: yes" in limited.output


def test_glob_search_does_not_expose_symlink_target_outside_workspace(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "linked.txt"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    result = execute(
        registry, workspace, "glob_search", {"pattern": "*.txt"}
    )

    assert result.ok is True
    assert "linked.txt" not in result.output
    assert "secret" not in result.output


def test_grep_files_supports_regex_include_and_case_control(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "Needle one\nplain\nneedle two\n", encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text("needle three\n", encoding="utf-8")

    insensitive = execute(
        registry,
        tmp_path,
        "grep_files",
        {"pattern": "needle\\s+(one|two)", "include": "**/*.py"},
    )
    sensitive = execute(
        registry,
        tmp_path,
        "grep_files",
        {
            "pattern": "needle\\s+one",
            "include": "**/*.py",
            "case_sensitive": True,
        },
    )

    assert insensitive.ok is True
    assert "src/main.py:1:Needle one" in insensitive.output
    assert "src/main.py:3:needle two" in insensitive.output
    assert "notes.txt" not in insensitive.output
    assert "MATCHES: 2" in insensitive.output
    assert sensitive.ok is True
    assert "No matches found." in sensitive.output


def test_grep_files_rejects_invalid_regex_and_limits_matches(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    (tmp_path / "many.txt").write_text(
        "\n".join(f"needle {index}" for index in range(5)), encoding="utf-8"
    )

    invalid = execute(
        registry, tmp_path, "grep_files", {"pattern": "["}
    )
    limited = execute(
        registry,
        tmp_path,
        "grep_files",
        {"pattern": "needle", "limit": 2},
    )

    assert invalid.ok is False
    assert invalid.error_code == "invalid_regex"
    assert limited.ok is True
    assert "MATCHES: 2" in limited.output
    assert "TRUNCATED: yes" in limited.output


def test_grep_files_caps_candidates_before_include_filter(
    tmp_path: Path, registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    yielded = 0

    def candidate_files(_root: Path):
        nonlocal yielded
        for index in range(5):
            yielded += 1
            yield tmp_path / f"excluded-{index}.txt"

    monkeypatch.setattr(read_only_module, "MAX_GREP_FILES", 3)
    monkeypatch.setattr(read_only_module, "_iter_search_files", candidate_files)

    result = execute(
        registry,
        tmp_path,
        "grep_files",
        {"pattern": "needle", "include": "**/*.py"},
    )

    assert result.ok is True
    assert yielded == 4
    assert "FILES_SCANNED: 3" in result.output
    assert "TRUNCATED: yes" in result.output


def test_grep_files_skips_large_and_non_utf8_files(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    (tmp_path / "large.txt").write_text(
        "needle" + "x" * 1_100_000, encoding="utf-8"
    )
    (tmp_path / "binary.txt").write_bytes(b"\xffneedle")

    result = execute(
        registry, tmp_path, "grep_files", {"pattern": "needle"}
    )

    assert result.ok is True
    assert "No matches found." in result.output
    assert "SKIPPED_LARGE: 1" in result.output
    assert "SKIPPED_NON_TEXT: 1" in result.output


def test_grep_files_truncates_long_lines_and_registry_caps_total_output(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    lines = [f"needle-{index}-" + "x" * 700 for index in range(50)]
    (tmp_path / "long.txt").write_text("\n".join(lines), encoding="utf-8")

    result = execute(
        registry,
        tmp_path,
        "grep_files",
        {"pattern": "needle", "limit": 50},
    )

    assert result.ok is True
    assert "[line truncated]" in result.output
    assert result.truncated is True
    assert result.original_length is not None
    assert len(result.output) == 20_000


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("read_file", {"path": "../secret.txt"}),
        ("list_files", {"path": "../outside"}),
        ("glob_search", {"path": "../outside", "pattern": "**/*"}),
        ("grep_files", {"path": "../outside", "pattern": "secret"}),
    ],
)
def test_all_read_only_tools_reject_parent_path_escape(
    tmp_path: Path,
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    result = execute(registry, workspace, tool_name, arguments)

    assert result.ok is False
    assert result.error_code == "path_outside_workspace"
    assert "secret" not in result.output


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("read_file", {"path": "placeholder"}),
        ("list_files", {"path": "placeholder"}),
        ("glob_search", {"path": "placeholder", "pattern": "**/*"}),
        ("grep_files", {"path": "placeholder", "pattern": "secret"}),
    ],
)
def test_all_read_only_tools_reject_absolute_path_escape(
    tmp_path: Path,
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    arguments = {**arguments, "path": str(outside / "secret.txt")}
    if tool_name in {"list_files", "glob_search", "grep_files"}:
        arguments["path"] = str(outside)

    result = execute(registry, workspace, tool_name, arguments)

    assert result.ok is False
    assert result.error_code == "path_outside_workspace"
    assert "secret" not in result.output


def test_registry_rejects_unexpected_read_only_tool_arguments(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    result = execute(
        registry,
        tmp_path,
        "read_file",
        {"path": "notes.txt", "unexpected": True},
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("read_file", {"path": "inside.txt"}),
        ("list_files", {"path": "."}),
        ("glob_search", {"path": ".", "pattern": "*.txt"}),
        ("grep_files", {"path": ".", "pattern": "inside"}),
    ],
)
def test_all_read_only_tools_allow_absolute_paths_inside_workspace(
    tmp_path: Path,
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict,
) -> None:
    target = tmp_path / "inside.txt"
    target.write_text("inside", encoding="utf-8")
    absolute_target = target if tool_name == "read_file" else tmp_path
    arguments = {**arguments, "path": str(absolute_target)}

    result = execute(registry, tmp_path, tool_name, arguments)

    assert result.ok is True
    assert "inside" in result.output


@pytest.mark.parametrize("tool_name", ["glob_search", "grep_files"])
@pytest.mark.parametrize(
    ("path", "error_code"),
    [("missing", "path_not_found"), ("file.txt", "not_a_directory")],
)
def test_search_tools_report_missing_and_non_directory_roots(
    tmp_path: Path,
    registry: ToolRegistry,
    tool_name: str,
    path: str,
    error_code: str,
) -> None:
    (tmp_path / "file.txt").write_text("content", encoding="utf-8")
    arguments = {"path": path, "pattern": ".*"}

    result = execute(registry, tmp_path, tool_name, arguments)

    assert result.ok is False
    assert result.error_code == error_code


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("read_file", {"path": "notes.txt", "limit": 16_001}),
        ("list_files", {"limit": 1_001}),
        ("glob_search", {"pattern": "*", "limit": 501}),
        ("grep_files", {"pattern": ".", "limit": 501}),
    ],
)
def test_registry_rejects_read_only_limits_above_documented_maximum(
    tmp_path: Path,
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict,
) -> None:
    result = execute(registry, tmp_path, tool_name, arguments)

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
