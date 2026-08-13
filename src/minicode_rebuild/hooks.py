"""Small synchronous extension hooks with explicit failure reporting."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


class HookEvent(str, Enum):
    """Stable extension points exposed by the runtime and tool boundary."""

    AGENT_START = "agent_start"
    AGENT_STOP = "agent_stop"
    SESSION_CREATE = "session_create"
    SESSION_RESUME = "session_resume"
    SESSION_SAVE = "session_save"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"


@dataclass(frozen=True, slots=True)
class HookContext:
    """Read-only snapshot passed to one hook handler."""

    event: HookEvent
    data: Mapping[str, object]


HookHandler = Callable[[HookContext], None]


@dataclass(frozen=True, slots=True)
class HookFailure:
    """One isolated hook failure that callers can surface to users."""

    event: HookEvent
    name: str
    error: str


@dataclass(frozen=True, slots=True)
class HookReport:
    """Observable result of emitting one hook event."""

    event: HookEvent
    handlers: int
    failures: tuple[HookFailure, ...]
    duration_ms: float


@dataclass(frozen=True, slots=True)
class HookRegistration:
    """Metadata for a registered in-process extension."""

    event: HookEvent
    name: str
    handler: HookHandler


HookObserver = Callable[[HookReport], None]


class HookManager:
    """Register and emit bounded synchronous hooks without global state."""

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[HookRegistration]] = {
            event: [] for event in HookEvent
        }
        self._reports: list[HookReport] = []

    def register(
        self, event: HookEvent, handler: HookHandler, *, name: str = ""
    ) -> Callable[[], None]:
        """Register a handler and return an idempotent unregister callback."""

        if not isinstance(event, HookEvent):
            raise TypeError("hook event must be a HookEvent")
        if not callable(handler):
            raise TypeError("hook handler must be callable")
        normalized_name = name.strip() or getattr(handler, "__name__", "hook")
        registration = HookRegistration(event, normalized_name, handler)
        self._hooks[event].append(registration)

        def unregister() -> None:
            try:
                self._hooks[event].remove(registration)
            except ValueError:
                pass

        return unregister

    def registrations(
        self, event: HookEvent | None = None
    ) -> tuple[HookRegistration, ...]:
        """Return a stable registration snapshot."""

        if event is not None:
            return tuple(self._hooks[event])
        return tuple(
            registration
            for current_event in HookEvent
            for registration in self._hooks[current_event]
        )

    def emit(self, event: HookEvent, **data: object) -> HookReport:
        """Run hooks in registration order and isolate ordinary exceptions."""

        if not isinstance(event, HookEvent):
            raise TypeError("hook event must be a HookEvent")
        handlers = tuple(self._hooks[event])
        failures: list[HookFailure] = []
        started = time.perf_counter()
        for registration in handlers:
            try:
                snapshot = MappingProxyType(deepcopy(data))
                registration.handler(HookContext(event=event, data=snapshot))
            except Exception as exc:
                detail = str(exc)
                suffix = f": {detail}" if detail else ""
                failures.append(
                    HookFailure(
                        event=event,
                        name=registration.name,
                        error=f"{type(exc).__name__}{suffix}",
                    )
                )
        report = HookReport(
            event=event,
            handlers=len(handlers),
            failures=tuple(failures),
            duration_ms=(time.perf_counter() - started) * 1_000,
        )
        self._reports.append(report)
        return report

    def reports(self) -> tuple[HookReport, ...]:
        """Return emitted reports so embedding callers never lose failures."""

        return tuple(self._reports)


__all__ = [
    "HookContext",
    "HookEvent",
    "HookFailure",
    "HookHandler",
    "HookManager",
    "HookObserver",
    "HookRegistration",
    "HookReport",
]
