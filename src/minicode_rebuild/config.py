"""Environment-based configuration for model adapters."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

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

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int):
            raise ModelConfigurationError("MINICODE_MAX_STEPS must be an integer")
        if self.max_steps < 1:
            raise ModelConfigurationError(
                "MINICODE_MAX_STEPS must be greater than zero"
            )
        if not isinstance(self.system_prompt, str):
            raise ModelConfigurationError("MINICODE_SYSTEM_PROMPT must be text")
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
        return cls(
            max_steps=max_steps,
            system_prompt=env.get("MINICODE_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        )
