"""Reproducible no-network MiniCode Rebuild demonstration."""

from __future__ import annotations

import tempfile
from pathlib import Path

from minicode_rebuild.cli import main


def run() -> int:
    with tempfile.TemporaryDirectory(prefix="minicode-rebuild-demo-") as directory:
        workspace = Path(directory)
        (workspace / "hello.py").write_text("print('hello')\n", encoding="utf-8")
        print(f"Demo workspace: {workspace}")
        code = main(
            ["--demo", "--cwd", str(workspace), "inspect this workspace"],
            environment={},
        )
        if code != 0:
            return code
        print("\nRedacted runtime timeline:")
        return main(
            ["--timeline", "20", "--cwd", str(workspace)],
            environment={},
        )


if __name__ == "__main__":
    raise SystemExit(run())
