"""Offline provider and runtime readiness checks with no network requests."""

from __future__ import annotations

import platform
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from minicode_rebuild.config import (
    ModelConfigurationError,
    ModelSettings,
    RuntimeSettings,
)
from minicode_rebuild.session import SessionStore
from minicode_rebuild.skills import SkillCatalog, SkillError


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    ready: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.ready for check in self.checks)


def check_readiness(
    workspace: Path,
    environment: Mapping[str, str],
    *,
    require_provider: bool = True,
) -> ReadinessReport:
    """Validate local configuration without contacting a provider."""

    checks: list[ReadinessCheck] = [
        ReadinessCheck(
            "python",
            sys.version_info >= (3, 11),
            f"{platform.python_implementation()} {platform.python_version()}",
        )
    ]
    try:
        RuntimeSettings.from_env(environment)
        checks.append(ReadinessCheck("runtime-config", True, "valid"))
    except ModelConfigurationError as exc:
        checks.append(ReadinessCheck("runtime-config", False, str(exc)))
    if require_provider:
        try:
            settings = ModelSettings.from_env(environment)
            checks.append(
                ReadinessCheck(
                    "provider-config",
                    True,
                    f"model={settings.model} endpoint={settings.chat_completions_url}",
                )
            )
        except ModelConfigurationError as exc:
            checks.append(ReadinessCheck("provider-config", False, str(exc)))
    else:
        checks.append(ReadinessCheck("provider-config", True, "not required for demo"))
    try:
        SessionStore(workspace)
        checks.append(ReadinessCheck("session-store", True, "workspace-local"))
    except Exception as exc:
        checks.append(ReadinessCheck("session-store", False, f"{type(exc).__name__}: {exc}"))
    try:
        count = len(SkillCatalog(workspace).discover())
        checks.append(ReadinessCheck("skills", True, f"{count} discovered"))
    except SkillError as exc:
        checks.append(ReadinessCheck("skills", False, str(exc)))
    return ReadinessReport(tuple(checks))


def format_readiness(report: ReadinessReport) -> str:
    lines = [f"Readiness: {'ready' if report.ready else 'not ready'}"]
    for check in report.checks:
        lines.append(f"[{'ok' if check.ready else 'fail'}] {check.name}: {check.detail}")
    return "\n".join(lines)


__all__ = [
    "ReadinessCheck",
    "ReadinessReport",
    "check_readiness",
    "format_readiness",
]
