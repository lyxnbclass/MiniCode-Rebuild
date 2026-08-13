"""Workspace-local session persistence, transcripts, checkpoints, and rewind."""

from __future__ import annotations

import builtins
import difflib
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from minicode_rebuild.core import JsonValue, Message, MessageRole, ToolCall
from minicode_rebuild.workspace import WorkspacePathError, resolve_workspace_path

SCHEMA_VERSION = 1
SESSION_DIRECTORY = ".minicode-rebuild/sessions"
_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class SessionError(RuntimeError):
    """Base class for recoverable session failures."""


class SessionNotFoundError(SessionError):
    """Raised when a requested session does not exist."""


class SessionFormatError(SessionError):
    """Raised when persisted data is invalid or belongs to another workspace."""


class RewindConfirmationRequired(SessionError):
    """Raised when destructive restore was not explicitly confirmed."""


class RewindConflictError(SessionError):
    """Raised when files changed after their checkpoint was captured."""


@dataclass(frozen=True, slots=True)
class SessionStatsData:
    turns: int = 0
    model_steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    compactions: int = 0


@dataclass(slots=True)
class FileCheckpoint:
    checkpoint_id: str
    created_at: float
    path: str
    existed: bool
    previous_content: str
    resulting_sha256: str
    operation: str
    rewound_at: float | None = None


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    workspace: str
    created_at: float
    updated_at: float
    messages: tuple[Message, ...] = ()
    transcript: tuple[Message, ...] = ()
    stats: SessionStatsData = field(default_factory=SessionStatsData)
    checkpoints: list[FileCheckpoint] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RewindFile:
    path: str
    action: str
    diff: str


@dataclass(frozen=True, slots=True)
class RewindPlan:
    checkpoint_ids: tuple[str, ...]
    files: tuple[RewindFile, ...]
    conflicts: tuple[str, ...]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _serialize_message(message: Message) -> dict[str, JsonValue]:
    return {
        "role": message.role.value,
        "content": message.content,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
            for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
    }


def _deserialize_message(value: object) -> Message:
    if not isinstance(value, dict):
        raise SessionFormatError("Session message must be an object")
    try:
        raw_calls = value.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise TypeError
        if any(not isinstance(call, dict) for call in raw_calls):
            raise TypeError
        calls = tuple(
            ToolCall(
                id=str(call["id"]),
                name=str(call["name"]),
                arguments=call.get("arguments", {}),
            )
            for call in raw_calls
        )
        return Message(
            role=MessageRole(str(value["role"])),
            content=str(value.get("content", "")),
            tool_calls=calls,
            tool_call_id=(
                None
                if value.get("tool_call_id") is None
                else str(value["tool_call_id"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionFormatError("Session contains an invalid message") from exc


def format_transcript(messages: tuple[Message, ...]) -> str:
    """Render normalized messages including complete tool-call evidence."""

    lines: list[str] = []
    for message in messages:
        if message.role is MessageRole.ASSISTANT and message.tool_calls:
            if message.content:
                lines.append(f"assistant: {message.content}")
            for call in message.tool_calls:
                arguments = json.dumps(
                    dict(call.arguments), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                lines.append(f"assistant tool[{call.id}] {call.name} {arguments}")
        elif message.role is MessageRole.TOOL:
            lines.append(f"tool[{message.tool_call_id}]: {message.content}")
        else:
            lines.append(f"{message.role.value}: {message.content}")
    return "\n".join(lines)


class SessionStore:
    """Persist one workspace's sessions and perform guarded rewinds."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise SessionFormatError(f"Workspace is not a directory: {self.workspace}")
        try:
            self.sessions_dir = resolve_workspace_path(
                self.workspace, SESSION_DIRECTORY
            )
        except WorkspacePathError as exc:
            raise SessionFormatError(
                "Session storage resolves outside the workspace"
            ) from exc

    def create(self) -> SessionRecord:
        now = time.time()
        record = SessionRecord(
            session_id=uuid.uuid4().hex,
            workspace=str(self.workspace),
            created_at=now,
            updated_at=now,
        )
        self.save(record)
        return record

    def _path(self, session_id: str) -> Path:
        if not _SESSION_ID.fullmatch(session_id):
            raise SessionFormatError("Invalid session id")
        return self.sessions_dir / f"{session_id}.json"

    def _resolve_record_path(self, relative: str) -> Path:
        try:
            target = resolve_workspace_path(self.workspace, relative)
            normalized = target.relative_to(self.workspace).as_posix()
            if normalized.split("/", 1)[0].casefold() == ".minicode-rebuild":
                raise ValueError
            return target
        except (WorkspacePathError, ValueError) as exc:
            raise SessionFormatError("Checkpoint path is outside the recoverable workspace") from exc

    def _validate_record(self, record: SessionRecord) -> None:
        if Path(record.workspace).resolve() != self.workspace:
            raise SessionFormatError("Session belongs to a different workspace")

    def _deserialize_checkpoint(self, value: object) -> FileCheckpoint:
        if not isinstance(value, dict):
            raise SessionFormatError("Session checkpoint must be an object")
        try:
            checkpoint = FileCheckpoint(**value)
            if not _SESSION_ID.fullmatch(checkpoint.checkpoint_id):
                raise ValueError
            if not isinstance(checkpoint.existed, bool):
                raise TypeError
            if not isinstance(checkpoint.previous_content, str):
                raise TypeError
            if not _SHA256.fullmatch(checkpoint.resulting_sha256):
                raise ValueError
            if not checkpoint.operation.strip():
                raise ValueError
            resolved = self._resolve_record_path(checkpoint.path)
            relative = resolved.relative_to(self.workspace).as_posix()
            checkpoint.path = relative
            return checkpoint
        except (TypeError, ValueError, WorkspacePathError) as exc:
            raise SessionFormatError("Session contains an invalid checkpoint") from exc

    def save(self, record: SessionRecord) -> None:
        self._validate_record(record)
        path = self._path(record.session_id)
        record.updated_at = max(record.updated_at, time.time())
        data = {
            "schema_version": SCHEMA_VERSION,
            "session_id": record.session_id,
            "workspace": record.workspace,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "messages": [_serialize_message(message) for message in record.messages],
            "transcript": [_serialize_message(message) for message in record.transcript],
            "stats": asdict(record.stats),
            "checkpoints": [asdict(checkpoint) for checkpoint in record.checkpoints],
        }
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{record.session_id}.", suffix=".tmp", dir=self.sessions_dir
        )
        temporary = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, session_id: str) -> SessionRecord:
        if session_id == "latest":
            sessions = self.list()
            if not sessions:
                raise SessionNotFoundError("No saved sessions for this workspace")
            return sessions[0]
        path = self._path(session_id)
        if not path.is_file():
            raise SessionNotFoundError(f"Session not found: {session_id}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
                raise SessionFormatError("Unsupported or missing session schema version")
            if value.get("session_id") != session_id:
                raise SessionFormatError("Session id does not match its file name")
            workspace = str(value["workspace"])
            if Path(workspace).resolve() != self.workspace:
                raise SessionFormatError("Session belongs to a different workspace")
            messages_value = value.get("messages", [])
            transcript_value = value.get("transcript", messages_value)
            checkpoints_value = value.get("checkpoints", [])
            if (
                not isinstance(messages_value, list)
                or not isinstance(transcript_value, list)
                or not isinstance(checkpoints_value, list)
            ):
                raise SessionFormatError("Session collections must be arrays")
            stats_value = value.get("stats", {})
            if not isinstance(stats_value, dict):
                raise SessionFormatError("Session stats must be an object")
            checkpoints = [self._deserialize_checkpoint(item) for item in checkpoints_value]
            stats = SessionStatsData(**stats_value)
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in asdict(stats).values()
            ):
                raise SessionFormatError("Session stats must be non-negative integers")
            return SessionRecord(
                session_id=session_id,
                workspace=workspace,
                created_at=float(value["created_at"]),
                updated_at=float(value["updated_at"]),
                messages=tuple(_deserialize_message(item) for item in messages_value),
                transcript=tuple(_deserialize_message(item) for item in transcript_value),
                stats=stats,
                checkpoints=checkpoints,
            )
        except SessionFormatError:
            raise
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise SessionFormatError(f"Invalid session file: {path.name}") from exc

    def list(self) -> list[SessionRecord]:
        if not self.sessions_dir.is_dir():
            return []
        records: list[SessionRecord] = []
        for path in self.sessions_dir.glob("*.json"):
            if not _SESSION_ID.fullmatch(path.stem):
                continue
            try:
                records.append(self.load(path.stem))
            except SessionError:
                continue
        return sorted(records, key=lambda item: item.updated_at, reverse=True)

    def record_checkpoint(
        self,
        record: SessionRecord,
        target: Path,
        previous_content: str | None,
        next_content: str,
        *,
        operation: str,
    ) -> FileCheckpoint:
        self._validate_record(record)
        resolved = self._resolve_record_path(str(Path(target).resolve()))
        checkpoint = FileCheckpoint(
            checkpoint_id=uuid.uuid4().hex,
            created_at=time.time(),
            path=resolved.relative_to(self.workspace).as_posix(),
            existed=previous_content is not None,
            previous_content=previous_content or "",
            resulting_sha256=_content_hash(next_content),
            operation=operation,
        )
        record.checkpoints.append(checkpoint)
        try:
            self.save(record)
        except Exception:
            record.checkpoints.remove(checkpoint)
            raise
        return checkpoint

    def discard_checkpoint(self, record: SessionRecord, checkpoint_id: str) -> None:
        self._validate_record(record)
        record.checkpoints[:] = [
            item for item in record.checkpoints if item.checkpoint_id != checkpoint_id
        ]
        self.save(record)

    def _selected(
        self, record: SessionRecord, checkpoint_id: str | None
    ) -> builtins.list[FileCheckpoint]:
        self._validate_record(record)
        active = [item for item in record.checkpoints if item.rewound_at is None]
        if not active:
            raise SessionNotFoundError("No active checkpoints to rewind")
        target = checkpoint_id or active[-1].checkpoint_id
        positions = [index for index, item in enumerate(active) if item.checkpoint_id == target]
        if not positions:
            raise SessionNotFoundError(f"Active checkpoint not found: {target}")
        return active[positions[0] :]

    def _rewind_states(
        self, selected: builtins.list[FileCheckpoint]
    ) -> tuple[dict[str, str | None], tuple[str, ...]]:
        desired: dict[str, str | None] = {}
        latest: dict[str, FileCheckpoint] = {}
        for checkpoint in selected:
            desired.setdefault(
                checkpoint.path,
                checkpoint.previous_content if checkpoint.existed else None,
            )
            latest[checkpoint.path] = checkpoint
        conflicts: list[str] = []
        for relative, checkpoint in latest.items():
            target = self._resolve_record_path(relative)
            try:
                current = target.read_text(encoding="utf-8") if target.is_file() else None
            except (OSError, UnicodeDecodeError):
                current = None
            if current is None or _content_hash(current) != checkpoint.resulting_sha256:
                conflicts.append(relative)
        return desired, tuple(sorted(conflicts))

    def preview_rewind(
        self, record: SessionRecord, checkpoint_id: str | None = None
    ) -> RewindPlan:
        selected = self._selected(record, checkpoint_id)
        desired, conflicts = self._rewind_states(selected)
        files: list[RewindFile] = []
        for relative, prior in desired.items():
            target = self._resolve_record_path(relative)
            try:
                current = target.read_text(encoding="utf-8") if target.is_file() else ""
            except (OSError, UnicodeDecodeError):
                current = ""
            after = prior or ""
            diff = "".join(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"current/{relative}",
                    tofile=f"rewind/{relative}",
                )
            )
            files.append(RewindFile(relative, "restore" if prior is not None else "delete", diff))
        return RewindPlan(
            checkpoint_ids=tuple(item.checkpoint_id for item in selected),
            files=tuple(files),
            conflicts=conflicts,
        )

    def apply_rewind(
        self,
        record: SessionRecord,
        checkpoint_id: str | None = None,
        *,
        confirmed: bool,
    ) -> RewindPlan:
        if not confirmed:
            raise RewindConfirmationRequired("Rewind requires explicit confirmation")
        selected = self._selected(record, checkpoint_id)
        desired, conflicts = self._rewind_states(selected)
        plan = self.preview_rewind(record, checkpoint_id)
        if conflicts:
            raise RewindConflictError(
                "Refusing to overwrite files changed after checkpoint: " + ", ".join(conflicts)
            )
        targets = {
            relative: self._resolve_record_path(relative) for relative in desired
        }
        originals: dict[str, tuple[bool, str]] = {}
        for relative, target in targets.items():
            try:
                originals[relative] = (
                    target.is_file(),
                    target.read_text(encoding="utf-8") if target.is_file() else "",
                )
            except (OSError, UnicodeDecodeError) as exc:
                raise SessionError(f"Cannot prepare rewind for {relative}: {exc}") from exc

        selected_ids = {item.checkpoint_id for item in selected}
        previous_rewound = {
            item.checkpoint_id: item.rewound_at
            for item in record.checkpoints
            if item.checkpoint_id in selected_ids
        }
        changed: list[str] = []
        try:
            for relative, prior in desired.items():
                target = targets[relative]
                if prior is None:
                    target.unlink(missing_ok=True)
                else:
                    self._atomic_write(target, prior)
                changed.append(relative)
            now = time.time()
            for item in record.checkpoints:
                if item.checkpoint_id in selected_ids:
                    item.rewound_at = now
            self.save(record)
        except Exception as exc:
            for item in record.checkpoints:
                if item.checkpoint_id in previous_rewound:
                    item.rewound_at = previous_rewound[item.checkpoint_id]
            rollback_errors: list[str] = []
            for relative in reversed(changed):
                existed, content = originals[relative]
                try:
                    if existed:
                        self._atomic_write(targets[relative], content)
                    else:
                        targets[relative].unlink(missing_ok=True)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{relative}: {rollback_exc}")
            suffix = (
                "; rollback errors: " + "; ".join(rollback_errors)
                if rollback_errors
                else "; original files restored"
            )
            raise SessionError(f"Rewind failed: {exc}{suffix}") from exc
        return plan

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        existing_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temporary = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if existing_mode is not None:
                os.chmod(temporary, existing_mode)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "FileCheckpoint",
    "RewindConfirmationRequired",
    "RewindConflictError",
    "RewindFile",
    "RewindPlan",
    "SessionError",
    "SessionFormatError",
    "SessionNotFoundError",
    "SessionRecord",
    "SessionStatsData",
    "SessionStore",
    "format_transcript",
]
