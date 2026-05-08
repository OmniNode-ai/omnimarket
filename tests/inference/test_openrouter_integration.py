# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for OpenRouter bridge integration (OMN-10692)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from omnimarket.inference.openrouter_models import (
    EnumModelAvailability,
    EnumOpenRouterTier,
    get_openrouter_models,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceBridgeConfig,
)

# --- openrouter_models unit tests ---


@pytest.mark.unit
def test_get_openrouter_models_returns_all():
    models = get_openrouter_models()
    assert len(models) == 12


@pytest.mark.unit
def test_get_openrouter_models_filter_small():
    small = get_openrouter_models(tier=EnumOpenRouterTier.SMALL_FREE)
    assert len(small) == 6
    assert all(m.tier == EnumOpenRouterTier.SMALL_FREE for m in small)


@pytest.mark.unit
def test_get_openrouter_models_filter_large():
    large = get_openrouter_models(tier=EnumOpenRouterTier.LARGE_FREE)
    assert len(large) == 6
    assert all(m.tier == EnumOpenRouterTier.LARGE_FREE for m in large)


@pytest.mark.unit
def test_all_available_models_have_free_suffix():
    for model in get_openrouter_models():
        if model.availability == EnumModelAvailability.AVAILABLE:
            assert model.model_id.endswith(":free"), (
                f"{model.display_name}: model_id must end with :free, got {model.model_id!r}"
            )


@pytest.mark.unit
def test_model_config_is_frozen():
    model = get_openrouter_models()[0]
    with pytest.raises(ValidationError):
        model.model_id = "changed"  # type: ignore[misc]


# --- bridge_config_loader OpenRouter registration ---


@pytest.mark.unit
def test_openrouter_models_registered_when_api_key_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-abc")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

    from omnimarket.inference.bridge_config_loader import (
        load_inference_bridge_config_from_env,
    )

    cfg = load_inference_bridge_config_from_env()

    openrouter_keys = [k for k in cfg.model_configs if k.startswith("openrouter/")]
    assert len(openrouter_keys) == 12

    sample_key = openrouter_keys[0]
    entry = cfg.model_configs[sample_key]
    assert entry["base_url"] == "https://openrouter.ai/api"
    assert entry["api_key"] == "test-key-abc"
    assert isinstance(entry["extra_headers"], dict)
    extra = entry["extra_headers"]
    assert isinstance(extra, dict)
    assert "HTTP-Referer" in extra
    assert "X-Title" in extra


@pytest.mark.unit
def test_openrouter_models_not_registered_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from omnimarket.inference.bridge_config_loader import (
        load_inference_bridge_config_from_env,
    )

    cfg = load_inference_bridge_config_from_env()

    openrouter_keys = [k for k in cfg.model_configs if k.startswith("openrouter/")]
    assert openrouter_keys == []


@pytest.mark.unit
def test_openrouter_base_url_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.example.com/api")

    from omnimarket.inference.bridge_config_loader import (
        load_inference_bridge_config_from_env,
    )

    cfg = load_inference_bridge_config_from_env()

    openrouter_keys = [k for k in cfg.model_configs if k.startswith("openrouter/")]
    assert len(openrouter_keys) > 0
    for key in openrouter_keys:
        assert cfg.model_configs[key]["base_url"] == "https://proxy.example.com/api"


# --- AdapterInferenceBridge extra_headers passthrough ---


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extra_headers_passed_to_http_call():
    config = ModelInferenceBridgeConfig(
        model_configs={
            "openrouter/qwen/qwen3-coder:free": {
                "base_url": "https://openrouter.ai/api",
                "model_id": "qwen/qwen3-coder:free",
                "transport": "http",
                "context_window": 262_144,
                "timeout_seconds": 60.0,
                "api_key": "test-openrouter-key",
                "extra_headers": {
                    "HTTP-Referer": "https://omninode.ai",
                    "X-Title": "OmniNode ONEX",
                },
            }
        }
    )
    bridge = AdapterInferenceBridge(config=config)

    captured_headers: dict[str, str] = {}

    async def mock_http(model_key, cfg, system_prompt, user_prompt, timeout_seconds):
        api_key = str(cfg.get("api_key", "")) or None

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        extra = cfg.get("extra_headers")
        if isinstance(extra, dict):
            for k, v in extra.items():
                headers[str(k)] = str(v)

        captured_headers.update(headers)
        return "ok"

    with patch.object(bridge, "_call_http_model", side_effect=mock_http):
        await bridge.infer(
            model_key="openrouter/qwen/qwen3-coder:free",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=60.0,
        )

    assert captured_headers.get("HTTP-Referer") == "https://omninode.ai"
    assert captured_headers.get("X-Title") == "OmniNode ONEX"
    assert captured_headers.get("Authorization") == "Bearer test-openrouter-key"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bridge_extra_headers_in_actual_request():
    """Verify extra_headers are merged into HTTP request headers end-to-end."""
    config = ModelInferenceBridgeConfig(
        model_configs={
            "openrouter/test-model:free": {
                "base_url": "https://openrouter.ai/api",
                "model_id": "test/model:free",
                "transport": "http",
                "context_window": 8192,
                "timeout_seconds": 60.0,
                "api_key": "sk-test",
                "extra_headers": {
                    "HTTP-Referer": "https://omninode.ai",
                    "X-Title": "OmniNode ONEX",
                },
            }
        }
    )
    bridge = AdapterInferenceBridge(config=config)

    mock_response = MagicMock()
    mock_response.raise_for_status = lambda: None
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "review result"}}]
    }

    import httpx

    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result = await bridge.infer(
            model_key="openrouter/test-model:free",
            system_prompt="You are a reviewer.",
            user_prompt="Review this.",
            timeout_seconds=60.0,
        )

    assert result == "review result"
    call_kwargs = mock_post.call_args
    sent_headers = call_kwargs.kwargs.get("headers", {})
    assert sent_headers.get("HTTP-Referer") == "https://omninode.ai"
    assert sent_headers.get("X-Title") == "OmniNode ONEX"
    assert sent_headers.get("Authorization") == "Bearer sk-test"


# --- Shared inference package re-export ---


@pytest.mark.unit
def test_shared_inference_package_exports():
    from omnimarket.inference import (
        AdapterInferenceBridge,
        EnumModelAvailability,
        EnumOpenRouterTier,
        ModelInferenceAdapter,
        ModelInferenceBridgeConfig,
        ModelOpenRouterModelConfig,
        get_openrouter_models,
        load_inference_bridge_config_from_env,
    )

    assert AdapterInferenceBridge is not None
    assert ModelInferenceAdapter is not None
    assert ModelInferenceBridgeConfig is not None
    assert ModelOpenRouterModelConfig is not None
    assert EnumOpenRouterTier is not None
    assert EnumModelAvailability is not None
    assert callable(get_openrouter_models)
    assert callable(load_inference_bridge_config_from_env)
