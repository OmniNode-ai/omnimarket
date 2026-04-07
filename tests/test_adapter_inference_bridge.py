"""Tests for the inference bridge adapter."""

from unittest.mock import AsyncMock, patch

import pytest

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceBridgeConfig,
)


@pytest.mark.asyncio
async def test_bridge_constructs_request_and_extracts_text():
    config = ModelInferenceBridgeConfig(
        model_configs={
            "test-model": {
                "base_url": "http://localhost:8000",
                "model_id": "test-model-v1",
                "transport": "http",
                "context_window": 32000,
                "timeout_seconds": 60.0,
            }
        }
    )
    bridge = AdapterInferenceBridge(config=config)

    with patch.object(bridge, "_call_http_model", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '[{"category":"security","severity":"major","title":"test","description":"test desc"}]'
        result = await bridge.infer(
            model_key="test-model",
            system_prompt="You are a reviewer.",
            user_prompt="Review this code.",
            timeout_seconds=60.0,
        )
        assert "test desc" in result
        mock_call.assert_called_once()


@pytest.mark.asyncio
async def test_bridge_unknown_model_raises():
    config = ModelInferenceBridgeConfig(model_configs={})
    bridge = AdapterInferenceBridge(config=config)

    with pytest.raises(ValueError, match="Unknown model_key"):
        await bridge.infer("nonexistent", "sys", "usr", 60.0)
