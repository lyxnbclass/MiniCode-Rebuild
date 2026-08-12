"""Review and atomically apply one complete UTF-8 file revision."""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
from pathlib import Path

from minicode_rebuild.permissions import (
    PermissionDeniedError,
    PermissionManager,
    PermissionRequest,
    classify_file_risk,
)
from minicode_rebuild.tooling import ToolContext, ToolResult

MAX_DIFF_PREVIEW = 12_000


def _relative(target: Path, context: ToolContext) -> str:
    return target.relative_to(context.cwd.resolve()).as_posix()


def read_existing_text(target: Path) -> tuple[str | None, ToolResult | None]:
    try:
        if not target.exists():
            return None, None
        if not target.is_file():
            return None, ToolResult.error("not_a_file", "The path is not a file.")
        return target.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, ToolResult.error("invalid_utf8", "The file is not valid UTF-8 text.")
    except OSError:
        return None, ToolResult.error("read_error", "The file could not be read.")


def _diff_preview(relative: str, old: str, new: str) -> str:
    preview = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
    )
    if len(preview) <= MAX_DIFF_PREVIEW:
        return preview
    marker = "\n... [diff preview truncated] ...\n"
    available = MAX_DIFF_PREVIEW - len(marker)
    return preview[: available // 2] + marker + preview[-(available - available // 2) :]


def apply_file_change(
    target: Path,
    next_content: str,
    context: ToolContext,
    *,
    operation: str,
) -> ToolResult:
    old_content, error = read_existing_text(target)
    if error is not None:
        return error
    existed = old_content is not None
    previous = old_content or ""
    if existed and previous == next_content:
        return ToolResult.success("No changes were needed.")

    assessment = classify_file_risk(target, existed=existed)
    relative = _relative(target, context)
    request = PermissionRequest(
        operation=operation,
        risk=assessment.level,
        summary=("Overwrite" if existed else "Create") + f" {relative}",
        scope=f"file:{target}",
        details=(assessment.reason, _diff_preview(relative, previous, next_content)),
    )
    manager = context.permissions or PermissionManager()
    try:
        manager.authorize(request)
    except PermissionDeniedError as exc:
        return ToolResult.error(exc.error_code, str(exc))

    temporary_path: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary_path = Path(raw_path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(next_content)
            stream.flush()
            os.fsync(stream.fileno())
        if existed:
            os.chmod(temporary_path, stat.S_IMODE(target.stat().st_mode))
        os.replace(temporary_path, target)
        temporary_path = None
    except OSError as exc:
        return ToolResult.error("write_error", f"The file could not be written: {exc}")
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return ToolResult.success(f"Updated {relative}.")


__all__ = ["MAX_DIFF_PREVIEW", "apply_file_change", "read_existing_text"]
