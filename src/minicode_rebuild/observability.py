"""Redacted workspace-local JSONL events and runtime timeline rendering."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicode_rebuild.hooks import HookContext, HookEvent, HookManager
from minicode_rebuild.workspace import WorkspacePathError, resolve_workspace_path

EVENT_LOG_PATH = ".minicode-rebuild/events.jsonl"
MAX_EVENT_BYTES = 16 * 1024
MAX_TIMELINE_EVENTS = 1_000
_ALLOWED_FIELDS: dict[HookEvent, tuple[str, ...]] = {
    HookEvent.SESSION_CREATE: ("session_id",),
    HookEvent.SESSION_RESUME: ("session_id",),
    HookEvent.SESSION_SAVE: ("session_id",),
    HookEvent.AGENT_START: ("session_id",),
    HookEvent.AGENT_STOP: ("session_id", "stop_reason", "completed"),
    HookEvent.BEFORE_TOOL: ("tool_name",),
    HookEvent.AFTER_TOOL: ("tool_name",),
}


class ObservabilityError(RuntimeError):
    """Raised for recoverable event storage failures."""


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    timestamp: float
    event: str
    data: Mapping[str, str | bool | int | float | None]


def _safe_scalar(value: object) -> str | bool | int | float | None:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


class EventLog:
    """Append redacted lifecycle metadata inside the reserved runtime directory."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        try:
            self.path = resolve_workspace_path(self.workspace, EVENT_LOG_PATH)
        except WorkspacePathError as exc:
            raise ObservabilityError("Event log resolves outside workspace") from exc

    def record(self, context: HookContext) -> None:
        """Write one bounded event without prompt, arguments, output, or secrets."""

        allowed = _ALLOWED_FIELDS[context.event]
        data = {
            name: _safe_scalar(context.data[name])
            for name in allowed
            if name in context.data
        }
        if context.event is HookEvent.AFTER_TOOL:
            result = context.data.get("result")
            if isinstance(result, Mapping):
                data["ok"] = bool(result.get("ok"))
                error_code = result.get("error_code")
                if error_code is not None:
                    data["error_code"] = str(error_code)
        payload = json.dumps(
            {"timestamp": time.time(), "event": context.event.value, "data": data},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded = (payload + "\n").encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise ObservabilityError("Event exceeds the bounded log entry size")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise ObservabilityError("Event log could not be written") from exc

    def read(self, *, limit: int = 100) -> tuple[RuntimeEvent, ...]:
        """Read and validate the latest bounded events, skipping corrupt lines."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TIMELINE_EVENTS:
            raise ObservabilityError(
                f"timeline limit must be between 1 and {MAX_TIMELINE_EVENTS}"
            )
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeDecodeError) as exc:
            raise ObservabilityError("Event log could not be read") from exc
        events: list[RuntimeEvent] = []
        for line in lines[-limit:]:
            try:
                value: Any = json.loads(line)
                timestamp = float(value["timestamp"])
                event = str(value["event"])
                data = value["data"]
                if not isinstance(data, dict):
                    continue
                normalized = {
                    str(key): _safe_scalar(item) for key, item in data.items()
                }
                events.append(RuntimeEvent(timestamp, event, normalized))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(events)


def register_event_log(hooks: HookManager, event_log: EventLog) -> None:
    """Register the same redacted sink at every public lifecycle point."""

    for event in HookEvent:
        hooks.register(event, event_log.record, name="event-log")


def format_timeline(events: tuple[RuntimeEvent, ...]) -> str:
    """Render a concise chronological event timeline."""

    if not events:
        return "No runtime events."
    lines: list[str] = []
    for event in events:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.timestamp))
        details = " ".join(f"{key}={value}" for key, value in event.data.items())
        lines.append(f"{timestamp} {event.event}" + (f" {details}" if details else ""))
    return "\n".join(lines)


__all__ = [
    "EVENT_LOG_PATH",
    "EventLog",
    "MAX_TIMELINE_EVENTS",
    "ObservabilityError",
    "RuntimeEvent",
    "format_timeline",
    "register_event_log",
]
