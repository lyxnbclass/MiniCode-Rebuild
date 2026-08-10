from __future__ import annotations

from pathlib import Path

import pytest

from minicode_rebuild.workspace import WorkspacePathError, resolve_workspace_path


def test_resolve_workspace_path_normalizes_relative_path(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"

    resolved = resolve_workspace_path(tmp_path, "src/../notes.txt")

    assert resolved == target.resolve()


def test_resolve_workspace_path_allows_absolute_path_inside_workspace(
    tmp_path: Path,
) -> None:
    target = tmp_path / "src" / "main.py"

    resolved = resolve_workspace_path(tmp_path, target)

    assert resolved == target.resolve()


@pytest.mark.parametrize("input_path", ["../secret.txt", "nested/../../secret.txt"])
def test_resolve_workspace_path_rejects_parent_escape(
    tmp_path: Path, input_path: str
) -> None:
    with pytest.raises(WorkspacePathError, match="outside workspace"):
        resolve_workspace_path(tmp_path, input_path)


def test_resolve_workspace_path_rejects_absolute_path_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "secret.txt"
    workspace.mkdir()

    with pytest.raises(WorkspacePathError, match="outside workspace"):
        resolve_workspace_path(workspace, outside)


def test_resolve_workspace_path_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(WorkspacePathError, match="outside workspace"):
        resolve_workspace_path(workspace, "linked/secret.txt")


def test_resolve_workspace_path_requires_directory_workspace(tmp_path: Path) -> None:
    workspace_file = tmp_path / "workspace.txt"
    workspace_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(WorkspacePathError, match="workspace root"):
        resolve_workspace_path(workspace_file, ".")
