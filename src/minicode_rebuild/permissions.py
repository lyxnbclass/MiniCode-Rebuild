"""Fail-closed permission decisions and operation risk classification."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PermissionDecision(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    level: RiskLevel
    reason: str


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    operation: str
    risk: RiskLevel
    summary: str
    scope: str
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("operation", "summary", "scope"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if not isinstance(self.risk, RiskLevel):
            raise TypeError("risk must be a RiskLevel")
        if not isinstance(self.details, tuple) or any(
            not isinstance(item, str) for item in self.details
        ):
            raise TypeError("details must be a tuple of strings")


class PermissionDeniedError(PermissionError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


PermissionPrompt = Callable[
    [PermissionRequest], PermissionDecision | str
]


class PermissionManager:
    """Authorize exact operation scopes, defaulting to denial."""

    def __init__(self, prompt: PermissionPrompt | None = None) -> None:
        self._prompt = prompt
        self._session_scopes: set[str] = set()

    def authorize(self, request: PermissionRequest) -> None:
        if request.scope in self._session_scopes:
            return
        if self._prompt is None:
            raise PermissionDeniedError(
                "permission_required", "Explicit permission is required."
            )
        try:
            raw_decision = self._prompt(request)
        except Exception as exc:
            raise PermissionDeniedError(
                "permission_denied", "Permission decision failed."
            ) from exc
        try:
            decision = PermissionDecision(raw_decision)
        except (TypeError, ValueError):
            decision = PermissionDecision.DENY
        if decision is PermissionDecision.ALLOW_ONCE:
            return
        if decision is PermissionDecision.ALLOW_SESSION:
            self._session_scopes.add(request.scope)
            return
        raise PermissionDeniedError("permission_denied", "Permission was denied.")


_INTERPRETERS = {
    "bash",
    "cmd",
    "cmd.exe",
    "node",
    "perl",
    "powershell",
    "powershell.exe",
    "py",
    "pwsh",
    "python",
    "python3",
    "ruby",
    "sh",
    "wscript",
    "cscript",
}

_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com")


def _normalized_command_name(command: str) -> str:
    name = command.casefold()
    for suffix in _WINDOWS_EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def classify_command_risk(
    command: str, arguments: Sequence[str]
) -> RiskAssessment:
    name = _normalized_command_name(command)
    lowered = [argument.casefold() for argument in arguments]
    if name in _INTERPRETERS:
        return RiskAssessment(
            RiskLevel.CRITICAL,
            "Interpreter and shell commands can execute arbitrary code.",
        )
    if name in {"rm", "rmdir", "del", "erase"} and any(
        item in {"-r", "-rf", "-fr", "/s"} for item in lowered
    ):
        return RiskAssessment(
            RiskLevel.CRITICAL, "recursive deletion can remove large file trees."
        )
    if name == "git":
        if lowered[:2] == ["reset", "--hard"] or (
            lowered[:1] == ["clean"] and any("f" in item for item in lowered[1:])
        ):
            return RiskAssessment(
                RiskLevel.CRITICAL,
                "This Git operation can discard uncommitted work.",
            )
        if lowered[:1] == ["push"] and any(
            item in {"--force", "-f", "--force-with-lease"} for item in lowered[1:]
        ):
            return RiskAssessment(
                RiskLevel.CRITICAL,
                "Forced push can rewrite shared history.",
            )
        if lowered[:1] and lowered[0] in {
            "status",
            "diff",
            "log",
            "show",
            "branch",
            "rev-parse",
        }:
            return RiskAssessment(
                RiskLevel.MEDIUM, "Known read-only Git command."
            )
        return RiskAssessment(
            RiskLevel.HIGH, "Git command may modify repository state."
        )
    if name in {"dir", "ls", "rg", "grep", "find", "findstr"}:
        return RiskAssessment(RiskLevel.MEDIUM, "Known read-only command.")
    return RiskAssessment(
        RiskLevel.HIGH, "The effects of this unknown command are not known."
    )


def classify_file_risk(target: Path, *, existed: bool) -> RiskAssessment:
    if any(part.casefold() == ".git" for part in target.parts):
        return RiskAssessment(
            RiskLevel.CRITICAL, "Writing Git metadata can corrupt repository state."
        )
    if existed:
        return RiskAssessment(
            RiskLevel.HIGH, "Overwriting an existing file can discard content."
        )
    return RiskAssessment(RiskLevel.MEDIUM, "Creating a new workspace file.")


__all__ = [
    "PermissionDecision",
    "PermissionDeniedError",
    "PermissionManager",
    "PermissionRequest",
    "RiskAssessment",
    "RiskLevel",
    "classify_command_risk",
    "classify_file_risk",
]
