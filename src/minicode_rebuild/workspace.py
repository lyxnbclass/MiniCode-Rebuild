"""Workspace path resolution shared by local tools."""

from __future__ import annotations

from os import PathLike
from pathlib import Path


class WorkspacePathError(ValueError):
    """Raised when a tool path cannot be safely resolved in its workspace."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def resolve_workspace_path(
    workspace_root: str | PathLike[str], input_path: str | PathLike[str]
) -> Path:
    """Resolve one untrusted path and require its real target to stay in root."""

    try:
        root = Path(workspace_root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise WorkspacePathError(
            "invalid_workspace", "The workspace root cannot be resolved."
        ) from exc
    if not root.is_dir():
        raise WorkspacePathError(
            "invalid_workspace", "The workspace root must be a directory."
        )

    try:
        candidate = Path(input_path)
    except (TypeError, ValueError) as exc:
        raise WorkspacePathError("invalid_path", "The path is invalid.") from exc

    target = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspacePathError("invalid_path", "The path cannot be resolved.") from exc

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError(
            "path_outside_workspace", "The path is outside workspace."
        ) from exc
    return resolved


__all__ = ["WorkspacePathError", "resolve_workspace_path"]
