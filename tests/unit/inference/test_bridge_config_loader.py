# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for bridge_config_loader — Settings-based and env-var paths.

OMN-10591 Task 19. Coverage:
- Settings path resolves specific keys (qwen3-coder, glm) from Settings fields
- Settings path skips keys with empty URL
- Settings path skips keys with empty model_id
- ConfigError raised with named missing keys on validate_keys
- Env-var path resolves keys from os.environ
- Env-var path skips entries with missing URL env var
- Env-var path skips entries with missing model-id env var
- GLM api_key picked up when LLM_GLM_API_KEY is set (env path)
- GLM api_key picked up from settings.llm_glm_api_key (settings path)
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from omnimarket.config.settings import Settings
from omnimarket.inference.bridge_config_loader import (
    ConfigError,
    load_inference_bridge_config,
    load_inference_bridge_config_from_env,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides: object) -> Settings:
    """Construct a Settings with all LLM fields empty except supplied overrides."""
    base: dict[str, object] = {
        "llm_coder_url": "",
        "llm_coder_model_id": "",
        "llm_coder_fast_url": "",
        "llm_coder_fast_model_id": "",
        "llm_reasoner_url": "",
        "llm_reasoner_model_id": "",
        "llm_glm_url": "",
        "llm_glm_model_name": "",
        "llm_glm_api_key": SecretStr(""),
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Settings path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_settings_path_resolves_coder_key() -> None:
    """qwen3-coder key is populated when llm_coder_url and llm_coder_model_id are set."""
    settings = _make_settings(
        llm_coder_url="http://localhost:8000",
        llm_coder_model_id="test-coder-model",
    )
    cfg = load_inference_bridge_config(settings)

    assert "qwen3-coder" in cfg.model_configs
    entry = cfg.model_configs["qwen3-coder"]
    assert entry["base_url"] == "http://localhost:8000"
    assert entry["model_id"] == "test-coder-model"
    assert entry["transport"] == "http"
    assert entry["context_window"] == 112_000


@pytest.mark.unit
def test_settings_path_skips_key_when_url_empty() -> None:
    """Keys with empty URL are excluded from model_configs."""
    settings = _make_settings(
        llm_coder_url="",
        llm_coder_model_id="some-model",
    )
    cfg = load_inference_bridge_config(settings)
    assert "qwen3-coder" not in cfg.model_configs


@pytest.mark.unit
def test_settings_path_skips_key_when_model_id_empty() -> None:
    """Keys with empty model_id are excluded even when URL is set."""
    settings = _make_settings(
        llm_coder_url="http://localhost:8000",
        llm_coder_model_id="",
    )
    cfg = load_inference_bridge_config(settings)
    assert "qwen3-coder" not in cfg.model_configs


@pytest.mark.unit
def test_settings_path_glm_api_key_included() -> None:
    """GLM api_key appears in model_configs when llm_glm_api_key is set."""
    settings = _make_settings(
        llm_glm_url="https://api.z.ai",
        llm_glm_model_name="glm-4.5",
        llm_glm_api_key=SecretStr("secret-key-123"),
    )
    cfg = load_inference_bridge_config(settings)

    assert "glm" in cfg.model_configs
    assert cfg.model_configs["glm"]["api_key"] == "secret-key-123"


@pytest.mark.unit
def test_settings_path_glm_api_key_omitted_when_empty() -> None:
    """GLM api_key is NOT in model_configs when llm_glm_api_key is empty."""
    settings = _make_settings(
        llm_glm_url="https://api.z.ai",
        llm_glm_model_name="glm-4.5",
        llm_glm_api_key=SecretStr(""),
    )
    cfg = load_inference_bridge_config(settings)
    assert "glm" in cfg.model_configs
    assert "api_key" not in cfg.model_configs["glm"]


@pytest.mark.unit
def test_settings_path_validate_keys_raises_config_error_with_named_keys() -> None:
    """ConfigError is raised naming each missing key when validate_keys is used."""
    settings = _make_settings()  # all empty — nothing resolves

    with pytest.raises(ConfigError) as exc_info:
        load_inference_bridge_config(settings, validate_keys=["qwen3-coder", "glm"])

    err = exc_info.value
    assert "qwen3-coder" in err.missing_keys
    assert "glm" in err.missing_keys
    assert "qwen3-coder" in str(err)
    assert "glm" in str(err)


@pytest.mark.unit
def test_settings_path_validate_keys_no_error_when_all_present() -> None:
    """No ConfigError when all requested keys resolve successfully."""
    settings = _make_settings(
        llm_coder_url="http://localhost:8000",
        llm_coder_model_id="my-coder",
    )
    cfg = load_inference_bridge_config(settings, validate_keys=["qwen3-coder"])
    assert "qwen3-coder" in cfg.model_configs


@pytest.mark.unit
def test_settings_path_empty_settings_returns_empty_model_configs() -> None:
    """Empty settings produces an empty model_configs dict without raising."""
    settings = _make_settings()
    cfg = load_inference_bridge_config(settings)
    assert cfg.model_configs == {}


# ---------------------------------------------------------------------------
# Env-var path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_path_resolves_coder_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """qwen3-coder key is populated when LLM_CODER_URL and LLM_CODER_MODEL_NAME are set."""
    monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
    monkeypatch.setenv("LLM_CODER_MODEL_NAME", "env-coder-model")

    cfg = load_inference_bridge_config_from_env()

    assert "qwen3-coder" in cfg.model_configs
    entry = cfg.model_configs["qwen3-coder"]
    assert entry["base_url"] == "http://localhost:8000"
    assert entry["model_id"] == "env-coder-model"


@pytest.mark.unit
def test_env_path_skips_key_when_url_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keys whose URL env var is absent are excluded from model_configs."""
    monkeypatch.delenv("LLM_CODER_URL", raising=False)
    monkeypatch.setenv("LLM_CODER_MODEL_NAME", "some-model")

    cfg = load_inference_bridge_config_from_env()
    assert "qwen3-coder" not in cfg.model_configs


@pytest.mark.unit
def test_env_path_skips_key_when_model_name_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keys whose model-id env var is absent are excluded even when URL is set."""
    monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
    monkeypatch.delenv("LLM_CODER_MODEL_NAME", raising=False)

    cfg = load_inference_bridge_config_from_env()
    assert "qwen3-coder" not in cfg.model_configs


@pytest.mark.unit
def test_env_path_glm_api_key_included(monkeypatch: pytest.MonkeyPatch) -> None:
    """GLM api_key appears when LLM_GLM_API_KEY is set alongside URL+model."""
    monkeypatch.setenv("LLM_GLM_URL", "https://api.z.ai")
    monkeypatch.setenv("LLM_GLM_MODEL_NAME", "glm-4.5")
    monkeypatch.setenv("LLM_GLM_API_KEY", "env-secret")

    cfg = load_inference_bridge_config_from_env()

    assert "glm" in cfg.model_configs
    assert cfg.model_configs["glm"]["api_key"] == "env-secret"


@pytest.mark.unit
def test_env_path_empty_env_returns_empty_model_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No LLM env vars set → empty model_configs without raising."""
    for var in (
        "LLM_CODER_URL",
        "LLM_CODER_MODEL_NAME",
        "LLM_CODER_FAST_URL",
        "LLM_CODER_FAST_MODEL_NAME",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_DEEPSEEK_R1_MODEL_NAME",
        "LLM_QWEN3_NEXT_URL",
        "LLM_QWEN3_NEXT_MODEL_NAME",
        "LLM_GLM_URL",
        "LLM_GLM_MODEL_NAME",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = load_inference_bridge_config_from_env()
    assert cfg.model_configs == {}
