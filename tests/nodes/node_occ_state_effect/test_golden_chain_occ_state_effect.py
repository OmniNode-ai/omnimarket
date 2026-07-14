# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_occ_state_effect (RSD-2, OMN-14619).

Satisfies golden-chain-coverage-gate (OMN-12691) and the "Golden Chain Suite"
CI job. Covers contract/metadata structural validation plus the handler's
routing shape. This node performs live GitHub I/O in its ``handle()`` path, so
the read-through-the-real-network proof lives in OMN-14619's evidence (a
canary run against a real PR), not in this offline CI-safe suite; the pure
symbol-derivation logic has its own dedicated seam tests
(``tests/unit/nodes/node_occ_state_effect/test_symbol_derivation.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    HandlerOccStateEffect,
)
from omnimarket.nodes.node_occ_state_effect.models.model_occ_state_request import (
    ModelOccStateRequest,
)


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_occ_state_effect"
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


# ---------------------------------------------------------------------------
# Contract / metadata gate
# ---------------------------------------------------------------------------


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == "node_occ_state_effect"
        assert data["lifecycle"] == "experimental"
        assert data["node_type"] == "EFFECT_GENERIC"

    def test_contract_declares_handler_routing(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        handlers = data.get("handler_routing", {}).get("handlers", [])
        assert handlers, "contract must declare at least one routed handler"
        handler = handlers[0]["handler"]
        assert handler["name"] == "HandlerOccStateEffect"
        assert "module" in handler

    def test_contract_declares_io_models(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert data["input_model"]["name"] == "ModelOccStateRequest"
        assert data["output_model"]["name"] == "ModelOccCompanionRequest"

    def test_contract_declares_github_secret(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert "GITHUB_TOKEN" in data.get("secrets", {})

    def test_contract_declares_read_only_side_effects(
        self, contract_path: Path
    ) -> None:
        data = yaml.safe_load(contract_path.read_text())
        side_effects = data.get("side_effects", {})
        assert side_effects.get("writes") == []
        assert "github_api" in side_effects.get("reads", [])

    def test_contract_declares_no_event_bus_topics_yet(
        self, contract_path: Path
    ) -> None:
        """OMN-14619: deliberately NOT wired to Kafka yet (no RSD-3 trigger
        exists). An event_bus block with no live producer/consumer would trip
        the contract-topic-graph gate's ORPHANED_CONSUMER/ORPHANED_PRODUCER
        checks — correctly, since nothing sends or reads these topics today.
        """
        data = yaml.safe_load(contract_path.read_text())
        assert "event_bus" not in data
        assert "terminal_event" not in data


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == "node_occ_state_effect"
        assert "version" in data
        assert "entry_points" in data

    def test_metadata_declares_read_only_network_required(
        self, metadata_path: Path
    ) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        caps = data.get("capabilities", {})
        assert caps.get("side_effect_class") == "read_only"
        assert caps.get("requires_network") is True


# ---------------------------------------------------------------------------
# Handler import + routing shape
# ---------------------------------------------------------------------------


class TestHandlerImport:
    def test_handler_class_exists(self) -> None:
        assert HandlerOccStateEffect is not None

    def test_handler_declares_effect_category(self) -> None:
        handler = HandlerOccStateEffect()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "EFFECT"

    def test_request_model_requires_repo_and_pr_number(self) -> None:
        request = ModelOccStateRequest(repo="OmniNode-ai/omnimarket", pr_number=1)
        assert request.occ_repo == "OmniNode-ai/onex_change_control"
        assert request.runner != request.verifier
