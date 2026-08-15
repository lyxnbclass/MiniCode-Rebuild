"""Run the local release-quality gate with the current Python interpreter."""

from __future__ import annotations

import subprocess
import sys
import tempfile

COMMANDS = (
    (sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"),
    (sys.executable, "-m", "mypy"),
    (
        sys.executable,
        "-m",
        "pytest",
        "--cov=minicode_rebuild",
        "--cov-report=term-missing",
        "-q",
    ),
    (sys.executable, "-m", "compileall", "-q", "src", "tests", "scripts"),
)


def run() -> int:
    for command in COMMANDS:
        print(f"+ {' '.join(command)}", flush=True)
        completed = subprocess.run(command, check=False)
        if completed.returncode:
            return completed.returncode
    with tempfile.TemporaryDirectory(prefix="minicode-rebuild-dist-") as directory:
        command = (
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            directory,
        )
        print(f"+ {' '.join(command)}", flush=True)
        completed = subprocess.run(command, check=False)
        if completed.returncode:
            return completed.returncode
    command = (sys.executable, "scripts/demo.py")
    print(f"+ {' '.join(command)}", flush=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
