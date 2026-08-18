from __future__ import annotations

from pathlib import Path

from minicode_rebuild.memory import MEMORY_FILE, MemoryStore
from minicode_rebuild.readiness import check_readiness, format_readiness


def test_readiness_validates_provider_without_network(tmp_path: Path) -> None:
    report = check_readiness(
        tmp_path,
        {
            "OPENAI_API_KEY": "not-sent-anywhere",
            "OPENAI_BASE_URL": "https://example.test/v1",
            "MINICODE_MODEL": "demo-model",
        },
    )

    assert report.ready is True
    output = format_readiness(report)
    assert "model=demo-model" in output
    assert "https://example.test/v1/chat/completions" in output
    assert "not-sent-anywhere" not in output
    assert "memory-store: 0 stored" in output


def test_readiness_reports_missing_provider_key(tmp_path: Path) -> None:
    report = check_readiness(tmp_path, {}, require_provider=True)

    assert report.ready is False
    assert any(
        check.name == "provider-config" and not check.ready
        for check in report.checks
    )


def test_demo_readiness_does_not_require_provider_key(tmp_path: Path) -> None:
    report = check_readiness(tmp_path, {}, require_provider=False)

    assert report.ready is True
    assert "not required for demo" in format_readiness(report)


def test_readiness_validates_existing_memory_storage(tmp_path: Path) -> None:
    MemoryStore(tmp_path).add("durable fact")

    healthy = check_readiness(tmp_path, {}, require_provider=False)

    assert healthy.ready is True
    assert "memory-store: 1 stored" in format_readiness(healthy)

    (tmp_path / MEMORY_FILE).write_text("not-json", encoding="utf-8")
    corrupt = check_readiness(tmp_path, {}, require_provider=False)

    assert corrupt.ready is False
    assert "[fail] memory-store:" in format_readiness(corrupt)
