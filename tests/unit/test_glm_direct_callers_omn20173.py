# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20173: env-driven GLM readers skip Coding Plan before any HTTP."""

import importlib
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr

from omnimarket.inference import bridge_config_loader as bridge
from omnimarket.nodes.node_build_loop_orchestrator.handlers import (
    adapter_delegation_router as delegation,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
    ModelPolicyLoader,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
)

pytestmark = pytest.mark.unit

URL_CASES = [
    ("https://api.z.ai/api/coding/paas/v4", True),
    ("https://api.z.ai/api/paas/v4", False),
    ("http://glm.example/v4", False),
]


@pytest.fixture(autouse=True)
def no_http() -> Iterator[None]:
    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post,
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as get,
    ):
        yield
        post.assert_not_called()
        get.assert_not_called()


# The bridge reads its glm endpoint from the bifrost contract (OMN-17103), which
# refuses the z.ai general-API surface for the paid glm id, so the allowed
# non-blocked case is a plain host.
BRIDGE_URL_CASES = [
    ("https://api.z.ai/api/coding/paas/v4", True),
    ("http://glm.example/v4", False),
]


@pytest.mark.parametrize(("url", "blocked"), BRIDGE_URL_CASES)
@pytest.mark.parametrize("async_loader", [False, True])
@pytest.mark.asyncio
async def test_bridge_glm_url(
    monkeypatch: pytest.MonkeyPatch,
    bind_bifrost_glm_endpoint: Callable[[str | None], None],
    url: str,
    blocked: bool,
    async_loader: bool,
) -> None:
    bind_bifrost_glm_endpoint(url)

    def key(ref: str, **_: object) -> SecretStr | None:
        return SecretStr("test-key") if ref == "llm.glm.api_key" else None

    async def async_key(ref: str, **kwargs: object) -> SecretStr | None:
        return key(ref, **kwargs)

    monkeypatch.setattr(bridge, "resolve_api_key", key)
    monkeypatch.setattr(bridge, "resolve_api_key_async", async_key)
    config = (
        await bridge.load_inference_bridge_config_from_env_async()
        if async_loader
        else bridge.load_inference_bridge_config_from_env()
    )
    if blocked:
        assert "glm" not in config.model_configs
    else:
        assert config.model_configs["glm"]["base_url"] == url
        assert config.model_configs["glm"]["model_id"] == "glm-5.3-flash"
        assert config.model_configs["glm"]["api_key"] == "test-key"


@pytest.mark.parametrize("async_loader", [False, True])
@pytest.mark.asyncio
async def test_bridge_glm_env_is_not_an_authority(
    monkeypatch: pytest.MonkeyPatch, async_loader: bool
) -> None:
    """The packaged contract parks GLM; LLM_GLM_URL cannot re-open it (OMN-17103)."""
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.setenv("LLM_GLM_URL", "https://api.z.ai/api/paas/v4")
    monkeypatch.setenv("LLM_GLM_MODEL_NAME", "glm-test")

    async def no_key(ref: str, **_: object) -> None:
        return None

    monkeypatch.setattr(bridge, "resolve_api_key", lambda *_a, **_k: None)
    monkeypatch.setattr(bridge, "resolve_api_key_async", no_key)
    config = (
        await bridge.load_inference_bridge_config_from_env_async()
        if async_loader
        else bridge.load_inference_bridge_config_from_env()
    )
    assert "glm" not in config.model_configs


@pytest.mark.parametrize(("url", "blocked"), URL_CASES)
def test_delegation_glm_tiers(
    monkeypatch: pytest.MonkeyPatch, url: str, blocked: bool
) -> None:
    monkeypatch.setenv("LLM_GLM_URL", url)
    monkeypatch.setenv("LLM_GLM_MODEL_NAME", "glm-test")
    monkeypatch.setenv("LLM_GLM_REVIEW_MODEL_NAME", "glm-review")
    monkeypatch.setattr(delegation, "_resolved_secret", lambda _ref: "test-key")
    configs = delegation.build_endpoint_configs()
    for tier, model in (
        (delegation.EnumModelTier.FRONTIER_GLM, "glm-test"),
        (delegation.EnumModelTier.FRONTIER_REVIEW, "glm-review"),
    ):
        if blocked:
            assert tier not in configs
        else:
            assert configs[tier].base_url == url
            assert configs[tier].model_id == model


@pytest.mark.parametrize(("url", "blocked"), URL_CASES)
@pytest.mark.asyncio
async def test_live_assembly_glm_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str, blocked: bool
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_GLM_URL", url)
    with (
        patch.object(ModelPolicyLoader, "resolve_api_key", return_value="test-key"),
        patch.object(
            ModelPolicyLoader, "resolve_model_id_optional", return_value="glm-test"
        ),
    ):
        module = importlib.import_module(
            "omnimarket.nodes.node_build_loop_orchestrator.assemble_live"
        )
        # Reload exercises the actual module-level env/policy reader.
        for name in ("LLM_GLM_URL", "LLM_GLM_API_KEY", "LLM_GLM_MODEL_NAME"):
            monkeypatch.setattr(module, name, getattr(module, name))
        importlib.reload(module)
    assert ("" if blocked else url) == module.LLM_GLM_URL
    for name in ("LOCAL_CODER_URL", "OPENAI_BASE_URL", "GOOGLE_BASE_URL"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, "")
    runner = module.LiveBuildDispatchHandler()
    with patch.object(
        runner,
        "_call_llm",
        new_callable=AsyncMock,
        return_value={"src/example.py": "pass"},
    ) as call:
        result = await runner._generate_implementation(
            BuildTarget(ticket_id="OMN-20173"), "example", tmp_path
        )
    if blocked:
        assert result is None
        call.assert_not_called()
    else:
        assert result == ({"src/example.py": "pass"}, "glm-test")
        call.assert_awaited_once()
        assert call.call_args.kwargs["url"] == f"{url}/chat/completions"


@pytest.mark.parametrize(("url", "blocked"), URL_CASES)
@pytest.mark.asyncio
async def test_thread_reply_glm_registry(
    monkeypatch: pytest.MonkeyPatch, url: str, blocked: bool
) -> None:
    module = importlib.import_module(
        "omnimarket.nodes.node_thread_reply_effect.handlers.handler_thread_reply"
    )
    monkeypatch.setenv("LLM_GLM_URL", url)
    monkeypatch.setattr(module, "_BASE_REGISTRY", module._BASE_REGISTRY)
    importlib.reload(module)
    assert module._BASE_REGISTRY["glm-4.5"]["base_url"] == ("" if blocked else url)
    policy = {"primary": "glm-4.5", "max_retries": 1}
    with patch.object(
        module.AdapterLlmProviderOpenai, "generate_async", new_callable=AsyncMock
    ) as generate:
        if blocked:
            with pytest.raises((ValueError, RuntimeError)):
                await module._real_llm_call("Review this", policy)
            generate.assert_not_called()
        else:
            from types import SimpleNamespace

            generate.return_value = SimpleNamespace(generated_text="Reply")
            assert await module._real_llm_call("Review this", policy) == (
                "Reply",
                False,
            )
            generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_thread_reply_non_glm_provider_default_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OMN-20173: an unset GLM route must not change other provider defaults.
    from types import SimpleNamespace

    module = importlib.import_module(
        "omnimarket.nodes.node_thread_reply_effect.handlers.handler_thread_reply"
    )
    monkeypatch.setenv("LLM_CODER_URL", "http://coder.example/v1")
    with patch.object(
        module.AdapterLlmProviderOpenai,
        "generate_async",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(generated_text="Local reply"),
    ) as generate:
        assert await module._real_llm_call(
            "Review this", {"primary": "custom-local-model", "max_retries": 1}
        ) == ("Local reply", False)
        generate.assert_awaited_once()
