# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain contract tests for node_intelligence_orchestrator.

OMN-12982 Batch 1: runtime_profiles corrected from [intelligence] (nonexistent)
to [main] (orchestrator lane). This file satisfies the golden-chain coverage
gate for the changed contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_intelligence_orchestrator"
)


def _load_contract() -> dict[object, object]:
    contract_path = _NODE_DIR / "contract.yaml"
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict)
    return raw


@pytest.mark.unit
class TestIntelligenceOrchestratorContract:
    """Contract-level golden-chain checks for node_intelligence_orchestrator."""

    def test_contract_declares_main_runtime_profile(self) -> None:
        """OMN-12982 B1: [intelligence] was nonexistent → corrected to [main]."""
        contract = _load_contract()
        profiles = contract.get("runtime_profiles")
        assert isinstance(profiles, list)
        assert "main" in profiles
        assert "intelligence" not in profiles, (
            "'intelligence' is not a registered ONEX runtime profile (OMN-12982 B1)"
        )

    def test_contract_node_type_is_orchestrator(self) -> None:
        contract = _load_contract()
        node_type = str(contract.get("node_type", "")).lower()
        assert "orchestrator" in node_type

    def test_contract_subscribes_to_cmd_topics(self) -> None:
        contract = _load_contract()
        event_bus = contract.get("event_bus") or {}
        assert isinstance(event_bus, dict)
        subscribe = event_bus.get("subscribe_topics", [])
        assert any("onex.cmd." in t for t in subscribe), (
            "node_intelligence_orchestrator must subscribe to at least one command topic"
        )

    def test_handler_module_resolves(self) -> None:
        import importlib

        m = importlib.import_module(
            "omnimarket.nodes.node_intelligence_orchestrator.handlers.handler_receive_intent"
        )
        assert hasattr(m, "handle_receive_intent")
