"""Command-line entry point for MiniCode Rebuild."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from minicode_rebuild import __version__


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser without performing side effects."""
    parser = argparse.ArgumentParser(
        prog="minicode-rebuild",
        description="A staged rebuild of a local terminal AI coding agent.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the phase-0 CLI and return a process exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
