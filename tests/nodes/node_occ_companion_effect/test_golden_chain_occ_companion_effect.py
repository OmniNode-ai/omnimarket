# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_occ_companion_effect (RSD-3, OMN-14622).

Satisfies golden-chain-coverage-gate (OMN-12691) and the "Golden Chain Suite"
CI job. Covers contract/metadata structural validation plus the handler routing
shape. This node performs live git/gh I/O in its mutate path, so the
read-through-the-real-network proof lives in OMN-14622's evidence (a live run
against a real gated PR); the offline read->compute->plan wiring is covered by
``tests/unit/nodes/node_occ_companion_effect/`` (dry_run + the cross-boundary
occ-preflight regression).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    HandlerOccCompanionEffect,
)
from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_request import (
    ModelOccCompanionEffectRequest,
)


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_occ_companion_effect"
    )


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
        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == "node_occ_companion_effect"
        assert data["lifecycle"] == "experimental"
        assert data["node_type"] == "EFFECT_GENERIC"

    def test_contract_declares_handler_routing(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        handlers = data.get("handler_routing", {}).get("handlers", [])
        assert handlers, "contract must declare at least one routed handler"
        handler = handlers[0]
        assert handler["operation"] == "author_occ_companion"
        assert handler["handler"]["name"] == "HandlerOccCompanionEffect"
        assert "module" in handler["handler"]

    def test_contract_declares_io_models(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert data["input_model"]["name"] == "ModelOccCompanionEffectRequest"
        assert data["output_model"]["name"] == "ModelOccCompanionEffectResult"

    def test_contract_declares_github_secret(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert "GITHUB_TOKEN" in data.get("secrets", {})

    def test_contract_declares_write_side_effects(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        side_effects = data.get("side_effects", {})
        assert "github_pull_request" in side_effects.get("writes", [])
        assert "github_api" in side_effects.get("reads", [])

    def test_contract_declares_no_event_bus_topics_yet(
        self, contract_path: Path
    ) -> None:
        """OMN-14622: first PR is directly-invoked (same staging as RSD-2). The
        Kafka trigger lands with the emitter-retirement follow-up, alongside the
        producer/consumer that keeps the contract-topic-graph gate satisfied.
        """
        data = yaml.safe_load(contract_path.read_text())
        assert "event_bus" not in data
        assert "terminal_event" not in data


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == "node_occ_companion_effect"
        assert "version" in data
        assert "entry_points" in data

    def test_metadata_declares_write_network_required(
        self, metadata_path: Path
    ) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        caps = data.get("capabilities", {})
        assert caps.get("side_effect_class") == "write"
        assert caps.get("requires_network") is True


class TestHandlerImport:
    def test_handler_class_exists(self) -> None:
        assert HandlerOccCompanionEffect is not None

    def test_handler_declares_effect_category(self) -> None:
        handler = HandlerOccCompanionEffect()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "EFFECT"

    def test_request_defaults_to_dry_run(self) -> None:
        request = ModelOccCompanionEffectRequest(
            repo="OmniNode-ai/omnimarket", pr_number=1
        )
        assert request.mode == "dry_run"
        assert request.occ_repo == "OmniNode-ai/onex_change_control"
        assert request.runner != request.verifier
