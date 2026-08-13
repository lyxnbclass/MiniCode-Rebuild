from __future__ import annotations

import json

from minicode_rebuild.context import (
    CONTEXT_SUMMARY_PREFIX,
    CompactionResult,
    ContextManager,
    ContextPolicy,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
)
from minicode_rebuild.core import Message, MessageRole, ToolCall


def conversation(turns: int, *, width: int = 80) -> tuple[Message, ...]:
    messages: list[Message] = []
    for index in range(turns):
        messages.extend(
            [
                Message(
                    role=MessageRole.USER,
                    content=f"request-{index} " + "u" * width,
                ),
                Message(
                    role=MessageRole.ASSISTANT,
                    content=f"answer-{index} " + "a" * width,
                ),
            ]
        )
    return tuple(messages)


def test_token_estimates_are_deterministic_and_cjk_aware() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("abcdefgh") == estimate_text_tokens("abcdefgh")
    assert estimate_text_tokens("中文中文中文中文") > estimate_text_tokens("abcdefgh")

    call_message = Message(
        role=MessageRole.ASSISTANT,
        tool_calls=(
            ToolCall(id="call-1", name="read_file", arguments={"path": "a.py"}),
        ),
    )
    assert estimate_message_tokens(call_message) > 0
    assert estimate_messages_tokens((call_message,)) == estimate_message_tokens(
        call_message
    )


def test_tool_result_trimming_preserves_json_protocol_and_evidence() -> None:
    payload = {
        "ok": True,
        "output": "HEAD-" + "x" * 4000 + "-TAIL",
        "error_code": None,
        "truncated": False,
        "original_length": None,
    }
    message = Message(
        role=MessageRole.TOOL,
        content=json.dumps(payload),
        tool_call_id="call-1",
    )
    manager = ContextManager(
        ContextPolicy(max_tokens=500, tool_result_tokens=80)
    )

    result = manager.compact((message,), force=False)

    assert result.tool_messages_trimmed == 1
    trimmed = result.messages[0]
    decoded = json.loads(trimmed.content)
    assert trimmed.tool_call_id == "call-1"
    assert decoded["ok"] is True
    assert decoded["truncated"] is True
    assert decoded["original_length"] == len(payload["output"])
    assert "HEAD-" in decoded["output"]
    assert "-TAIL" in decoded["output"]
    assert "tool output compacted" in decoded["output"]
    assert estimate_text_tokens(decoded["output"]) <= 80


def test_cjk_tool_result_trimming_honors_token_budget() -> None:
    payload = {
        "ok": True,
        "output": "开头" + "中文内容" * 1000 + "结尾",
        "error_code": None,
        "truncated": False,
        "original_length": None,
    }
    message = Message(
        role=MessageRole.TOOL,
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id="call-cjk",
    )
    manager = ContextManager(
        ContextPolicy(max_tokens=500, tool_result_tokens=40)
    )

    result = manager.compact((message,))
    decoded = json.loads(result.messages[0].content)

    assert result.tool_messages_trimmed == 1
    assert estimate_text_tokens(decoded["output"]) <= 40
    assert decoded["output"].startswith("开头")
    assert decoded["output"].endswith("结尾")


def test_auto_compaction_waits_for_threshold() -> None:
    manager = ContextManager(
        ContextPolicy(max_tokens=2000, trigger_ratio=0.8, keep_recent_turns=1)
    )

    result = manager.compact(conversation(2, width=10), force=False)

    assert result.compacted is False
    assert result.removed_messages == 0


def test_compaction_summarizes_old_turns_and_keeps_recent_complete_turns() -> None:
    messages = conversation(5, width=180)
    manager = ContextManager(
        ContextPolicy(
            max_tokens=300,
            trigger_ratio=0.5,
            keep_recent_turns=2,
            summary_tokens=100,
        )
    )

    result = manager.compact(messages, force=False)

    assert result.compacted is True
    assert result.removed_messages == 6
    assert result.messages[0].role is MessageRole.SYSTEM
    assert result.messages[0].content.startswith(CONTEXT_SUMMARY_PREFIX)
    assert "User requests" in result.messages[0].content
    assert "Assistant conclusions" in result.messages[0].content
    assert [message.content.split()[0] for message in result.messages[1:]] == [
        "request-3",
        "answer-3",
        "request-4",
        "answer-4",
    ]
    assert result.after_tokens < result.before_tokens


def test_tool_call_and_result_pair_stay_together_in_recent_turn() -> None:
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "a.py"})
    recent = (
        Message(role=MessageRole.USER, content="inspect a.py"),
        Message(role=MessageRole.ASSISTANT, tool_calls=(call,)),
        Message(role=MessageRole.TOOL, content="{}", tool_call_id="call-1"),
        Message(role=MessageRole.ASSISTANT, content="done"),
    )
    manager = ContextManager(
        ContextPolicy(max_tokens=100, trigger_ratio=0.1, keep_recent_turns=1)
    )

    result = manager.compact((*conversation(2), *recent), force=True)

    assert result.messages[-4:] == recent


def test_custom_summarizer_failure_uses_local_fallback() -> None:
    def fail(_messages: tuple[Message, ...], _budget: int) -> str:
        raise RuntimeError("summary provider failed")

    manager = ContextManager(
        ContextPolicy(max_tokens=100, keep_recent_turns=1),
        summarizer=fail,
    )

    result = manager.compact(conversation(3), force=True)

    assert result.compacted is True
    assert result.fallback_used is True
    assert result.error == "RuntimeError: summary provider failed"
    assert "request-0" in result.messages[0].content
    assert result.messages[-2:] == conversation(3)[-2:]


def test_custom_summary_is_bounded_even_when_summarizer_ignores_budget() -> None:
    manager = ContextManager(
        ContextPolicy(
            max_tokens=100,
            keep_recent_turns=1,
            summary_tokens=30,
        ),
        summarizer=lambda _messages, _budget: "summary " + "x" * 2000,
    )

    result = manager.compact(conversation(3), force=True)

    assert result.compacted is True
    assert estimate_text_tokens(result.messages[0].content) <= 30
    assert "compacted" in result.messages[0].content


def test_manual_force_compaction_works_below_automatic_threshold() -> None:
    manager = ContextManager(
        ContextPolicy(max_tokens=10_000, trigger_ratio=0.9, keep_recent_turns=1)
    )

    automatic = manager.compact(conversation(3), force=False)
    manual = manager.compact(conversation(3), force=True)

    assert automatic.compacted is False
    assert manual.compacted is True
    assert isinstance(manual, CompactionResult)


def test_compaction_preserves_primary_system_prompt() -> None:
    primary = Message(role=MessageRole.SYSTEM, content="Never bypass permissions")
    manager = ContextManager(
        ContextPolicy(max_tokens=100, trigger_ratio=0.1, keep_recent_turns=1)
    )

    result = manager.compact((primary, *conversation(3)), force=True)

    assert result.messages[0] == primary
    assert result.messages[1].content.startswith(CONTEXT_SUMMARY_PREFIX)


def test_recompaction_carries_forward_earlier_summary() -> None:
    prior = Message(
        role=MessageRole.SYSTEM,
        content=CONTEXT_SUMMARY_PREFIX + "Important earlier decision",
    )
    manager = ContextManager(
        ContextPolicy(max_tokens=100, trigger_ratio=0.1, keep_recent_turns=1)
    )

    result = manager.compact((prior, *conversation(3)), force=True)

    assert result.messages[0].content.startswith(CONTEXT_SUMMARY_PREFIX)
    assert "Important earlier decision" in result.messages[0].content
