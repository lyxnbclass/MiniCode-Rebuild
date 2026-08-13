from __future__ import annotations

from pathlib import Path

import pytest

from minicode_rebuild.core import Message, MessageRole, ToolCall
from minicode_rebuild.session import (
    RewindConfirmationRequired,
    RewindConflictError,
    SessionFormatError,
    SessionStatsData,
    SessionStore,
    format_transcript,
)


def _conversation() -> tuple[Message, ...]:
    return (
        Message(role=MessageRole.USER, content="inspect"),
        Message(
            role=MessageRole.ASSISTANT,
            tool_calls=(ToolCall(id="call-1", name="read_file", arguments={"path": "a.txt"}),),
        ),
        Message(role=MessageRole.TOOL, content='{"ok": true}', tool_call_id="call-1"),
        Message(role=MessageRole.ASSISTANT, content="done"),
    )


def test_session_round_trip_survives_new_store_instance(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create()
    session.messages = _conversation()
    session.stats = SessionStatsData(turns=1, model_steps=2, tool_calls=1, input_tokens=3, output_tokens=4)
    store.save(session)

    restored = SessionStore(tmp_path).load(session.session_id)

    assert restored.messages == session.messages
    assert restored.stats == session.stats
    assert restored.workspace == str(tmp_path.resolve())
    assert "read_file" in format_transcript(restored.messages)
    assert "call-1" in format_transcript(restored.messages)
    assert '{"path":"a.txt"}' in format_transcript(restored.messages)
    assert '{"ok": true}' in format_transcript(restored.messages)


def test_session_list_and_latest_are_workspace_local(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    older = store.create()
    newer = store.create()
    newer.updated_at = older.updated_at + 10
    store.save(newer)

    assert [item.session_id for item in store.list()] == [newer.session_id, older.session_id]
    assert store.load("latest").session_id == newer.session_id


def test_corrupt_or_cross_workspace_session_is_rejected(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create()
    path = store.sessions_dir / f"{record.session_id}.json"
    path.write_text('{"schema_version": 999}', encoding="utf-8")

    with pytest.raises(SessionFormatError):
        store.load(record.session_id)

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(SessionFormatError):
        SessionStore(other).save(record)


def test_checkpoint_path_escape_in_persisted_session_is_rejected(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create()
    target = tmp_path / "demo.txt"
    store.record_checkpoint(record, target, None, "new", operation="write_file")
    path = store.sessions_dir / f"{record.session_id}.json"
    data = path.read_text(encoding="utf-8").replace(
        '"path":"demo.txt"', '"path":"../outside.txt"'
    )
    path.write_text(data, encoding="utf-8")

    with pytest.raises(SessionFormatError):
        store.load(record.session_id)


def test_rewind_preview_is_read_only_and_apply_requires_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before\n", encoding="utf-8")
    store = SessionStore(tmp_path)
    session = store.create()
    checkpoint = store.record_checkpoint(session, target, "before\n", "after\n", operation="edit_file")
    target.write_text("after\n", encoding="utf-8")

    plan = store.preview_rewind(session, checkpoint.checkpoint_id)

    assert target.read_text(encoding="utf-8") == "after\n"
    assert plan.conflicts == ()
    assert plan.files[0].action == "restore"
    assert "-after" in plan.files[0].diff and "+before" in plan.files[0].diff
    with pytest.raises(RewindConfirmationRequired):
        store.apply_rewind(session, checkpoint.checkpoint_id, confirmed=False)
    store.apply_rewind(session, checkpoint.checkpoint_id, confirmed=True)
    assert target.read_text(encoding="utf-8") == "before\n"


def test_rewind_deletes_created_file_only_after_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    store = SessionStore(tmp_path)
    session = store.create()
    checkpoint = store.record_checkpoint(session, target, None, "new", operation="write_file")
    target.write_text("new", encoding="utf-8")

    plan = store.preview_rewind(session, checkpoint.checkpoint_id)
    assert plan.files[0].action == "delete"
    assert target.exists()
    store.apply_rewind(session, checkpoint.checkpoint_id, confirmed=True)
    assert not target.exists()


def test_rewind_refuses_to_overwrite_external_change(tmp_path: Path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before", encoding="utf-8")
    store = SessionStore(tmp_path)
    session = store.create()
    checkpoint = store.record_checkpoint(session, target, "before", "agent", operation="edit_file")
    target.write_text("external", encoding="utf-8")

    plan = store.preview_rewind(session, checkpoint.checkpoint_id)

    assert plan.conflicts == ("demo.txt",)
    with pytest.raises(RewindConflictError):
        store.apply_rewind(session, checkpoint.checkpoint_id, confirmed=True)
    assert target.read_text(encoding="utf-8") == "external"


def test_rewind_to_earlier_checkpoint_unwinds_later_changes(tmp_path: Path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("zero", encoding="utf-8")
    store = SessionStore(tmp_path)
    session = store.create()
    first = store.record_checkpoint(session, target, "zero", "one", operation="edit_file")
    target.write_text("one", encoding="utf-8")
    store.record_checkpoint(session, target, "one", "two", operation="edit_file")
    target.write_text("two", encoding="utf-8")

    plan = store.preview_rewind(session, first.checkpoint_id)
    assert len(plan.checkpoint_ids) == 2
    store.apply_rewind(session, first.checkpoint_id, confirmed=True)
    assert target.read_text(encoding="utf-8") == "zero"


def test_multi_file_rewind_rolls_back_if_one_restore_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first-before", encoding="utf-8")
    second.write_text("second-before", encoding="utf-8")
    store = SessionStore(tmp_path)
    session = store.create()
    first_checkpoint = store.record_checkpoint(
        session, first, "first-before", "first-after", operation="edit_file"
    )
    first.write_text("first-after", encoding="utf-8")
    store.record_checkpoint(
        session, second, "second-before", "second-after", operation="edit_file"
    )
    second.write_text("second-after", encoding="utf-8")
    original_write = store._atomic_write
    calls = 0

    def fail_second_write(target: Path, content: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("restore failed")
        original_write(target, content)

    monkeypatch.setattr(store, "_atomic_write", fail_second_write)

    with pytest.raises(Exception, match="Rewind failed"):
        store.apply_rewind(session, first_checkpoint.checkpoint_id, confirmed=True)

    assert first.read_text(encoding="utf-8") == "first-after"
    assert second.read_text(encoding="utf-8") == "second-after"
    assert all(item.rewound_at is None for item in session.checkpoints)
