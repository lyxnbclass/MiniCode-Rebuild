"""Tests for the user-facing command-line interface."""

from __future__ import annotations

import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from minicode_rebuild import __version__
from minicode_rebuild.cli import main
from minicode_rebuild.core import ModelResponse, TokenUsage, ToolCall
from minicode_rebuild.models import MockModel


def run_module(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the package module in a child process like an end user."""
    return subprocess.run(
        [sys.executable, "-m", "minicode_rebuild", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def test_package_exposes_version() -> None:
    assert __version__ == "0.1.0"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0

    output = capsys.readouterr().out
    assert "usage: minicode-rebuild" in output
    assert "--version" in output


def test_module_entrypoint_prints_version() -> None:
    result = run_module("--version")

    assert result.returncode == 0
    assert result.stdout.strip() == f"minicode-rebuild {__version__}"
    assert result.stderr == ""


def test_module_entrypoint_rejects_unknown_argument() -> None:
    result = run_module("--unknown")

    assert result.returncode == 2
    assert "unrecognized arguments: --unknown" in result.stderr


def test_demo_runs_complete_headless_tool_flow(tmp_path: Path) -> None:
    stdout = StringIO()
    stderr = StringIO()

    code = main(
        ["--demo", "--cwd", str(tmp_path), "inspect this workspace"],
        environment={},
        stdin=StringIO(),
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert "[tool] list_files -> ok" in stdout.getvalue()
    assert "Mock demo complete" in stdout.getvalue()
    assert "turns=1" in stdout.getvalue()
    assert "tools=1" in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_headless_real_model_uses_environment_and_reports_stats(
    tmp_path: Path,
) -> None:
    stdout = StringIO()
    model = MockModel(
        [ModelResponse(content="Done", usage=TokenUsage(3, 2))]
    )

    code = main(
        ["--cwd", str(tmp_path), "answer once"],
        environment={"OPENAI_API_KEY": "secret", "MINICODE_MAX_STEPS": "4"},
        stdin=StringIO(),
        stdout=stdout,
        stderr=StringIO(),
        model=model,
    )

    assert code == 0
    assert "Done" in stdout.getvalue()
    assert "steps=1" in stdout.getvalue()
    assert "tokens=5" in stdout.getvalue()
    assert model.requests[0].messages[-1].content == "answer once"


def test_headless_can_read_prompt_from_stdin(tmp_path: Path) -> None:
    model = MockModel([ModelResponse(content="From pipe")])
    stdout = StringIO()

    code = main(
        ["--headless", "--cwd", str(tmp_path)],
        environment={"OPENAI_API_KEY": "secret"},
        stdin=StringIO("piped request\n"),
        stdout=stdout,
        stderr=StringIO(),
        model=model,
    )

    assert code == 0
    assert "From pipe" in stdout.getvalue()
    assert model.requests[0].messages[-1].content == "piped request"


def test_missing_real_model_configuration_is_friendly(tmp_path: Path) -> None:
    stderr = StringIO()

    code = main(
        ["--cwd", str(tmp_path), "hello"],
        environment={},
        stdin=StringIO(),
        stdout=StringIO(),
        stderr=stderr,
    )

    assert code == 2
    assert "Configuration error:" in stderr.getvalue()
    assert "OPENAI_API_KEY" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_invalid_runtime_configuration_is_friendly(tmp_path: Path) -> None:
    stderr = StringIO()

    code = main(
        ["--demo", "--cwd", str(tmp_path), "hello"],
        environment={"MINICODE_MAX_STEPS": "zero"},
        stdin=StringIO(),
        stdout=StringIO(),
        stderr=stderr,
    )

    assert code == 2
    assert "MINICODE_MAX_STEPS must be an integer" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_model_failure_returns_nonzero_without_traceback(tmp_path: Path) -> None:
    stdout = StringIO()
    model = MockModel([RuntimeError("provider unavailable")])

    code = main(
        ["--cwd", str(tmp_path), "hello"],
        environment={"OPENAI_API_KEY": "secret"},
        stdin=StringIO(),
        stdout=stdout,
        stderr=StringIO(),
        model=model,
    )

    assert code == 1
    assert "Model request failed" in stdout.getvalue()
    assert "Traceback" not in stdout.getvalue()


def test_headless_mutations_are_denied_by_default(tmp_path: Path) -> None:
    model = MockModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={"path": "created.txt", "content": "hello"},
                    ),
                )
            ),
            ModelResponse(content="Handled denial"),
        ]
    )
    stdout = StringIO()

    code = main(
        ["--cwd", str(tmp_path), "write a file"],
        environment={"OPENAI_API_KEY": "secret"},
        stdin=StringIO(),
        stdout=stdout,
        stderr=StringIO(),
        model=model,
    )

    assert code == 0
    assert "[tool] write_file -> error (permission_required)" in stdout.getvalue()
    assert not (tmp_path / "created.txt").exists()


def test_headless_mutations_require_explicit_opt_in(tmp_path: Path) -> None:
    model = MockModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={"path": "created.txt", "content": "hello"},
                    ),
                )
            ),
            ModelResponse(content="Created"),
        ]
    )
    stderr = StringIO()

    code = main(
        ["--allow-mutations", "--cwd", str(tmp_path), "write a file"],
        environment={"OPENAI_API_KEY": "secret"},
        stdin=StringIO(),
        stdout=StringIO(),
        stderr=stderr,
        model=model,
    )

    assert code == 0
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "hello"
    assert "WARNING" in stderr.getvalue()


def test_interactive_mode_keeps_history_and_supports_commands(
    tmp_path: Path,
) -> None:
    model = MockModel(
        [ModelResponse(content="First"), ModelResponse(content="Second")]
    )
    stdout = StringIO()

    code = main(
        ["--interactive", "--cwd", str(tmp_path)],
        environment={"OPENAI_API_KEY": "secret"},
        stdin=StringIO("one\n/stats\ntwo\n/compact\n/help\n/exit\n"),
        stdout=stdout,
        stderr=StringIO(),
        model=model,
    )

    output = stdout.getvalue()
    assert code == 0
    assert "MiniCode Rebuild interactive" in output
    assert "First" in output and "Second" in output
    assert "turns=1" in output
    assert "/stats" in output and "/exit" in output
    assert "Context compact" in output
    assert "Goodbye" in output
    assert [message.content for message in model.requests[1].messages[-3:]] == [
        "one",
        "First",
        "two",
    ]


def test_cli_lists_and_resumes_saved_session(tmp_path: Path) -> None:
    first_model = MockModel([ModelResponse(content="First")])
    first_output = StringIO()
    assert main(
        ["--cwd", str(tmp_path), "one"],
        environment={"OPENAI_API_KEY": "secret"},
        stdout=first_output,
        stderr=StringIO(),
        model=first_model,
    ) == 0
    session_id = next((tmp_path / ".minicode-rebuild" / "sessions").glob("*.json")).stem

    list_output = StringIO()
    assert main(
        ["--cwd", str(tmp_path), "--list-sessions"],
        environment={},
        stdout=list_output,
        stderr=StringIO(),
    ) == 0
    assert session_id in list_output.getvalue()

    second_model = MockModel([ModelResponse(content="Second")])
    assert main(
        ["--cwd", str(tmp_path), "--resume", session_id, "two"],
        environment={"OPENAI_API_KEY": "secret"},
        stdout=StringIO(),
        stderr=StringIO(),
        model=second_model,
    ) == 0
    assert [message.content for message in second_model.requests[0].messages[-3:]] == [
        "one",
        "First",
        "two",
    ]


def test_interactive_rewind_requires_full_yes_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before", encoding="utf-8")
    model = MockModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={"path": "demo.txt", "content": "after"},
                    ),
                )
            ),
            ModelResponse(content="Changed"),
        ]
    )
    output = StringIO()

    code = main(
        ["--interactive", "--cwd", str(tmp_path)],
        environment={"OPENAI_API_KEY": "secret"},
        stdin=StringIO("change it\ny\n/rewind\nno\n/rewind\nyes\n/exit\n"),
        stdout=output,
        stderr=StringIO(),
        model=model,
    )

    assert code == 0
    assert "Rewind cancelled; no files changed." in output.getvalue()
    assert "Rewind applied." in output.getvalue()
    assert target.read_text(encoding="utf-8") == "before"


def test_keyboard_interrupt_exits_safely(tmp_path: Path) -> None:
    class InterruptingInput(StringIO):
        def readline(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt

    stderr = StringIO()

    code = main(
        ["--interactive", "--cwd", str(tmp_path)],
        environment={"OPENAI_API_KEY": "secret"},
        stdin=InterruptingInput(),
        stdout=StringIO(),
        stderr=stderr,
        model=MockModel([]),
    )

    assert code == 130
    assert "Interrupted" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()
