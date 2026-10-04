# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The remaining direct readers follow Bifrost, including its null endpoints."""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from click.testing import CliRunner
from omnibase_core.models.routing.model_routing_policy import ModelRoutingPolicy

from omnimarket.cli.cli_delegation_cost_demo import main
from omnimarket.inference.bridge_config_loader import resolve_bifrost_backend
from omnimarket.nodes.node_build_loop_orchestrator.handlers import (
    adapter_delegation_router as delegation,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
    ModelPolicyLoader,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
)
from omnimarket.nodes.node_thread_reply_effect.handlers import (
    handler_thread_reply as thread,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def bind_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[str, str | None, str | None], None]:
    packaged = (
        Path(__file__).parents[2] / "src/omnimarket/configs/bifrost_delegation.yaml"
    )

    def bind(backend_id: str, endpoint: str | None, model: str | None) -> None:
        data = yaml.safe_load(packaged.read_text())
        backend = next(b for b in data["backends"] if b["backend_id"] == backend_id)
        backend.update(endpoint_url=endpoint, model_name=model)
        contract = tmp_path / "bifrost.yaml"
        contract.write_text(yaml.safe_dump(data))
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
        monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)

    return bind


@pytest.mark.parametrize("policy", ["delegation", "delegation_review", "coder"])
@pytest.mark.parametrize("parked", [False, True])
def test_policy_contract_decides_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch,
    bind_backend: Callable[[str, str | None, str | None], None],
    policy: str,
    parked: bool,
) -> None:
    backend_id = "local-coder" if policy == "coder" else "cloud-glm"
    endpoint = None if parked else "http://contract.example/v1/chat/completions"
    bind_backend(backend_id, endpoint, "contract-model")
    loader = ModelPolicyLoader()
    before = loader.resolve_optional(policy)
    monkeypatch.setenv("LLM_GLM_URL", "http://legacy.example/v1")
    monkeypatch.setenv("LLM_CODER_URL", "http://legacy-coder.example/v1")
    monkeypatch.setenv("LLM_GLM_MODEL_NAME", "legacy-model")
    monkeypatch.setenv("LLM_GLM_REVIEW_MODEL_NAME", "legacy-review")
    monkeypatch.setenv("LLM_CODER_MODEL_NAME", "legacy-coder")
    assert loader.resolve_optional(policy) == before == endpoint
    assert loader.resolve_model_id(policy) == "contract-model"
    if parked:
        with pytest.raises(RuntimeError, match="parked"):
            loader.resolve(policy)


@pytest.mark.parametrize("parked", [False, True])
@pytest.mark.asyncio
async def test_coder_readers_use_contract_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bind_backend: Callable[[str, str | None, str | None], None],
    parked: bool,
) -> None:
    endpoint = None if parked else "http://coder.example/custom/chat/completions"
    bind_backend("local-coder", endpoint, "contract-coder")
    monkeypatch.setenv("LLM_CODER_URL", "http://legacy.example/v1")
    monkeypatch.setenv("LLM_CODER_MODEL_NAME", "legacy-coder")
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setattr(delegation, "_resolved_secret", lambda _ref: "")
    configs = delegation.build_endpoint_configs()
    if parked:
        assert delegation.EnumModelTier.LOCAL_CODER not in configs
    else:
        coder = configs[delegation.EnumModelTier.LOCAL_CODER]
        assert (coder.base_url, coder.model_id) == (endpoint, "contract-coder")

    registry = thread._build_registry(ModelRoutingPolicy(primary="qwen3-coder-30b"))
    assert registry["qwen3-coder-30b"]["base_url"] == (endpoint or "")
    with patch.object(thread, "AdapterLlmProviderOpenai") as provider:
        provider.return_value.generate_async = AsyncMock(
            return_value=SimpleNamespace(generated_text="Reply")
        )
        if parked:
            with pytest.raises(RuntimeError, match="endpoint is not configured"):
                await thread._real_llm_call("Review", {"primary": "qwen3-coder-30b"})
            provider.assert_not_called()
        else:
            await thread._real_llm_call("Review", {"primary": "qwen3-coder-30b"})
            assert provider.call_args.kwargs["base_url"] == endpoint
            assert provider.call_args.kwargs["default_model"] == "contract-coder"
            request = provider.return_value.generate_async.call_args.args[0]
            assert request.model_name == "contract-coder"

    live = importlib.import_module(
        "omnimarket.nodes.node_build_loop_orchestrator.assemble_live"
    )
    for name in (
        "LOCAL_CODER_URL",
        "LOCAL_CODER_MODEL_NAME",
        "LLM_GLM_URL",
        "LLM_GLM_MODEL_NAME",
        "LLM_GLM_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.setattr(live, name, getattr(live, name))
    importlib.reload(live)
    monkeypatch.setattr(live, "OPENAI_BASE_URL", None)
    runner = live.LiveBuildDispatchHandler()
    with patch.object(
        runner, "_call_llm", new_callable=AsyncMock, return_value={"example.py": "pass"}
    ) as call:
        result = await runner._generate_implementation(
            BuildTarget(ticket_id="TEST-1"), "example", tmp_path
        )
    if parked:
        assert result is None
        call.assert_not_called()
    else:
        assert result == ({"example.py": "pass"}, "contract-coder")
        assert call.call_args.kwargs["url"] == endpoint


@pytest.mark.parametrize("parked", [False, True])
def test_cost_demo_uses_contract_model_metadata_even_when_parked(
    monkeypatch: pytest.MonkeyPatch,
    bind_backend: Callable[[str, str | None, str | None], None],
    parked: bool,
) -> None:
    endpoint = None if parked else "http://glm.example/v1/chat/completions"
    bind_backend("cloud-glm", endpoint, "contract-glm")
    monkeypatch.delenv("OMNI_HOME", raising=False)
    runner = CliRunner()
    with runner.isolated_filesystem():
        before = runner.invoke(main, ["--output", "json"])
        monkeypatch.setenv("LLM_GLM_URL", "http://legacy.example/v1")
        monkeypatch.setenv("LLM_GLM_MODEL_NAME", "legacy-glm")
        after = runner.invoke(main, ["--output", "json"])
    assert before.exit_code == after.exit_code == 0
    assert json.loads(before.output)["profile"] == json.loads(after.output)["profile"]
    assert json.loads(after.output)["profile"]["cloud_baseline_model"] == "contract-glm"
    backend = resolve_bifrost_backend("cloud-glm")
    assert backend is not None
    assert backend.endpoint_url == endpoint


def test_bound_overlay_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    {
                        "backend_id": "cloud-glm",
                        "endpoint_url": "http://overlay.example/v1/chat/completions",
                        "model_name": "overlay-glm",
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    monkeypatch.setenv("LLM_GLM_URL", "http://legacy.example/v1")
    loader = ModelPolicyLoader()
    assert loader.resolve("delegation") == "http://overlay.example/v1/chat/completions"
    assert loader.resolve_model_id("delegation") == "overlay-glm"


def test_broken_contract_raises_instead_of_using_legacy_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("LLM_GLM_URL", "http://legacy.example/v1")
    with pytest.raises(FileNotFoundError):
        ModelPolicyLoader().resolve_optional("delegation")


@pytest.mark.asyncio
async def test_thread_reply_missing_contract_model_refuses_legacy_alias(
    bind_backend: Callable[[str, str | None, str | None], None],
) -> None:
    bind_backend("local-coder", "http://coder.example/v1/chat/completions", None)
    with patch.object(thread, "AdapterLlmProviderOpenai") as provider:
        with pytest.raises(RuntimeError, match="endpoint is not configured"):
            await thread._real_llm_call("Review", {"primary": "qwen3-coder-30b"})
        provider.assert_not_called()
