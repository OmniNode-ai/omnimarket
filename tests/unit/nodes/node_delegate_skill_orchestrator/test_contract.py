# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract tests for node_delegate_skill_orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
)
_CONTRACT_PATH = _NODE_DIR / "contract.yaml"
_METADATA_PATH = _NODE_DIR / "metadata.yaml"


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


@pytest.mark.unit
def test_contract_declares_named_topic_fields() -> None:
    contract = _load_contract()
    rd = contract["runtime_dispatch"]
    assert rd["command_topic"] == "onex.cmd.omnimarket.delegate-skill.v1"
    assert (
        rd["terminal_events"]["success"]
        == "onex.evt.omnimarket.delegate-skill-completed.v1"
    )
    assert (
        rd["terminal_events"]["failure"]
        == "onex.evt.omnimarket.delegate-skill-failed.v1"
    )
    assert rd["default_timeout_ms"] == 300000
    assert rd["max_timeout_ms"] == 900000


@pytest.mark.unit
def test_contract_declares_runtime_profile() -> None:
    contract = _load_contract()
    assert "main" in contract["runtime_profiles"]
    assert len(contract["runtime_profiles"]) == 1


@pytest.mark.unit
def test_contract_declares_allowed_task_types() -> None:
    contract = _load_contract()
    assert set(contract["allowed_task_types"]) == {"test", "document", "research"}


@pytest.mark.unit
def test_contract_declares_timeout_behavior() -> None:
    contract = _load_contract()
    tb = contract["timeout_behavior"]
    assert tb["default_ms"] == 300000
    assert tb["max_ms"] == 900000
    assert tb["terminal_response"]["status"] == "timeout"


@pytest.mark.unit
def test_contract_declares_cross_repo_dependencies() -> None:
    contract = _load_contract()
    deps = contract["cross_repo_dependencies"]
    assert len(deps) == 1
    dep = deps[0]
    assert dep["repo"] == "omnimarket"
    assert dep["node"] == "node_delegation_orchestrator"
    assert dep["contract_name"] == "node_delegation_orchestrator"
    assert "onex.cmd.omnimarket.delegation-request.v1" in dep["required_topics"]
    assert "onex.evt.omnimarket.delegation-completed.v1" in dep["terminal_events"]
    assert "onex.evt.omnimarket.delegation-failed.v1" in dep["terminal_events"]
    model_names = {m["name"] for m in dep["required_models"]}
    assert "ModelDelegationRequest" in model_names


@pytest.mark.unit
def test_contract_handler_module_resolves() -> None:
    contract = _load_contract()
    handler = contract["handler_routing"]["handlers"][0]
    parts = handler["handler_module"].split(".")
    module_file = (
        Path(__file__).resolve().parents[4] / "src" / Path(*parts)
    ).with_suffix(".py")
    assert module_file.exists(), module_file


@pytest.mark.unit
def test_contract_event_bus_topics_match_runtime_dispatch() -> None:
    contract = _load_contract()
    rd = contract["runtime_dispatch"]
    eb = contract["event_bus"]
    assert rd["command_topic"] in eb["subscribe_topics"]
    assert rd["terminal_events"]["success"] in eb["publish_topics"]
    assert rd["terminal_events"]["failure"] in eb["publish_topics"]


@pytest.mark.unit
def test_metadata_registers_entry_points() -> None:
    metadata = yaml.safe_load(_METADATA_PATH.read_text())
    assert (
        metadata["entry_points"]["onex.nodes"]["node_delegate_skill_orchestrator"]
        == "omnimarket.nodes.node_delegate_skill_orchestrator"
    )
    assert (
        metadata["entry_points"]["project.scripts"]["onex-delegate"]
        == "omnimarket.adapters.claude_code.delegate:main"
    )
