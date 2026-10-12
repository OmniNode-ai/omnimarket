# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The swarm endpoint registry is a deployment fact (OMN-20935).

The package ships a neutral registry that declares no endpoint. A deployment supplies its
own through ``OMNIMARKET_SWARM_ENDPOINT_REGISTRY`` or a node overlay under
``ONEX_SKILL_OVERLAY_ROOTS``, and every swarm node that reads the registry resolves it
the same way.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    resolve_swarm_endpoint_registry_path,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.handlers import (
    handler_swarm_endpoint_health as health,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.handlers import (
    handler_swarm_fanout as fanout,
)
from omnimarket.nodes.node_swarm_fleet_discovery_effect.handlers.handler_swarm_fleet_discovery import (
    HandlerSwarmFleetDiscovery,
)
from omnimarket.nodes.node_swarm_registry_compute.handlers import (
    handler_swarm_registry as registry,
)
from tests.node_overlay_support import install_node_overlay

SHIPPED = (
    Path(registry.__file__).resolve().parent.parent
    / "contracts"
    / "endpoint_registry.yaml"
)
FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "swarm_registry"
    / "endpoint_registry.yaml"
)


@pytest.fixture(autouse=True)
def _no_ambient_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIMARKET_SWARM_ENDPOINT_REGISTRY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)


@pytest.mark.unit
def test_the_shipped_registry_declares_no_endpoint() -> None:
    shipped = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    assert shipped == {"registry_schema_version": "1.0.0", "endpoints": []}


@pytest.mark.unit
def test_the_fixture_registry_is_a_positive_control_that_declares_endpoints() -> None:
    fixture = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["endpoints"], "the control must declare endpoints"
    assert all("192.0.2." in ep["base_url"] for ep in fixture["endpoints"])


@pytest.mark.unit
def test_unbound_resolution_is_the_shipped_neutral_file() -> None:
    assert resolve_swarm_endpoint_registry_path(SHIPPED) == SHIPPED
    endpoints, _hash = registry._load_registry(SHIPPED)
    assert endpoints == []


@pytest.mark.unit
def test_the_pinned_file_is_the_registry_every_swarm_node_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIMARKET_SWARM_ENDPOINT_REGISTRY", str(FIXTURE))
    ids = [ep.id for ep in registry.HandlerSwarmRegistry()._endpoints]
    assert ids == ["example-chat-endpoint", "example-embedding-endpoint"]
    assert set(health._load_endpoint_registry()) == set(ids)
    assert set(fanout._load_endpoint_registry()) == set(ids)
    assert HandlerSwarmFleetDiscovery()._registry_path == FIXTURE


@pytest.mark.unit
def test_the_node_overlay_supplies_the_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_swarm_registry_compute",
        yaml.safe_load(FIXTURE.read_text(encoding="utf-8")),
    )
    assert resolve_swarm_endpoint_registry_path(SHIPPED) == overlay
    assert len(registry.HandlerSwarmRegistry()._endpoints) == 2


@pytest.mark.unit
def test_a_pin_to_a_missing_file_refuses_rather_than_using_the_neutral_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        "OMNIMARKET_SWARM_ENDPOINT_REGISTRY", str(tmp_path / "nope.yaml")
    )
    with pytest.raises(NodeOverlayError, match="OMNIMARKET_SWARM_ENDPOINT_REGISTRY"):
        registry.HandlerSwarmRegistry()
