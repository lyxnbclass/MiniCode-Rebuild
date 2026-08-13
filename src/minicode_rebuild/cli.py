"""Command-line entry point for MiniCode Rebuild."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from minicode_rebuild import __version__
from minicode_rebuild.agent import AgentResult, AgentStopReason
from minicode_rebuild.cli_runtime import (
    AgentSession,
    build_session,
    format_stats,
    make_permission_prompt,
)
from minicode_rebuild.config import (
    ModelConfigurationError,
    ModelSettings,
    RuntimeSettings,
)
from minicode_rebuild.core import ModelAdapter, ModelResponse, ToolCall
from minicode_rebuild.models import MockModel
from minicode_rebuild.models.openai_compatible import OpenAICompatibleAdapter
from minicode_rebuild.permissions import PermissionDecision
from minicode_rebuild.session import RewindPlan, SessionError, SessionStore

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser without performing side effects."""

    parser = argparse.ArgumentParser(
        prog="minicode-rebuild",
        description="A safe local terminal AI coding agent.",
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="run one Headless request; quote multi-word prompts if preferred",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--interactive",
        action="store_true",
        help="start a line-oriented interactive session",
    )
    mode.add_argument(
        "--headless",
        action="store_true",
        help="read one prompt from arguments or standard input and exit",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="run a deterministic MockModel tool demonstration without an API key",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path.cwd(),
        metavar="PATH",
        help="workspace root (default: current directory)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        metavar="N",
        help="maximum model steps per turn (or MINICODE_MAX_STEPS)",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        metavar="TEXT",
        help="override MINICODE_SYSTEM_PROMPT for this process",
    )
    parser.add_argument(
        "--allow-mutations",
        action="store_true",
        help="Headless only: approve mutations for this run (use with care)",
    )
    parser.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="resume a workspace session by id, or use 'latest'",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="list saved sessions for the workspace and exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _runtime_settings(
    args: argparse.Namespace,
    environment: Mapping[str, str],
) -> RuntimeSettings:
    settings = RuntimeSettings.from_env(environment)
    if args.max_steps is not None:
        settings = replace(settings, max_steps=args.max_steps)
    if args.system_prompt is not None:
        settings = replace(settings, system_prompt=args.system_prompt)
    return settings


def _workspace(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.exists():
        raise ModelConfigurationError(f"Workspace does not exist: {resolved}")
    if not resolved.is_dir():
        raise ModelConfigurationError(f"Workspace is not a directory: {resolved}")
    return resolved


def _demo_model() -> MockModel:
    return MockModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="demo-list",
                        name="list_files",
                        arguments={"path": ".", "limit": 5},
                    ),
                )
            ),
            ModelResponse(content="Mock demo complete: inspected the workspace."),
        ]
    )


def _select_model(
    *,
    demo: bool,
    environment: Mapping[str, str],
    injected: ModelAdapter | None,
) -> ModelAdapter:
    if demo:
        return injected or _demo_model()
    model_settings = ModelSettings.from_env(environment)
    return injected or OpenAICompatibleAdapter(model_settings)


def _print_result(
    session: AgentSession,
    result: AgentResult,
    output: TextIO,
) -> int:
    output.write(f"{result.content}\n")
    output.write(f"[stats] {format_stats(session.stats)}\n")
    output.flush()
    if result.stop_reason is AgentStopReason.FINAL_RESPONSE:
        return EXIT_OK
    return EXIT_RUNTIME_ERROR


def _headless_prompt(args: argparse.Namespace, input_stream: TextIO) -> str:
    prompt = " ".join(args.prompt).strip()
    if not prompt and args.headless:
        prompt = input_stream.read().strip()
    if not prompt:
        raise ModelConfigurationError(
            "Headless mode requires a prompt argument or piped standard input"
        )
    return prompt


def _run_headless(
    *,
    args: argparse.Namespace,
    session: AgentSession,
    input_stream: TextIO,
    output: TextIO,
) -> int:
    return _print_result(
        session,
        session.run(_headless_prompt(args, input_stream)),
        output,
    )


def _format_rewind_plan(plan: RewindPlan) -> str:
    lines = [f"Rewind preview: {len(plan.checkpoint_ids)} checkpoint(s)"]
    for item in plan.files:
        lines.append(f"[{item.action}] {item.path}")
        if item.diff:
            lines.append(item.diff.rstrip())
    if plan.conflicts:
        lines.append("Conflicts: " + ", ".join(plan.conflicts))
    return "\n".join(lines) + "\n"


def _run_interactive(
    *,
    session: AgentSession,
    input_stream: TextIO,
    output: TextIO,
) -> int:
    output.write(
        "MiniCode Rebuild interactive\n"
        f"Session: {session.session_id}\n"
        "Commands: /help, /session, /sessions, /transcript, /checkpoints, "
        "/rewind-preview [id], /rewind [id], /stats, /compact, /exit\n"
    )
    output.flush()
    while True:
        output.write("you> ")
        output.flush()
        line = input_stream.readline()
        if line == "":
            output.write("Goodbye.\n")
            return EXIT_OK
        user_message = line.strip()
        if not user_message:
            continue
        if user_message in {"/exit", "/quit"}:
            output.write("Goodbye.\n")
            return EXIT_OK
        if user_message == "/help":
            output.write(
                "Commands: /help, /session, /sessions, /transcript, /checkpoints, "
                "/rewind-preview [id], /rewind [id], /stats, /compact, /exit\n"
            )
            continue
        if user_message == "/session":
            output.write(f"Session: {session.session_id}\n")
            continue
        if user_message == "/sessions":
            records = session.list_sessions()
            if not records:
                output.write("No saved sessions.\n")
            for record in records:
                active = sum(item.rewound_at is None for item in record.checkpoints)
                output.write(
                    f"{record.session_id} turns={record.stats.turns} checkpoints={active}\n"
                )
            continue
        if user_message == "/transcript":
            output.write((session.transcript() or "(empty transcript)") + "\n")
            continue
        if user_message == "/checkpoints":
            record = session.session_record
            active = [] if record is None else [
                item for item in record.checkpoints if item.rewound_at is None
            ]
            if not active:
                output.write("No active checkpoints.\n")
            for item in active:
                output.write(f"{item.checkpoint_id} {item.operation} {item.path}\n")
            continue
        if user_message == "/rewind-preview" or user_message.startswith("/rewind-preview "):
            checkpoint_id = user_message[len("/rewind-preview") :].strip() or None
            try:
                output.write(_format_rewind_plan(session.preview_rewind(checkpoint_id)))
            except SessionError as exc:
                output.write(f"Session error: {exc}\n")
            continue
        if user_message == "/rewind" or user_message.startswith("/rewind "):
            checkpoint_id = user_message[len("/rewind") :].strip() or None
            try:
                plan = session.preview_rewind(checkpoint_id)
            except SessionError as exc:
                output.write(f"Session error: {exc}\n")
                continue
            output.write(_format_rewind_plan(plan))
            if plan.conflicts:
                output.write("Rewind refused because conflicts exist.\n")
                continue
            output.write("Type yes to apply this rewind: ")
            output.flush()
            if input_stream.readline().strip().casefold() != "yes":
                output.write("Rewind cancelled; no files changed.\n")
                continue
            try:
                session.apply_rewind(checkpoint_id, confirmed=True)
                output.write("Rewind applied.\n")
            except SessionError as exc:
                output.write(f"Session error: {exc}\n")
            continue
        if user_message == "/stats":
            output.write(f"[stats] {format_stats(session.stats)}\n")
            continue
        if user_message == "/compact":
            compaction = session.compact_history()
            status = "completed" if compaction.compacted else "not needed"
            output.write(
                f"Context compact {status}: "
                f"{compaction.before_tokens} -> {compaction.after_tokens} tokens; "
                f"removed={compaction.removed_messages}\n"
            )
            continue
        code = _print_result(session, session.run(user_message), output)
        if code != EXIT_OK:
            return code


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    model: ModelAdapter | None = None,
) -> int:
    """Parse CLI input, assemble dependencies, and return a process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    input_stream = stdin or sys.stdin
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    env = os.environ if environment is None else environment

    if (
        not args.interactive
        and not args.headless
        and not args.prompt
        and not args.demo
        and not args.list_sessions
    ):
        parser.print_help(file=output)
        return EXIT_OK
    if args.interactive and args.prompt:
        error_output.write(
            "Configuration error: interactive mode does not accept a prompt\n"
        )
        return EXIT_USAGE_ERROR
    if args.allow_mutations and args.interactive:
        error_output.write(
            "Configuration error: --allow-mutations is only valid in Headless mode\n"
        )
        return EXIT_USAGE_ERROR
    if args.list_sessions and (
        args.prompt or args.interactive or args.headless or args.demo or args.resume
    ):
        error_output.write(
            "Configuration error: --list-sessions cannot run a model request\n"
        )
        return EXIT_USAGE_ERROR

    try:
        workspace = _workspace(args.cwd)
        if args.list_sessions:
            records = SessionStore(workspace).list()
            if not records:
                output.write("No saved sessions.\n")
            for record in records:
                active = sum(item.rewound_at is None for item in record.checkpoints)
                output.write(
                    f"{record.session_id} turns={record.stats.turns} checkpoints={active}\n"
                )
            return EXIT_OK
        runtime_settings = _runtime_settings(args, env)
        selected_model = _select_model(
            demo=args.demo,
            environment=env,
            injected=model,
        )
        if args.interactive:
            permission_prompt = make_permission_prompt(input_stream, output)
        elif args.allow_mutations:
            error_output.write(
                "WARNING: --allow-mutations approves file and command changes "
                "for this Headless run.\n"
            )
            permission_prompt = lambda _request: PermissionDecision.ALLOW_ONCE
        else:
            permission_prompt = None
        session = build_session(
            model=selected_model,
            workspace=workspace,
            settings=runtime_settings,
            output=output,
            permission_prompt=permission_prompt,
            resume=args.resume,
        )
        if args.interactive:
            return _run_interactive(
                session=session,
                input_stream=input_stream,
                output=output,
            )
        return _run_headless(
            args=args,
            session=session,
            input_stream=input_stream,
            output=output,
        )
    except ModelConfigurationError as exc:
        error_output.write(f"Configuration error: {exc}\n")
        return EXIT_USAGE_ERROR
    except SessionError as exc:
        error_output.write(f"Session error: {exc}\n")
        return EXIT_USAGE_ERROR
    except KeyboardInterrupt:
        error_output.write("\nInterrupted by user. Exiting safely.\n")
        return EXIT_INTERRUPTED
