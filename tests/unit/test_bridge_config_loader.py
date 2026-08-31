# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for bridge_config_loader — env-driven model ID resolution."""

from __future__ import annotations

import pytest

from omnimarket.inference.bridge_config_loader import (
    load_inference_bridge_config_from_env,
)


@pytest.fixture(autouse=True)
def _isolate_openrouter_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMN-15048: keep this suite independent of ambient OpenRouter credentials.

    Prior to OMN-15048 the loader read the literal ``OPENROUTER_API_KEY`` (no
    underscore), which no real deployment surface ever sets, so OpenRouter
    registration always silently no-opped here regardless of what was in the
    ambient dev-shell environment. Now that the loader resolves the real
    ``OPEN_ROUTER_API_KEY`` (via the secret store's ``llm.openrouter.api_key``
    alias), a developer's own ``~/.omnibase/.env`` value would otherwise leak
    into these URL-focused tests. Tests that specifically exercise OpenRouter
    registration set the var explicitly after this fixture runs.
    """
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)


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
    # OMN-16492: qwen3-coder-30b is qwen3.8 on .201:8000 via SGLang; live probe
    # 2026-08-23 GET /v1/models -> max_model_len 122880 (was 131072 under the
    # retired Qwen3.6-35B-A3B/vLLM serving, OMN-12492).
    assert cfg.model_configs["qwen3-coder"]["context_window"] == 122_880


@pytest.mark.unit
def test_openrouter_key_resolves_through_the_store_not_the_house_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-17372: OpenRouter registration resolves through the SECRET STORE.

    ASSERTION INVERTED at the exact case it replaces. This test previously
    required registration to succeed from ``OPEN_ROUTER_API_KEY`` — the loader
    passed that name as a literal ``env_var_fallback``, and the assertion
    encoded the premise that OmniNode supplies the model. Under the current
    rule OmniNode does not offer inference and there are no keyless customers
    on the cloud, so an ambient house variable resolving a provider key IS the
    defect: ``env_var_fallback`` is serviced by ``os.environ`` inside the
    resolver AFTER the store lookup, bypassing the lane secret mapping that is
    the only sanctioned path on a deployed lane.

    So the house-variable case must now FAIL to register, and the store path
    must still work. Both halves are asserted — dropping the second would let a
    loader that resolves nothing at all pass.
    """
    for var in (
        "LLM_CODER_URL",
        "LLM_CODER_FAST_URL",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_QWEN3_NEXT_URL",
        "LLM_GLM_URL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    # The deleted house fallback: this variable alone must no longer register
    # a single OpenRouter model.
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-house-underscore-key")
    house_only = load_inference_bridge_config_from_env()
    assert [k for k in house_only.model_configs if k.startswith("openrouter/")] == [], (
        "OPEN_ROUTER_API_KEY still registered OpenRouter models — the house "
        "env-var fallback deleted by OMN-17372 has come back"
    )

    # The surviving path: the ref resolves through the store, which on a local
    # install maps llm.openrouter.api_key onto the provider-native name.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-store-resolved-key")
    cfg = load_inference_bridge_config_from_env()

    openrouter_keys = [k for k in cfg.model_configs if k.startswith("openrouter/")]
    assert openrouter_keys, "expected at least one openrouter/* model registered"
    for key in openrouter_keys:
        assert cfg.model_configs[key]["api_key"] == "sk-store-resolved-key"


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
