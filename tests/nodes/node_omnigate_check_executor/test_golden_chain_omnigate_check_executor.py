# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain contract tests for node_omnigate_check_executor.

OMN-12982 Batch 1: runtime_profiles corrected from [effects, local_cli,
contributor_workstation, ci_action] to [effects]. This file satisfies the
golden-chain coverage gate for the changed contract.
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
    / "node_omnigate_check_executor"
)


def _load_contract() -> dict[object, object]:
    contract_path = _NODE_DIR / "contract.yaml"
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict)
    return raw


@pytest.mark.unit
class TestOmnigateCheckExecutorContract:
    """Contract-level golden-chain checks for node_omnigate_check_executor."""

    def test_contract_declares_effects_runtime_profile(self) -> None:
        """OMN-12982 B1: local_cli/contributor_workstation/ci_action stripped; effects kept."""
        contract = _load_contract()
        descriptor = contract.get("descriptor")
        assert isinstance(descriptor, dict)
        profiles = descriptor.get("runtime_profiles")
        assert isinstance(profiles, list)
        assert "effects" in profiles
        # Unregistered profiles must be absent
        for bad in ("local_cli", "contributor_workstation", "ci_action"):
            assert bad not in profiles, f"unregistered profile {bad!r} must not appear"

    def test_contract_subscribes_to_omnigate_cmd_topic(self) -> None:
        contract = _load_contract()
        descriptor = contract.get("descriptor")
        assert isinstance(descriptor, dict)
        event_bus = descriptor.get("event_bus") or contract.get("event_bus") or {}
        assert isinstance(event_bus, dict)
        subscribe = event_bus.get("subscribe_topics", [])
        assert any("omnigate" in t for t in subscribe), (
            "node_omnigate_check_executor must subscribe to an omnigate command topic"
        )

    def test_contract_node_type_is_effect(self) -> None:
        contract = _load_contract()
        node_type = str(contract.get("node_type", "")).lower()
        assert "effect" in node_type

    def test_handler_module_resolves(self) -> None:
        import importlib

        m = importlib.import_module(
            "omnimarket.nodes.node_omnigate_check_executor.handlers.handler_check_executor"
        )
        assert hasattr(m, "HandlerCheckExecutor")
