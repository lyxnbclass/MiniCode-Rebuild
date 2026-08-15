from __future__ import annotations

import json
from pathlib import Path

from minicode_rebuild.hooks import HookContext, HookEvent, HookManager
from minicode_rebuild.observability import EventLog, format_timeline, register_event_log


def test_event_log_redacts_prompts_arguments_outputs_and_unknown_fields(
    tmp_path: Path,
) -> None:
    log = EventLog(tmp_path)
    log.record(
        HookContext(
            HookEvent.BEFORE_TOOL,
            {
                "session_id": "not-allowed-here",
                "tool_name": "read_file",
                "arguments": {"path": "secret.txt", "token": "top-secret"},
                "user_message": "private prompt",
            },
        )
    )
    log.record(
        HookContext(
            HookEvent.AFTER_TOOL,
            {
                "tool_name": "read_file",
                "result": {
                    "ok": False,
                    "error_code": "read_error",
                    "output": "private file contents",
                },
            },
        )
    )

    raw = log.path.read_text(encoding="utf-8")
    assert "read_file" in raw and "read_error" in raw
    assert "secret.txt" not in raw
    assert "top-secret" not in raw
    assert "private prompt" not in raw
    assert "private file contents" not in raw


def test_event_log_skips_corrupt_lines_and_limits_latest_events(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    log.path.parent.mkdir(parents=True)
    log.path.write_text("not-json\n", encoding="utf-8")
    for index in range(3):
        log.record(HookContext(HookEvent.AGENT_STOP, {"stop_reason": str(index)}))

    events = log.read(limit=2)

    assert [event.data["stop_reason"] for event in events] == ["1", "2"]
    assert "agent_stop" in format_timeline(events)


def test_registered_event_log_receives_all_lifecycle_events(tmp_path: Path) -> None:
    hooks = HookManager()
    log = EventLog(tmp_path)
    register_event_log(hooks, log)

    for event in HookEvent:
        hooks.emit(event, session_id="abc", tool_name="sample")

    assert [event.event for event in log.read()] == [event.value for event in HookEvent]


def test_event_log_is_valid_one_object_per_line(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    log.record(HookContext(HookEvent.SESSION_CREATE, {"session_id": "abc"}))

    payload = json.loads(log.path.read_text(encoding="utf-8"))

    assert payload["event"] == "session_create"
    assert payload["data"] == {"session_id": "abc"}
