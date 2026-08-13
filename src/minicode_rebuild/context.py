"""Deterministic context budgeting, tool trimming, and structured compaction."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from minicode_rebuild.core import Message, MessageRole

CONTEXT_SUMMARY_PREFIX = "[Context summary]\n"
TOOL_COMPACTION_MARKER = "\n... [tool output compacted] ...\n"
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def estimate_text_tokens(text: str) -> int:
    """Estimate text tokens with a small CJK-aware deterministic heuristic."""

    if not text:
        return 0
    cjk_count = len(_CJK_PATTERN.findall(text))
    other_count = len(text) - cjk_count
    return max(1, math.ceil(cjk_count / 1.5 + other_count / 4))


def estimate_message_tokens(message: Message) -> int:
    """Estimate one normalized message including protocol overhead."""

    role_overhead = {
        MessageRole.SYSTEM: 3,
        MessageRole.USER: 4,
        MessageRole.ASSISTANT: 3,
        MessageRole.TOOL: 6,
    }[message.role]
    total = role_overhead + estimate_text_tokens(message.content)
    for call in message.tool_calls:
        arguments = json.dumps(
            dict(call.arguments), ensure_ascii=False, sort_keys=True
        )
        total += 7 + estimate_text_tokens(call.id)
        total += estimate_text_tokens(call.name) + estimate_text_tokens(arguments)
    if message.tool_call_id:
        total += estimate_text_tokens(message.tool_call_id)
    return total


def estimate_messages_tokens(messages: Iterable[Message]) -> int:
    """Estimate the total tokens for normalized messages."""

    return sum(estimate_message_tokens(message) for message in messages)


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """Validated budgets used by one context manager."""

    max_tokens: int = 16_000
    trigger_ratio: float = 0.8
    keep_recent_turns: int = 4
    tool_result_tokens: int = 1_500
    summary_tokens: int = 1_200

    def __post_init__(self) -> None:
        for name in (
            "max_tokens",
            "keep_recent_turns",
            "tool_result_tokens",
            "summary_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be greater than zero")
        if isinstance(self.trigger_ratio, bool) or not isinstance(
            self.trigger_ratio, (int, float)
        ):
            raise TypeError("trigger_ratio must be a number")
        if not 0 < float(self.trigger_ratio) <= 1:
            raise ValueError("trigger_ratio must be greater than zero and at most one")
        object.__setattr__(self, "trigger_ratio", float(self.trigger_ratio))

    @property
    def trigger_tokens(self) -> int:
        return max(1, int(self.max_tokens * self.trigger_ratio))


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Messages plus evidence describing one compaction decision."""

    messages: tuple[Message, ...]
    before_tokens: int
    after_tokens: int
    compacted: bool = False
    removed_messages: int = 0
    tool_messages_trimmed: int = 0
    fallback_used: bool = False
    error: str | None = None


Summarizer = Callable[[tuple[Message, ...], int], str]


def _truncate_evidence(text: str, token_budget: int) -> str:
    if estimate_text_tokens(text) <= token_budget:
        return text
    marker = TOOL_COMPACTION_MARKER
    if estimate_text_tokens(marker) > token_budget:
        marker = "..."
    if estimate_text_tokens(marker) > token_budget:
        return ""

    low, high = 0, len(text)
    best = marker
    while low <= high:
        kept = (low + high) // 2
        head = (kept + 1) // 2
        tail = kept // 2
        candidate = text[:head] + marker + (text[-tail:] if tail else "")
        if estimate_text_tokens(candidate) <= token_budget:
            best = candidate
            low = kept + 1
        else:
            high = kept - 1
    return best


def _trim_tool_message(
    message: Message, token_budget: int
) -> tuple[Message, bool]:
    if message.role is not MessageRole.TOOL:
        return message, False
    if estimate_text_tokens(message.content) <= token_budget:
        return message, False

    try:
        payload = json.loads(message.content)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict) and isinstance(payload.get("output"), str):
        original_output = payload["output"]
        payload["output"] = _truncate_evidence(original_output, token_budget)
        payload["truncated"] = True
        if not isinstance(payload.get("original_length"), int):
            payload["original_length"] = len(original_output)
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    else:
        content = _truncate_evidence(message.content, token_budget)
    return replace(message, content=content), True


def _trim_tool_messages(
    messages: tuple[Message, ...], token_budget: int
) -> tuple[tuple[Message, ...], int]:
    trimmed: list[Message] = []
    count = 0
    for message in messages:
        current, changed = _trim_tool_message(message, token_budget)
        trimmed.append(current)
        count += int(changed)
    return tuple(trimmed), count


def _turn_boundaries(messages: tuple[Message, ...]) -> list[int]:
    return [
        index
        for index, message in enumerate(messages)
        if message.role is MessageRole.USER
    ]


def _split_for_recent_turns(
    messages: tuple[Message, ...], keep_recent_turns: int
) -> tuple[tuple[Message, ...], tuple[Message, ...], tuple[Message, ...]]:
    boundaries = _turn_boundaries(messages)
    if len(boundaries) <= keep_recent_turns:
        return (), (), messages
    keep_from = boundaries[-keep_recent_turns]
    leading_systems = 0
    for message in messages:
        if message.role is not MessageRole.SYSTEM:
            break
        leading_systems += 1
    protected = tuple(
        message
        for message in messages[:leading_systems]
        if not message.content.startswith(CONTEXT_SUMMARY_PREFIX.rstrip())
    )
    removed = (
        *(
            message
            for message in messages[:leading_systems]
            if message not in protected
        ),
        *messages[leading_systems:keep_from],
    )
    return protected, tuple(removed), messages[keep_from:]


def _preview(text: str, limit: int = 180) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _local_summary(messages: tuple[Message, ...], token_budget: int) -> str:
    sections: list[tuple[str, list[str]]] = [
        ("User requests", []),
        ("Assistant conclusions", []),
        ("Tool activity", []),
    ]
    users, assistants, tools = (section[1] for section in sections)
    earlier_summaries: list[str] = []
    for message in messages:
        if (
            message.role is MessageRole.SYSTEM
            and message.content.startswith(CONTEXT_SUMMARY_PREFIX.rstrip())
        ):
            earlier_summaries.append(_preview(message.content, 240))
        elif message.role is MessageRole.USER and message.content.strip():
            users.append(_preview(message.content))
        elif message.role is MessageRole.ASSISTANT:
            if message.content.strip():
                assistants.append(_preview(message.content))
            for call in message.tool_calls:
                tools.append(f"called {call.name} ({call.id})")
        elif message.role is MessageRole.TOOL:
            try:
                payload = json.loads(message.content)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                status = "ok" if payload.get("ok") else payload.get("error_code")
                evidence = _preview(str(payload.get("output", "")), 100)
                tools.append(
                    f"result {message.tool_call_id}: {status}; {evidence}"
                )
            else:
                tools.append(
                    f"result {message.tool_call_id}: {_preview(message.content, 100)}"
                )

    lines = [CONTEXT_SUMMARY_PREFIX.rstrip()]
    if earlier_summaries:
        lines.append("## Earlier summaries")
        for item in earlier_summaries:
            lines.append(f"- {item}")
    for heading, items in sections:
        if not items:
            continue
        lines.append(f"## {heading}")
        for item in items:
            candidate = "\n".join([*lines, f"- {item}"])
            if estimate_text_tokens(candidate) > token_budget:
                break
            lines.append(f"- {item}")
    if len(lines) == 1:
        lines.append("- Earlier context was compacted without textual details.")
    summary = "\n".join(lines)
    if estimate_text_tokens(summary) <= token_budget:
        return summary
    return _truncate_evidence(summary, token_budget)


class ContextManager:
    """Apply tool trimming and bounded structured compaction to messages."""

    def __init__(
        self,
        policy: ContextPolicy | None = None,
        *,
        summarizer: Summarizer | None = None,
    ) -> None:
        self.policy = policy or ContextPolicy()
        self.summarizer = summarizer

    def compact(
        self,
        messages: Iterable[Message],
        *,
        force: bool = False,
    ) -> CompactionResult:
        """Trim tools, then summarize old complete turns when required."""

        original = tuple(messages)
        if any(not isinstance(message, Message) for message in original):
            raise TypeError("messages must contain only Message instances")
        before_tokens = estimate_messages_tokens(original)
        trimmed, trimmed_count = _trim_tool_messages(
            original, self.policy.tool_result_tokens
        )
        trimmed_tokens = estimate_messages_tokens(trimmed)
        should_compact = force or trimmed_tokens >= self.policy.trigger_tokens
        if not should_compact:
            return CompactionResult(
                messages=trimmed,
                before_tokens=before_tokens,
                after_tokens=trimmed_tokens,
                tool_messages_trimmed=trimmed_count,
            )

        protected, removed, recent = _split_for_recent_turns(
            trimmed, self.policy.keep_recent_turns
        )
        if not removed:
            return CompactionResult(
                messages=trimmed,
                before_tokens=before_tokens,
                after_tokens=trimmed_tokens,
                tool_messages_trimmed=trimmed_count,
            )

        fallback_used = False
        error: str | None = None
        summary = ""
        if self.summarizer is not None:
            try:
                summary = self.summarizer(removed, self.policy.summary_tokens).strip()
                if not summary:
                    raise ValueError("summarizer returned an empty summary")
            except Exception as exc:
                fallback_used = True
                error = f"{type(exc).__name__}: {exc}"
        if not summary:
            summary = _local_summary(removed, self.policy.summary_tokens)
        elif not summary.startswith(CONTEXT_SUMMARY_PREFIX.rstrip()):
            summary = CONTEXT_SUMMARY_PREFIX + summary
        if estimate_text_tokens(summary) > self.policy.summary_tokens:
            summary = _truncate_evidence(summary, self.policy.summary_tokens)

        compacted_messages = (
            *protected,
            Message(role=MessageRole.SYSTEM, content=summary),
            *recent,
        )
        return CompactionResult(
            messages=compacted_messages,
            before_tokens=before_tokens,
            after_tokens=estimate_messages_tokens(compacted_messages),
            compacted=True,
            removed_messages=len(removed),
            tool_messages_trimmed=trimmed_count,
            fallback_used=fallback_used,
            error=error,
        )


__all__ = [
    "CONTEXT_SUMMARY_PREFIX",
    "CompactionResult",
    "ContextManager",
    "ContextPolicy",
    "Summarizer",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_text_tokens",
]
