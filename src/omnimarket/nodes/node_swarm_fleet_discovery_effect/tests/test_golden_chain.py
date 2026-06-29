# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_swarm_fleet_discovery_effect [OMN-12805].

Verifies the contract/metadata YAML are valid, the handler imports cleanly,
the request/result models are well-formed, and the discovery chain runs
end-to-end (command-shaped request -> terminal discovery result). Also pins
the routing-authority contract for the OpenRouter base URL: the handler
resolves it from ``OPENROUTER_BASE_URL`` config and fails closed when unset —
no hardcoded provider URL default (epic OMN-12803).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

NODE_NAME = "node_swarm_fleet_discovery_effect"


@pytest.fixture
def node_dir() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == NODE_NAME
        assert data["node_type"] == "effect"

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        handler = data.get("handler", {})
        assert "module" in handler
        assert "class" in handler

    def test_contract_has_topics(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        eb = data.get("event_bus", {})
        assert eb.get("subscribe_topics")
        assert eb.get("publish_topics")
        assert data.get("terminal_event") in eb.get("publish_topics", [])


class TestMetadataYaml:
    def test_metadata_loads(self, metadata_path: Path) -> None:
        import yaml

        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == NODE_NAME
        assert "version" in data
        assert "entry_points" in data


class TestHandlerImport:
    def test_handler_class_imports(self) -> None:
        from omnimarket.nodes.node_swarm_fleet_discovery_effect.handlers.handler_swarm_fleet_discovery import (
            HandlerSwarmFleetDiscovery,
        )

        assert HandlerSwarmFleetDiscovery is not None

    def test_models_import(self) -> None:
        from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_request import (
            ModelFleetDiscoveryRequest,
        )
        from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_result import (
            ModelFleetDiscoveryResult,
        )

        assert ModelFleetDiscoveryRequest is not None
        assert ModelFleetDiscoveryResult is not None


def _http_get_for(
    responses: dict[str, tuple[int, bytes]],
) -> Callable[[str, float, dict[str, str]], Coroutine[Any, Any, tuple[int, bytes]]]:
    async def _get(
        url: str, timeout: float, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        if url not in responses:
            raise ConnectionError(f"unexpected url: {url}")
        return responses[url]

    return _get


class TestDiscoveryChain:
    @pytest.mark.asyncio
    async def test_openrouter_chain_resolves_base_url_from_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Command -> terminal result; OpenRouter base URL comes from config."""
        from omnimarket.nodes.node_swarm_fleet_discovery_effect.handlers.handler_swarm_fleet_discovery import (
            HandlerSwarmFleetDiscovery,
        )
        from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_request import (
            ModelFleetDiscoveryRequest,
        )

        base_url = "https://openrouter.example/api/v1"
        monkeypatch.setenv("OPENROUTER_BASE_URL", base_url)

        live_ids = ["qwen/qwen3-coder:free", "deepseek/deepseek-r1:free"]
        body = json.dumps({"data": [{"id": mid} for mid in live_ids]}).encode()
        responses = {f"{base_url}/models": (200, body)}

        handler = HandlerSwarmFleetDiscovery(
            http_get_fn=_http_get_for(responses),
            openrouter_api_key="test-key",
        )
        req = ModelFleetDiscoveryRequest(
            correlation_id="golden-chain-1",
            include_local=False,
            include_openrouter=True,
            min_healthy_endpoints=1,
        )

        result = await handler.handle(req)

        assert result.correlation_id == "golden-chain-1"
        assert result.openrouter_count > 0
        # Endpoints carry the config-resolved base URL, not a hardcoded literal.
        for ep in result.endpoints:
            assert ep.base_url == base_url

    @pytest.mark.asyncio
    async def test_openrouter_chain_fails_closed_without_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No OPENROUTER_BASE_URL configured -> fail closed (OMN-12805)."""
        from omnimarket.nodes.node_swarm_fleet_discovery_effect.handlers.handler_swarm_fleet_discovery import (
            HandlerSwarmFleetDiscovery,
        )
        from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_request import (
            ModelFleetDiscoveryRequest,
        )

        monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

        handler = HandlerSwarmFleetDiscovery(
            http_get_fn=_http_get_for({}),
            openrouter_api_key="test-key",
        )
        req = ModelFleetDiscoveryRequest(
            correlation_id="golden-chain-2",
            include_local=False,
            include_openrouter=True,
            min_healthy_endpoints=1,
        )

        with pytest.raises(ValueError, match="OPENROUTER_BASE_URL"):
            await handler.handle(req)
