"""Workspace-local discovery and on-demand loading of SKILL.md files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from minicode_rebuild.workspace import WorkspacePathError, resolve_workspace_path

SKILL_ROOT = ".minicode/skills"
MAX_SKILLS = 100
MAX_SKILL_BYTES = 128 * 1024
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SkillError(RuntimeError):
    """Base class for safe, user-visible skill failures."""


class SkillNotFoundError(SkillError):
    """Raised when a requested skill was not discovered."""


class SkillFormatError(SkillError):
    """Raised when a SKILL.md cannot be safely parsed."""


@dataclass(frozen=True, slots=True)
class SkillSummary:
    name: str
    description: str
    path: str


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    name: str
    description: str
    path: str
    content: str


def _frontmatter(markdown: str) -> dict[str, str]:
    normalized = markdown.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}
    end = normalized.find("\n---\n", 4)
    if end < 0:
        return {}
    values: dict[str, str] = {}
    for line in normalized[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            values[key.strip()] = value.strip().strip("\"'")
    return values


def _description(markdown: str) -> str:
    metadata = _frontmatter(markdown)
    if metadata.get("description"):
        return metadata["description"][:500]
    normalized = markdown.replace("\r\n", "\n")
    if normalized.startswith("---\n"):
        end = normalized.find("\n---\n", 4)
        if end >= 0:
            normalized = normalized[end + 5 :]
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        return line.replace("`", "")[:500]
    return "No description provided."


class SkillCatalog:
    """Discover only direct child skills under one workspace-owned root."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise SkillFormatError("Skill workspace must be a directory")
        try:
            self.root = resolve_workspace_path(self.workspace, SKILL_ROOT)
        except WorkspacePathError as exc:
            raise SkillFormatError("Skill root resolves outside workspace") from exc

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name.strip()):
            raise SkillFormatError("Skill name must be a safe 1-64 character slug")
        try:
            target = resolve_workspace_path(
                self.workspace, f"{SKILL_ROOT}/{name.strip()}/SKILL.md"
            )
            target.relative_to(self.root)
        except (WorkspacePathError, ValueError) as exc:
            raise SkillFormatError("Skill path resolves outside skill root") from exc
        return target

    @staticmethod
    def _read(path: Path) -> str:
        try:
            if not path.is_file():
                raise SkillNotFoundError("SKILL.md does not exist")
            if path.stat().st_size > MAX_SKILL_BYTES:
                raise SkillFormatError(
                    f"SKILL.md exceeds the {MAX_SKILL_BYTES}-byte limit"
                )
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillFormatError("SKILL.md must be UTF-8 text") from exc
        except OSError as exc:
            raise SkillFormatError("SKILL.md could not be read") from exc

    def discover(self) -> tuple[SkillSummary, ...]:
        """Return bounded metadata without retaining full skill content."""

        try:
            entries = sorted(
                self.root.iterdir(), key=lambda item: (item.name.casefold(), item.name)
            )
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise SkillFormatError("Skill root could not be scanned") from exc
        summaries: list[SkillSummary] = []
        for entry in entries[:MAX_SKILLS]:
            if not _SKILL_NAME.fullmatch(entry.name):
                continue
            try:
                path = self._path(entry.name)
                content = self._read(path)
            except SkillError:
                continue
            metadata = _frontmatter(content)
            summaries.append(
                SkillSummary(
                    name=entry.name,
                    description=_description(content),
                    path=path.relative_to(self.workspace).as_posix(),
                )
            )
            if metadata.get("name") and metadata["name"] != entry.name:
                summaries.pop()
        return tuple(summaries)

    def load(self, name: str) -> LoadedSkill:
        """Load one explicitly selected skill after validating its metadata."""

        normalized = name.strip()
        path = self._path(normalized)
        content = self._read(path)
        metadata = _frontmatter(content)
        if metadata.get("name") and metadata["name"] != normalized:
            raise SkillFormatError("SKILL.md name must match its directory")
        return LoadedSkill(
            name=normalized,
            description=_description(content),
            path=path.relative_to(self.workspace).as_posix(),
            content=content,
        )

    def prompt_summary(self) -> str:
        """Build the bounded catalog injected into the system prompt."""

        skills = self.discover()
        if not skills:
            return ""
        lines = [
            "[Available skills]",
            "Skills are local instructions. Load one with load_skill only when relevant; do not guess its contents.",
        ]
        lines.extend(f"- {skill.name}: {skill.description}" for skill in skills)
        return "\n".join(lines)


__all__ = [
    "LoadedSkill",
    "MAX_SKILL_BYTES",
    "MAX_SKILLS",
    "SKILL_ROOT",
    "SkillCatalog",
    "SkillError",
    "SkillFormatError",
    "SkillNotFoundError",
    "SkillSummary",
]
