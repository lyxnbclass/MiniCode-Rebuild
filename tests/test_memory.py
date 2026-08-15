from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from minicode_rebuild.memory import (
    MAX_MEMORY_CONTENT,
    MAX_MEMORY_PREVIEW,
    MEMORY_FILE,
    MemoryStore,
    MemoryStoreError,
    format_memory_records,
)


class SequenceClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def id_factory() -> Callable[[], str]:
    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"{counter:032x}"

    return next_id


def make_store(workspace: Path, *, max_records: int = 500) -> MemoryStore:
    factory = id_factory()
    return MemoryStore(
        workspace,
        max_records=max_records,
        clock=SequenceClock(),
        id_factory=factory,
    )


def test_memory_round_trip_is_workspace_local_and_normalized(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    saved = store.add(
        "  Prefer pytest for this project.  ",
        tags=(" Testing ", "PYTHON", "python"),
        source_session_id="session-1",
    )
    restored = MemoryStore(tmp_path).get(saved.memory_id)

    assert restored.content == "Prefer pytest for this project."
    assert restored.tags == ("Testing", "PYTHON")
    assert restored.source_session_id == "session-1"
    assert (tmp_path / MEMORY_FILE).is_file()
    assert store.list() == (saved,)


def test_search_ranks_exact_phrase_tag_and_newer_ties(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    old = store.add("Run unit tests before commit", tags=("quality",))
    tag = store.add("Use Ruff before every push", tags=("unit tests",))
    exact = store.add("The unit tests run offline", tags=("testing",))

    results = store.search("unit tests")

    assert [item.record for item in results] == [tag, exact, old]
    assert results[0].score > results[1].score
    assert results[1].score == results[2].score

    store.add("alpha older")
    newer = store.add("alpha newer")
    assert store.search("alpha")[0].record == newer


def test_search_omits_unrelated_records_and_honors_limit(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add("Python testing")
    newest = store.add("Python typing")
    store.add("Rust build")

    results = store.search("python", limit=1)

    assert len(results) == 1
    assert results[0].record == newest


def test_search_and_human_format_bound_memory_content(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    record = store.add("needle " + "x" * (MAX_MEMORY_PREVIEW + 50))

    result = store.search("needle")[0]
    rendered = format_memory_records((record,))

    assert len(result.preview) <= MAX_MEMORY_PREVIEW
    assert "[memory truncated]" in result.preview
    assert "[memory truncated]" in rendered


@pytest.mark.parametrize(
    ("content", "tags"),
    [
        (" ", ()),
        ("x" * (MAX_MEMORY_CONTENT + 1), ()),
        ("valid", ("",)),
        ("valid", tuple(str(index) for index in range(9))),
        ("valid", ("x" * 33,)),
    ],
)
def test_memory_rejects_unbounded_or_empty_input(
    tmp_path: Path, content: str, tags: tuple[str, ...]
) -> None:
    with pytest.raises(MemoryStoreError):
        make_store(tmp_path).add(content, tags=tags)


def test_capacity_is_explicit_and_does_not_evict(tmp_path: Path) -> None:
    store = make_store(tmp_path, max_records=2)
    first = store.add("first")
    second = store.add("second")

    with pytest.raises(MemoryStoreError, match="capacity"):
        store.add("third")

    assert store.list() == (second, first)


def test_delete_requires_valid_existing_id(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    record = store.add("temporary fact")

    assert store.delete(record.memory_id) == record
    assert store.list() == ()
    with pytest.raises(MemoryStoreError, match="not found"):
        store.delete(record.memory_id)
    with pytest.raises(MemoryStoreError, match="invalid"):
        store.get("../memory")


def test_corrupt_or_cross_workspace_storage_is_rejected(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    record = make_store(first).add("workspace fact")
    target = second / MEMORY_FILE
    target.parent.mkdir(parents=True)
    target.write_bytes((first / MEMORY_FILE).read_bytes())

    with pytest.raises(MemoryStoreError, match="different workspace"):
        MemoryStore(second).list()

    data = json.loads((first / MEMORY_FILE).read_text(encoding="utf-8"))
    data["memories"][0]["memory_id"] = "invalid"
    (first / MEMORY_FILE).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MemoryStoreError, match="invalid id"):
        MemoryStore(first).get(record.memory_id)


def test_memory_file_size_and_search_arguments_are_bounded(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    target = tmp_path / MEMORY_FILE
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * 2_000_001)

    with pytest.raises(MemoryStoreError, match="too large"):
        store.list()

    target.unlink()
    store.add("known fact")
    with pytest.raises(MemoryStoreError, match="must not be empty"):
        store.search(" ")
    with pytest.raises(MemoryStoreError, match="between"):
        store.search("known", limit=0)
    with pytest.raises(MemoryStoreError, match="integer"):
        store.search("known", limit=True)  # type: ignore[arg-type]


def test_memory_public_api_rejects_invalid_types_and_workspace(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(MemoryStoreError, match="sequence"):
        store.add("fact", tags="tag")
    with pytest.raises(MemoryStoreError, match="only strings"):
        store.add("fact", tags=(1,))  # type: ignore[arg-type]
    with pytest.raises(MemoryStoreError, match="Source session"):
        store.add("fact", source_session_id=" ")
    with pytest.raises(MemoryStoreError, match="must be text"):
        store.search(1)  # type: ignore[arg-type]

    missing = tmp_path / "missing"
    with pytest.raises(MemoryStoreError, match="cannot be resolved"):
        MemoryStore(missing)
    file_workspace = tmp_path / "file.txt"
    file_workspace.write_text("not a directory", encoding="utf-8")
    with pytest.raises(MemoryStoreError, match="must be a directory"):
        MemoryStore(file_workspace)
    with pytest.raises(TypeError, match="integer"):
        MemoryStore(tmp_path, max_records=True)
    with pytest.raises(ValueError, match="between"):
        MemoryStore(tmp_path, max_records=0)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ([], "root must be an object"),
        ({"schema_version": 99, "workspace": "x", "memories": []}, "schema"),
        ({"schema_version": 1, "workspace": "x", "memories": "bad"}, "workspace"),
    ],
)
def test_memory_rejects_invalid_persisted_roots(
    tmp_path: Path, replacement: object, message: str
) -> None:
    path = tmp_path / MEMORY_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(replacement), encoding="utf-8")

    with pytest.raises(MemoryStoreError, match=message):
        MemoryStore(tmp_path).list()


def test_memory_storage_rejects_runtime_symlink_escape(tmp_path: Path) -> None:
    runtime = tmp_path / ".minicode-rebuild"
    outside = tmp_path.parent / f"{tmp_path.name}-outside-memory"
    outside.mkdir()
    try:
        runtime.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This environment does not allow directory symlinks")

    with pytest.raises(MemoryStoreError, match="escapes"):
        MemoryStore(tmp_path).add("must stay local")
    assert not (outside / "memories.json").exists()
