"""Workspace-local, bounded long-term memory storage and lexical retrieval."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from minicode_rebuild.workspace import WorkspacePathError, resolve_workspace_path

MEMORY_FILE = ".minicode-rebuild/memories.json"
MEMORY_SCHEMA_VERSION = 1
MAX_MEMORY_FILE_BYTES = 2_000_000
MAX_MEMORIES = 500
MAX_MEMORY_CONTENT = 2_000
MAX_MEMORY_TAGS = 8
MAX_MEMORY_TAG_LENGTH = 32
MAX_MEMORY_QUERY = 500
MAX_SEARCH_LIMIT = 20
MAX_MEMORY_PREVIEW = 500

_MEMORY_ID = re.compile(r"[0-9a-f]{32}")
_WORD = re.compile(r"\w+", re.UNICODE)


class MemoryStoreError(ValueError):
    """Raised when memory input or persisted state is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One user- or agent-approved workspace memory."""

    memory_id: str
    content: str
    tags: tuple[str, ...]
    created_at: str
    source_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    """A ranked memory result with a bounded display preview."""

    record: MemoryRecord
    score: int
    preview: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_content(content: str) -> str:
    if not isinstance(content, str):
        raise MemoryStoreError("Memory content must be text.")
    normalized = content.strip()
    if not normalized:
        raise MemoryStoreError("Memory content must not be empty.")
    if len(normalized) > MAX_MEMORY_CONTENT:
        raise MemoryStoreError(
            f"Memory content exceeds {MAX_MEMORY_CONTENT} characters."
        )
    return normalized


def _normalize_tags(tags: Sequence[str]) -> tuple[str, ...]:
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Sequence):
        raise MemoryStoreError("Memory tags must be a sequence of strings.")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        if not isinstance(raw_tag, str):
            raise MemoryStoreError("Memory tags must contain only strings.")
        tag = raw_tag.strip()
        if not tag:
            raise MemoryStoreError("Memory tags must not be empty.")
        if len(tag) > MAX_MEMORY_TAG_LENGTH:
            raise MemoryStoreError(
                f"Memory tags must not exceed {MAX_MEMORY_TAG_LENGTH} characters."
            )
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(tag)
    if len(normalized) > MAX_MEMORY_TAGS:
        raise MemoryStoreError(f"A memory may have at most {MAX_MEMORY_TAGS} tags.")
    return tuple(normalized)


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise MemoryStoreError("Memory search limit must be an integer.")
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise MemoryStoreError(
            f"Memory search limit must be between 1 and {MAX_SEARCH_LIMIT}."
        )
    return limit


def _preview(content: str) -> str:
    if len(content) <= MAX_MEMORY_PREVIEW:
        return content
    marker = "... [memory truncated]"
    return content[: MAX_MEMORY_PREVIEW - len(marker)].rstrip() + marker


class MemoryStore:
    """Persist and retrieve bounded memories for exactly one workspace."""

    def __init__(
        self,
        workspace: Path,
        *,
        max_records: int = MAX_MEMORIES,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        try:
            self.workspace = Path(workspace).resolve(strict=True)
        except OSError as exc:
            raise MemoryStoreError("Memory workspace cannot be resolved.") from exc
        if not self.workspace.is_dir():
            raise MemoryStoreError("Memory workspace must be a directory.")
        try:
            self.path = resolve_workspace_path(self.workspace, MEMORY_FILE)
        except WorkspacePathError as exc:
            if exc.error_code == "path_outside_workspace":
                raise MemoryStoreError(
                    "Memory storage path escapes the workspace."
                ) from exc
            raise MemoryStoreError("Memory workspace cannot be resolved.") from exc
        if isinstance(max_records, bool) or not isinstance(max_records, int):
            raise TypeError("max_records must be an integer")
        if not 1 <= max_records <= MAX_MEMORIES:
            raise ValueError(f"max_records must be between 1 and {MAX_MEMORIES}")
        self.max_records = max_records
        self._clock = clock
        self._id_factory = id_factory

    def _workspace_identity(self) -> str:
        return os.path.normcase(str(self.workspace))

    def _checked_path(self) -> Path:
        try:
            return resolve_workspace_path(self.workspace, MEMORY_FILE)
        except WorkspacePathError as exc:
            raise MemoryStoreError(
                "Memory storage path escapes the workspace."
            ) from exc

    def _decode_record(self, value: object) -> MemoryRecord:
        if not isinstance(value, dict):
            raise MemoryStoreError("Memory record must be an object.")
        memory_id = value.get("memory_id")
        content = value.get("content")
        tags = value.get("tags")
        created_at = value.get("created_at")
        source_session_id = value.get("source_session_id")
        if not isinstance(memory_id, str) or not _MEMORY_ID.fullmatch(memory_id):
            raise MemoryStoreError("Memory record has an invalid id.")
        normalized_content = _normalize_content(content) if isinstance(content, str) else None
        if normalized_content is None or normalized_content != content:
            raise MemoryStoreError("Memory record has invalid content.")
        if not isinstance(tags, list):
            raise MemoryStoreError("Memory record has invalid tags.")
        normalized_tags = _normalize_tags(tags)
        if list(normalized_tags) != tags:
            raise MemoryStoreError("Memory record has non-normalized tags.")
        if not isinstance(created_at, str):
            raise MemoryStoreError("Memory record has an invalid timestamp.")
        try:
            timestamp = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise MemoryStoreError("Memory record has an invalid timestamp.") from exc
        if timestamp.tzinfo is None:
            raise MemoryStoreError("Memory timestamp must include a timezone.")
        if source_session_id is not None and (
            not isinstance(source_session_id, str)
            or not source_session_id
            or len(source_session_id) > 128
        ):
            raise MemoryStoreError("Memory record has an invalid source session id.")
        return MemoryRecord(
            memory_id=memory_id,
            content=content,
            tags=normalized_tags,
            created_at=created_at,
            source_session_id=source_session_id,
        )

    def _load(self) -> list[MemoryRecord]:
        path = self._checked_path()
        if not path.exists():
            return []
        try:
            if not path.is_file():
                raise MemoryStoreError("Memory storage path is not a file.")
            if path.stat().st_size > MAX_MEMORY_FILE_BYTES:
                raise MemoryStoreError("Memory storage file is too large.")
            value = json.loads(path.read_text(encoding="utf-8"))
        except MemoryStoreError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MemoryStoreError("Memory storage file is unreadable or corrupt.") from exc
        if not isinstance(value, dict):
            raise MemoryStoreError("Memory storage root must be an object.")
        if value.get("schema_version") != MEMORY_SCHEMA_VERSION:
            raise MemoryStoreError("Unsupported or missing memory schema version.")
        if value.get("workspace") != self._workspace_identity():
            raise MemoryStoreError("Memory storage belongs to a different workspace.")
        records = value.get("memories")
        if not isinstance(records, list) or len(records) > self.max_records:
            raise MemoryStoreError("Memory storage contains an invalid record list.")
        decoded = [self._decode_record(item) for item in records]
        if len({item.memory_id for item in decoded}) != len(decoded):
            raise MemoryStoreError("Memory storage contains duplicate ids.")
        return decoded

    def _save(self, records: Sequence[MemoryRecord]) -> None:
        path = self._checked_path()
        payload = {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "workspace": self._workspace_identity(),
            "memories": [
                {
                    "memory_id": item.memory_id,
                    "content": item.content,
                    "tags": list(item.tags),
                    "created_at": item.created_at,
                    "source_session_id": item.source_session_id,
                }
                for item in records
            ],
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > MAX_MEMORY_FILE_BYTES:
            raise MemoryStoreError("Memory storage would exceed its size limit.")
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path = self._checked_path()
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temporary_path = Path(raw_path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError as exc:
            raise MemoryStoreError("Memory storage could not be written.") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def add(
        self,
        content: str,
        *,
        tags: Sequence[str] = (),
        source_session_id: str | None = None,
    ) -> MemoryRecord:
        """Append one validated memory without silently evicting old records."""

        normalized_content = _normalize_content(content)
        normalized_tags = _normalize_tags(tags)
        if source_session_id is not None and (
            not isinstance(source_session_id, str)
            or not source_session_id.strip()
            or len(source_session_id) > 128
        ):
            raise MemoryStoreError("Source session id is invalid.")
        records = self._load()
        if len(records) >= self.max_records:
            raise MemoryStoreError(
                f"Memory capacity of {self.max_records} records has been reached."
            )
        memory_id = self._id_factory()
        if not isinstance(memory_id, str) or not _MEMORY_ID.fullmatch(memory_id):
            raise MemoryStoreError("Memory id factory returned an invalid id.")
        if any(item.memory_id == memory_id for item in records):
            raise MemoryStoreError("Memory id already exists.")
        created_at = self._clock().astimezone(UTC).isoformat()
        record = MemoryRecord(
            memory_id=memory_id,
            content=normalized_content,
            tags=normalized_tags,
            created_at=created_at,
            source_session_id=(
                source_session_id.strip() if source_session_id is not None else None
            ),
        )
        self._save((*records, record))
        return record

    def list(self, *, limit: int = 20) -> tuple[MemoryRecord, ...]:
        """Return newest records first with a bounded result count."""

        checked_limit = _validate_limit(limit)
        return tuple(reversed(self._load()))[:checked_limit]

    def get(self, memory_id: str) -> MemoryRecord:
        """Load one memory by its opaque id."""

        if not isinstance(memory_id, str) or not _MEMORY_ID.fullmatch(memory_id):
            raise MemoryStoreError("Memory id is invalid.")
        for record in self._load():
            if record.memory_id == memory_id:
                return record
        raise MemoryStoreError(f"Memory not found: {memory_id}")

    def delete(self, memory_id: str) -> MemoryRecord:
        """Delete exactly one existing memory and return its previous value."""

        target = self.get(memory_id)
        remaining = [
            item for item in self._load() if item.memory_id != target.memory_id
        ]
        self._save(remaining)
        return target

    def search(self, query: str, *, limit: int = 5) -> tuple[MemorySearchResult, ...]:
        """Rank records using deterministic phrase and token overlap scoring."""

        if not isinstance(query, str):
            raise MemoryStoreError("Memory query must be text.")
        normalized_query = query.strip().casefold()
        if not normalized_query:
            raise MemoryStoreError("Memory query must not be empty.")
        if len(normalized_query) > MAX_MEMORY_QUERY:
            raise MemoryStoreError(
                f"Memory query exceeds {MAX_MEMORY_QUERY} characters."
            )
        checked_limit = _validate_limit(limit)
        query_terms = set(_WORD.findall(normalized_query))
        ranked: list[MemorySearchResult] = []
        for record in self._load():
            normalized_content = record.content.casefold()
            normalized_tags = tuple(tag.casefold() for tag in record.tags)
            searchable = " ".join((normalized_content, *normalized_tags))
            searchable_terms = set(_WORD.findall(searchable))
            overlap = len(query_terms & searchable_terms)
            phrase_match = normalized_query in searchable
            tag_match = normalized_query in normalized_tags
            if not phrase_match and overlap == 0:
                continue
            score = overlap * 10 + int(phrase_match) * 100 + int(tag_match) * 25
            ranked.append(
                MemorySearchResult(record, score=score, preview=_preview(record.content))
            )
        ranked.sort(
            key=lambda item: (
                item.score,
                item.record.created_at,
                item.record.memory_id,
            ),
            reverse=True,
        )
        return tuple(ranked[:checked_limit])


def format_memory_records(records: Sequence[MemoryRecord]) -> str:
    """Render a bounded human-facing memory list."""

    if not records:
        return "No memories found."
    lines: list[str] = []
    for record in records:
        tags = ",".join(record.tags) if record.tags else "-"
        lines.append(
            f"{record.memory_id} {record.created_at} tags={tags}\n"
            f"  {_preview(record.content)}"
        )
    return "\n".join(lines)


__all__ = [
    "MAX_MEMORIES",
    "MAX_MEMORY_CONTENT",
    "MAX_SEARCH_LIMIT",
    "MEMORY_FILE",
    "MemoryRecord",
    "MemorySearchResult",
    "MemoryStore",
    "MemoryStoreError",
    "format_memory_records",
]
