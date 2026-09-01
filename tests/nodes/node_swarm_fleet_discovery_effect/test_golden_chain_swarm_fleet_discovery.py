# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_swarm_fleet_discovery_effect [OMN-12805, OMN-15048].

Satisfies golden-chain-coverage-gate (OMN-12691): the node-local suite at
``src/omnimarket/nodes/node_swarm_fleet_discovery_effect/tests/`` is never
collected by any CI pytest invocation (``testpaths = ["tests"]``, OMN-14338),
so it does not satisfy the gate. This top-level copy is the one that actually
runs.

Verifies the contract YAML is valid, the handler imports cleanly, the
request/result models are well-formed, and the discovery chain runs
end-to-end (command-shaped request -> terminal discovery result). Also pins
two routing-authority contracts:

1. The OpenRouter base URL is resolved from ``OPENROUTER_BASE_URL`` config and
   fails closed when unset — no hardcoded provider URL default (epic
   OMN-12803).
2. The OpenRouter API key is resolved from the canonical ``OPEN_ROUTER_API_KEY``
   env var (via the secret store's ``llm.openrouter.api_key`` alias), not the
   legacy no-underscore ``OPENROUTER_API_KEY`` literal that no real deployment
   surface (k8s manifests, ``docker-compose.judge.yml``, ``~/.omnibase/.env``)
   ever sets (OMN-15048).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
import yaml

NODE_NAME = "node_swarm_fleet_discovery_effect"


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / NODE_NAME
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == NODE_NAME
        assert data["node_type"] == "effect"

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        handler = data.get("handler", {})
        assert handler.get("name") == "HandlerSwarmFleetDiscovery"
        assert "module" in handler

    def test_contract_has_topics(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        eb = data.get("event_bus", {})
        assert eb.get("subscribe_topics")
        assert eb.get("publish_topics")
        assert data.get("terminal_event") in eb.get("publish_topics", [])

    def test_contract_declares_canonical_openrouter_secret(
        self, contract_path: Path
    ) -> None:
        """OMN-15048: the contract's declared env var must match the name every
        real deployment surface sets — OPEN_ROUTER_API_KEY (with underscore)."""
        data = yaml.safe_load(contract_path.read_text())
        secrets = data.get("secrets") or []
        env_vars = {s.get("env_var") for s in secrets if isinstance(s, dict)}
        assert "OPEN_ROUTER_API_KEY" in env_vars
        assert "OPENROUTER_API_KEY" not in env_vars


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

    @pytest.mark.asyncio
    async def test_default_key_resolution_goes_through_the_store_not_house_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-17372: end-to-end, the DEFAULT key resolution goes through the
        SECRET STORE, and the house env var alone authenticates nothing.

        ASSERTION INVERTED at the case it replaces. This previously required
        the default path to pick up ``OPEN_ROUTER_API_KEY``, passed to the
        resolver as a literal ``env_var_fallback``. That parameter is serviced
        by ``os.environ`` AFTER the store lookup, so it bypassed the lane
        secret mapping and kept this effect authenticating on OmniNode's own
        OpenRouter account. OmniNode does not offer inference: the fallback is
        deleted, so the house variable must now carry NO Authorization header,
        while the ``llm.openrouter.api_key`` ref still resolves through the
        store.
        """
        from omnimarket.nodes.node_swarm_fleet_discovery_effect.handlers.handler_swarm_fleet_discovery import (
            HandlerSwarmFleetDiscovery,
        )
        from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_request import (
            ModelFleetDiscoveryRequest,
        )

        base_url = "https://openrouter.example/api/v1"
        monkeypatch.setenv("OPENROUTER_BASE_URL", base_url)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        # The deleted house fallback. Set on its own it must authenticate
        # nothing; the store-resolved name is set further down.
        monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-house-underscore-key")

        captured_headers: dict[str, str] = {}

        async def _capture(
            url: str, timeout: float, headers: dict[str, str]
        ) -> tuple[int, bytes]:
            captured_headers.update(headers)
            return 200, json.dumps({"data": []}).encode()

        handler = HandlerSwarmFleetDiscovery(http_get_fn=_capture)
        req = ModelFleetDiscoveryRequest(
            correlation_id="golden-chain-omn-15048",
            include_local=False,
            include_openrouter=True,
            min_healthy_endpoints=0,
        )

        await handler.handle(req)

        assert captured_headers.get("Authorization") is None, (
            "OPEN_ROUTER_API_KEY still produced an Authorization header — the "
            "house env-var fallback deleted by OMN-17372 has come back"
        )

        # The surviving path: the ref resolves through the store, which on a
        # local install maps it onto the provider-native name.
        captured_headers.clear()
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-store-resolved-key")
        store_handler = HandlerSwarmFleetDiscovery(http_get_fn=_capture)
        await store_handler.handle(req)

        assert captured_headers.get("Authorization") == "Bearer sk-store-resolved-key"
