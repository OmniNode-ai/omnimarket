# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for bridge_config_loader — env-driven model ID resolution."""

from __future__ import annotations

import pytest

from omnimarket.inference.bridge_config_loader import (
    load_inference_bridge_config_from_env,
)


@pytest.mark.unit
def test_empty_env_produces_empty_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LLM_CODER_URL",
        "LLM_CODER_FAST_URL",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_QWEN3_NEXT_URL",
        "LLM_GLM_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_inference_bridge_config_from_env()
    assert cfg.model_configs == {}


@pytest.mark.unit
def test_coder_url_set_includes_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
    monkeypatch.setenv("LLM_CODER_MODEL_NAME", "my-coder-model")
    for var in (
        "LLM_CODER_FAST_URL",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_QWEN3_NEXT_URL",
        "LLM_GLM_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_inference_bridge_config_from_env()
    assert "qwen3-coder" in cfg.model_configs
    assert cfg.model_configs["qwen3-coder"]["base_url"] == "http://localhost:8000"
    assert cfg.model_configs["qwen3-coder"]["model_id"] == "my-coder-model"


@pytest.mark.unit
def test_model_id_empty_string_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
    monkeypatch.delenv("LLM_CODER_MODEL_NAME", raising=False)
    for var in (
        "LLM_CODER_FAST_URL",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_QWEN3_NEXT_URL",
        "LLM_GLM_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_inference_bridge_config_from_env()
    assert cfg.model_configs["qwen3-coder"]["model_id"] == ""


@pytest.mark.unit
def test_multiple_keys_registered_when_urls_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
    monkeypatch.setenv("LLM_CODER_MODEL_NAME", "model-a")
    monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://localhost:8101")
    monkeypatch.setenv("LLM_DEEPSEEK_R1_MODEL_NAME", "model-b")
    for var in ("LLM_CODER_FAST_URL", "LLM_QWEN3_NEXT_URL", "LLM_GLM_URL"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_inference_bridge_config_from_env()
    assert set(cfg.model_configs) == {"qwen3-coder", "deepseek-r1"}


@pytest.mark.unit
def test_glm_api_key_included_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_GLM_URL", "https://api.z.ai")
    monkeypatch.setenv("LLM_GLM_MODEL_NAME", "glm-4.5")
    monkeypatch.setenv("LLM_GLM_API_KEY", "secret-key")
    for var in (
        "LLM_CODER_URL",
        "LLM_CODER_FAST_URL",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_QWEN3_NEXT_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_inference_bridge_config_from_env()
    assert cfg.model_configs["glm"]["api_key"] == "secret-key"


@pytest.mark.unit
def test_glm_api_key_absent_when_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_GLM_URL", "https://api.z.ai")
    monkeypatch.setenv("LLM_GLM_MODEL_NAME", "glm-4.5")
    monkeypatch.delenv("LLM_GLM_API_KEY", raising=False)
    for var in (
        "LLM_CODER_URL",
        "LLM_CODER_FAST_URL",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_QWEN3_NEXT_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_inference_bridge_config_from_env()
    assert "api_key" not in cfg.model_configs["glm"]


@pytest.mark.unit
def test_context_window_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
    monkeypatch.setenv("LLM_CODER_MODEL_NAME", "some-model")
    for var in (
        "LLM_CODER_FAST_URL",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_QWEN3_NEXT_URL",
        "LLM_GLM_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_inference_bridge_config_from_env()
    assert cfg.model_configs["qwen3-coder"]["context_window"] == 112_000


@pytest.mark.unit
def test_transport_is_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
    monkeypatch.setenv("LLM_CODER_MODEL_NAME", "some-model")
    for var in (
        "LLM_CODER_FAST_URL",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_QWEN3_NEXT_URL",
        "LLM_GLM_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_inference_bridge_config_from_env()
    assert cfg.model_configs["qwen3-coder"]["transport"] == "http"
