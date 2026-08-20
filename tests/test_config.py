"""Tests for model configuration loaded from environment variables."""

from __future__ import annotations

import pytest

from minicode_rebuild.config import (
    DEFAULT_MODEL,
    DEFAULT_OPENAI_BASE_URL,
    ModelConfigurationError,
    ModelSettings,
    RuntimeSettings,
)


def test_settings_use_deepseek_defaults() -> None:
    settings = ModelSettings.from_env({"DEEPSEEK_API_KEY": "test-secret"})

    assert settings.model == DEFAULT_MODEL == "deepseek-v4-pro"
    assert settings.base_url == DEFAULT_OPENAI_BASE_URL == "https://api.deepseek.com"
    assert settings.chat_completions_url == "https://api.deepseek.com/chat/completions"
    assert settings.timeout_seconds == 120


def test_settings_allow_openai_compatible_overrides() -> None:
    settings = ModelSettings.from_env(
        {
            "MINICODE_MODEL": "local-model",
            "OPENAI_BASE_URL": "https://example.test/v1/",
            "OPENAI_API_KEY": "openai-test-secret",
            "DEEPSEEK_API_KEY": "deepseek-test-secret",
            "MINICODE_MODEL_TIMEOUT": "15",
            "MINICODE_FALLBACK_MODELS": "fallback-small, fallback-stable",
        }
    )

    assert settings.model == "local-model"
    assert settings.base_url == "https://example.test/v1"
    assert settings.api_key == "openai-test-secret"
    assert settings.chat_completions_url == "https://example.test/v1/chat/completions"
    assert settings.timeout_seconds == 15
    assert settings.fallback_models == ("fallback-small", "fallback-stable")
    assert settings.model_candidates == (
        "local-model",
        "fallback-small",
        "fallback-stable",
    )


def test_settings_accept_full_chat_completions_url() -> None:
    settings = ModelSettings.from_env(
        {
            "OPENAI_BASE_URL": "https://example.test/v1/chat/completions",
            "OPENAI_API_KEY": "test-secret",
        }
    )

    assert settings.chat_completions_url == "https://example.test/v1/chat/completions"


def test_settings_require_api_key() -> None:
    with pytest.raises(ModelConfigurationError, match="OPENAI_API_KEY"):
        ModelSettings.from_env({})


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("OPENAI_BASE_URL", "file:///tmp/model", "http"),
        ("MINICODE_MODEL_TIMEOUT", "zero", "integer"),
        ("MINICODE_MODEL_TIMEOUT", "0", "greater than zero"),
    ],
)
def test_settings_reject_invalid_values(
    variable: str,
    value: str,
    message: str,
) -> None:
    environment = {
        "OPENAI_API_KEY": "test-secret",
        variable: value,
    }

    with pytest.raises(ModelConfigurationError, match=message):
        ModelSettings.from_env(environment)


def test_settings_repr_does_not_expose_api_key() -> None:
    settings = ModelSettings.from_env({"OPENAI_API_KEY": "top-secret-value"})

    assert "top-secret-value" not in repr(settings)


@pytest.mark.parametrize(
    "fallbacks",
    [
        "primary",
        "secondary,secondary",
        "secondary,,tertiary",
        "one,two,three,four,five",
        "secondary\nforged",
        "secondary\x1b[31m",
        "x" * 257,
    ],
)
def test_settings_reject_invalid_fallback_models(fallbacks: str) -> None:
    with pytest.raises(ModelConfigurationError, match="MINICODE_FALLBACK_MODELS"):
        ModelSettings.from_env(
            {
                "OPENAI_API_KEY": "test-secret",
                "MINICODE_MODEL": "primary",
                "MINICODE_FALLBACK_MODELS": fallbacks,
            }
        )


def test_settings_constructor_rejects_string_fallback_collection() -> None:
    with pytest.raises(ModelConfigurationError, match="fallback_models"):
        ModelSettings(
            model="primary",
            base_url="https://example.test",
            api_key="secret",
            fallback_models="secondary",  # type: ignore[arg-type]
        )


def test_settings_reject_control_characters_in_primary_model() -> None:
    with pytest.raises(ModelConfigurationError, match="control characters"):
        ModelSettings.from_env(
            {
                "OPENAI_API_KEY": "test-secret",
                "MINICODE_MODEL": "primary\nforged",
            }
        )


def test_settings_reject_base_url_credentials() -> None:
    with pytest.raises(ModelConfigurationError, match="embedded credentials"):
        ModelSettings.from_env(
            {
                "OPENAI_API_KEY": "test-secret",
                "OPENAI_BASE_URL": "https://user:password@example.test/v1",
            }
        )


def test_runtime_settings_load_cli_environment() -> None:
    settings = RuntimeSettings.from_env(
        {
            "MINICODE_MAX_STEPS": "7",
            "MINICODE_SYSTEM_PROMPT": "  Be careful  ",
            "MINICODE_CONTEXT_TOKENS": "9000",
            "MINICODE_CONTEXT_TRIGGER": "0.75",
            "MINICODE_KEEP_RECENT_TURNS": "3",
            "MINICODE_TOOL_RESULT_TOKENS": "700",
            "MINICODE_SUMMARY_TOKENS": "500",
            "MINICODE_SESSION_TOKEN_BUDGET": "12000",
            "MINICODE_MAX_OUTPUT_TOKENS": "800",
        }
    )

    assert settings.max_steps == 7
    assert settings.system_prompt == "Be careful"
    assert settings.context_policy.max_tokens == 9000
    assert settings.context_policy.trigger_ratio == 0.75
    assert settings.context_policy.keep_recent_turns == 3
    assert settings.context_policy.tool_result_tokens == 700
    assert settings.context_policy.summary_tokens == 500
    assert settings.token_budget_policy.session_tokens == 12000
    assert settings.token_budget_policy.max_output_tokens == 800


@pytest.mark.parametrize("value", ["zero", "0", "-1"])
def test_runtime_settings_reject_invalid_max_steps(value: str) -> None:
    with pytest.raises(ModelConfigurationError, match="MINICODE_MAX_STEPS"):
        RuntimeSettings.from_env({"MINICODE_MAX_STEPS": value})


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("MINICODE_CONTEXT_TOKENS", "0"),
        ("MINICODE_CONTEXT_TRIGGER", "1.1"),
        ("MINICODE_KEEP_RECENT_TURNS", "0"),
        ("MINICODE_TOOL_RESULT_TOKENS", "bad"),
        ("MINICODE_SUMMARY_TOKENS", "-1"),
    ],
)
def test_runtime_settings_reject_invalid_context_policy(
    variable: str,
    value: str,
) -> None:
    with pytest.raises(ModelConfigurationError, match=variable):
        RuntimeSettings.from_env({variable: value})


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("MINICODE_SESSION_TOKEN_BUDGET", "0"),
        ("MINICODE_SESSION_TOKEN_BUDGET", "bad"),
        ("MINICODE_MAX_OUTPUT_TOKENS", "-1"),
    ],
)
def test_runtime_settings_reject_invalid_token_budget(
    variable: str,
    value: str,
) -> None:
    with pytest.raises(ModelConfigurationError, match=variable):
        RuntimeSettings.from_env({variable: value})
