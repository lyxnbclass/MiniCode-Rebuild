"""Environment-based configuration for model adapters."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from minicode_rebuild.context import ContextPolicy
from minicode_rebuild.cost import TokenBudgetPolicy

DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_OPENAI_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL_TIMEOUT_SECONDS = 120
DEFAULT_MAX_STEPS = 12
DEFAULT_SYSTEM_PROMPT = (
    "You are a careful local coding assistant. Inspect the workspace with tools "
    "before making claims, and ask for permission before mutations."
)


class ModelConfigurationError(ValueError):
    """Raised when model settings are missing or invalid."""


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Validated settings for an OpenAI-compatible model endpoint."""

    model: str
    base_url: str
    api_key: str = field(repr=False)
    timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        model = self.model.strip()
        base_url = self.base_url.rstrip("/")
        api_key = self.api_key.strip()

        if not model:
            raise ModelConfigurationError("MINICODE_MODEL must not be empty")
        if not api_key:
            raise ModelConfigurationError(
                "Set OPENAI_API_KEY or DEEPSEEK_API_KEY before using the real model adapter"
            )
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ModelConfigurationError("OPENAI_BASE_URL must be an http(s) URL")
        if parsed_url.query or parsed_url.fragment:
            raise ModelConfigurationError(
                "OPENAI_BASE_URL must not contain a query string or fragment"
            )
        if self.timeout_seconds <= 0:
            raise ModelConfigurationError(
                "MINICODE_MODEL_TIMEOUT must be greater than zero"
            )

        object.__setattr__(self, "model", model)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "api_key", api_key)

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> ModelSettings:
        """Load settings from a supplied mapping or the process environment."""
        env = os.environ if environment is None else environment
        timeout_text = env.get(
            "MINICODE_MODEL_TIMEOUT",
            str(DEFAULT_MODEL_TIMEOUT_SECONDS),
        ).strip()
        try:
            timeout_seconds = int(timeout_text)
        except ValueError as error:
            raise ModelConfigurationError(
                "MINICODE_MODEL_TIMEOUT must be an integer"
            ) from error

        return cls(
            model=env.get("MINICODE_MODEL", DEFAULT_MODEL),
            base_url=env.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
            api_key=env.get("OPENAI_API_KEY")
            or env.get("DEEPSEEK_API_KEY", ""),
            timeout_seconds=timeout_seconds,
        )

    @property
    def chat_completions_url(self) -> str:
        """Return the full Chat Completions endpoint."""
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Validated settings owned by the CLI and agent runtime."""

    max_steps: int = DEFAULT_MAX_STEPS
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    context_policy: ContextPolicy = field(default_factory=ContextPolicy)
    token_budget_policy: TokenBudgetPolicy = field(default_factory=TokenBudgetPolicy)

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int):
            raise ModelConfigurationError("MINICODE_MAX_STEPS must be an integer")
        if self.max_steps < 1:
            raise ModelConfigurationError(
                "MINICODE_MAX_STEPS must be greater than zero"
            )
        if not isinstance(self.system_prompt, str):
            raise ModelConfigurationError("MINICODE_SYSTEM_PROMPT must be text")
        if not isinstance(self.context_policy, ContextPolicy):
            raise ModelConfigurationError("context_policy must be a ContextPolicy")
        if not isinstance(self.token_budget_policy, TokenBudgetPolicy):
            raise ModelConfigurationError(
                "token_budget_policy must be a TokenBudgetPolicy"
            )
        object.__setattr__(self, "system_prompt", self.system_prompt.strip())

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> RuntimeSettings:
        """Load CLI runtime controls without requiring a model API key."""

        env = os.environ if environment is None else environment
        raw_steps = env.get("MINICODE_MAX_STEPS", str(DEFAULT_MAX_STEPS)).strip()
        try:
            max_steps = int(raw_steps)
        except ValueError as error:
            raise ModelConfigurationError(
                "MINICODE_MAX_STEPS must be an integer"
            ) from error

        def integer(name: str, default: int) -> int:
            raw = env.get(name, str(default)).strip()
            try:
                value = int(raw)
            except ValueError as error:
                raise ModelConfigurationError(f"{name} must be an integer") from error
            if value < 1:
                raise ModelConfigurationError(
                    f"{name} must be greater than zero"
                )
            return value

        def optional_integer(name: str) -> int | None:
            raw = env.get(name, "").strip()
            if not raw:
                return None
            try:
                value = int(raw)
            except ValueError as error:
                raise ModelConfigurationError(f"{name} must be an integer") from error
            if value < 1:
                raise ModelConfigurationError(
                    f"{name} must be greater than zero"
                )
            return value

        trigger_name = "MINICODE_CONTEXT_TRIGGER"
        trigger_raw = env.get(
            trigger_name, str(ContextPolicy().trigger_ratio)
        ).strip()
        try:
            trigger = float(trigger_raw)
        except ValueError as error:
            raise ModelConfigurationError(
                f"{trigger_name} must be a number"
            ) from error
        if not 0 < trigger <= 1:
            raise ModelConfigurationError(
                f"{trigger_name} must be greater than zero and at most one"
            )

        return cls(
            max_steps=max_steps,
            system_prompt=env.get("MINICODE_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            context_policy=ContextPolicy(
                max_tokens=integer(
                    "MINICODE_CONTEXT_TOKENS", ContextPolicy().max_tokens
                ),
                trigger_ratio=trigger,
                keep_recent_turns=integer(
                    "MINICODE_KEEP_RECENT_TURNS",
                    ContextPolicy().keep_recent_turns,
                ),
                tool_result_tokens=integer(
                    "MINICODE_TOOL_RESULT_TOKENS",
                    ContextPolicy().tool_result_tokens,
                ),
                summary_tokens=integer(
                    "MINICODE_SUMMARY_TOKENS", ContextPolicy().summary_tokens
                ),
            ),
            token_budget_policy=TokenBudgetPolicy(
                session_tokens=optional_integer("MINICODE_SESSION_TOKEN_BUDGET"),
                max_output_tokens=optional_integer("MINICODE_MAX_OUTPUT_TOKENS"),
            ),
        )
