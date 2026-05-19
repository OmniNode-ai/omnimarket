# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Contract validation tests for delegation pipeline nodes [OMN-7040].

Verifies that all 3 delegation node contracts are well-formed, reference
existing models, and declare correct config dependencies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

NODES_DIR = Path("src/omnimarket/nodes")

_DELEGATION_NODES = [
    "node_delegation_orchestrator",
    "node_delegation_routing_reducer",
    "node_delegation_quality_gate_reducer",
]


@pytest.mark.unit
class TestDelegationContractsExist:
    """All 3 delegation nodes must have contract.yaml files."""

    @pytest.mark.parametrize("node_name", _DELEGATION_NODES)
    def test_contract_file_exists(self, node_name: str) -> None:
        contract_path = NODES_DIR / node_name / "contract.yaml"
        assert contract_path.exists(), f"Missing contract: {contract_path}"

    @pytest.mark.parametrize("node_name", _DELEGATION_NODES)
    def test_contract_is_valid_yaml(self, node_name: str) -> None:
        contract_path = NODES_DIR / node_name / "contract.yaml"
        with contract_path.open() as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert data["name"] == node_name


@pytest.mark.unit
class TestOrchestratorContract:
    """Orchestrator contract must declare FSM states, topics, and models."""

    def _load(self) -> dict:
        path = NODES_DIR / "node_delegation_orchestrator" / "contract.yaml"
        with path.open() as f:
            return yaml.safe_load(f)

    def test_node_type_is_orchestrator(self) -> None:
        data = self._load()
        assert data["node_type"] == "ORCHESTRATOR_GENERIC"

    def test_fsm_states_match_enum(self) -> None:
        data = self._load()
        expected_states = {
            "RECEIVED",
            "ROUTED",
            "EXECUTING",
            "INFERENCE_COMPLETED",
            "GATE_EVALUATED",
            "COMPLETED",
            "FAILED",
        }
        assert set(data["fsm"]["states"]) == expected_states

    def test_terminal_states(self) -> None:
        data = self._load()
        assert set(data["fsm"]["terminal_states"]) == {"COMPLETED", "FAILED"}

    def test_subscribes_to_delegation_request(self) -> None:
        data = self._load()
        topics = data["event_bus"]["subscribe_topics"]
        assert "onex.cmd.omnibase-infra.delegation-request.v1" in topics
        assert "onex.cmd.omnibase-infra.invocation.v1" in topics

    def test_publishes_completed_and_failed(self) -> None:
        data = self._load()
        topics = data["event_bus"]["publish_topics"]
        assert "onex.evt.omnibase-infra.delegation-completed.v1" in topics
        assert "onex.evt.omnibase-infra.delegation-failed.v1" in topics
        assert "onex.cmd.omnibase-infra.remote-agent-invoke.v1" in topics

    def test_terminal_events_declared(self) -> None:
        data = self._load()
        terminal_events = data.get("terminal_events")
        assert isinstance(terminal_events, dict)
        assert (
            terminal_events.get("success")
            == "onex.evt.omnibase-infra.delegation-completed.v1"
        )
        assert (
            terminal_events.get("failure")
            == "onex.evt.omnibase-infra.delegation-failed.v1"
        )


@pytest.mark.unit
class TestRoutingReducerContract:
    """Routing reducer contract must declare config dependencies for LLM endpoints."""

    def _load(self) -> dict:
        path = NODES_DIR / "node_delegation_routing_reducer" / "contract.yaml"
        with path.open() as f:
            return yaml.safe_load(f)

    def test_node_type_is_reducer(self) -> None:
        data = self._load()
        assert data["node_type"] == "REDUCER_GENERIC"

    def test_config_dependencies_include_bifrost_contract_path(self) -> None:
        data = self._load()
        dep_keys = {d["key"] for d in data["config_dependencies"]}
        assert "BIFROST_CONTRACT_PATH" in dep_keys
        assert "BIFROST_OVERLAY_PATH" in dep_keys

    def test_bifrost_contract_path_is_optional(self) -> None:
        data = self._load()
        deps = {d["key"]: d for d in data["config_dependencies"]}
        assert "BIFROST_CONTRACT_PATH" in deps, (
            "BIFROST_CONTRACT_PATH missing from config_dependencies"
        )
        assert deps["BIFROST_CONTRACT_PATH"]["required"] is False
        assert "BIFROST_OVERLAY_PATH" in deps, (
            "BIFROST_OVERLAY_PATH missing from config_dependencies"
        )
        assert deps["BIFROST_OVERLAY_PATH"]["required"] is False


@pytest.mark.unit
class TestQualityGateReducerContract:
    """Quality gate reducer contract must be pure compute (no config deps)."""

    def _load(self) -> dict:
        path = NODES_DIR / "node_delegation_quality_gate_reducer" / "contract.yaml"
        with path.open() as f:
            return yaml.safe_load(f)

    def test_node_type_is_reducer(self) -> None:
        data = self._load()
        assert data["node_type"] == "REDUCER_GENERIC"

    def test_no_config_dependencies(self) -> None:
        data = self._load()
        deps = data.get("config_dependencies", [])
        assert len(deps) == 0, "Quality gate is pure compute -- no config deps expected"
