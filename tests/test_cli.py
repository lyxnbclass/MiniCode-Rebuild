"""Tests for the phase-0 command-line interface."""

from __future__ import annotations

import subprocess
import sys

import pytest

from minicode_rebuild import __version__
from minicode_rebuild.cli import main


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
