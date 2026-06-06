# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract tests for node_delegate_skill_orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from omnimarket.adapters.claude_code.delegate import _ALLOWED_TASK_TYPES
from omnimarket.models.delegation.wire.model_token_limits import (
    DELEGATION_DEFAULT_MAX_TOKENS,
    DELEGATION_MAX_TOKENS_HARD_LIMIT,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    load_runtime_delegation_dispatch_config,
)

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
def test_contract_declares_delegation_runtime_dispatch_config() -> None:
    config = load_runtime_delegation_dispatch_config(_CONTRACT_PATH)

    assert config.topics.command == "onex.cmd.omnibase-infra.delegation-request.v1"
    assert config.topics.completed == "onex.evt.omnibase-infra.delegation-completed.v1"
    assert config.topics.failed == "onex.evt.omnibase-infra.delegation-failed.v1"
    assert config.request_message_type == "omnibase-infra.delegation-request"
    assert config.source_tool == "delegate-skill-runtime-port"
    assert config.consumer_group_prefix == "delegate-skill-runtime-port"
    assert config.wait_timeout_seconds == 300


@pytest.mark.unit
def test_contract_declares_runtime_profile() -> None:
    contract = _load_contract()
    assert "main" in contract["runtime_profiles"]
    assert len(contract["runtime_profiles"]) == 1


@pytest.mark.unit
def test_contract_declares_allowed_task_types() -> None:
    contract = _load_contract()
    assert set(contract["allowed_task_types"]) == {
        "test",
        "document",
        "research",
        "code_generation",
        "code_review",
        "refactor",
        "reasoning",
        "complex_reasoning",
        "planning",
        "review",
        "summarization",
        "agent_delegation",
        "escalation",
    }


@pytest.mark.unit
def test_contract_model_and_adapter_task_types_match() -> None:
    contract = _load_contract()
    model_task_types = set(
        get_args(ModelDelegateSkillRequest.model_fields["task_type"].annotation)
    )
    assert set(contract["allowed_task_types"]) == model_task_types
    assert set(_ALLOWED_TASK_TYPES) == model_task_types


@pytest.mark.unit
def test_contract_declares_max_tokens_boundary() -> None:
    max_tokens = _load_contract()["inputs"]["max_tokens"]

    assert max_tokens["default"] == DELEGATION_DEFAULT_MAX_TOKENS
    assert max_tokens["maximum"] == DELEGATION_MAX_TOKENS_HARD_LIMIT


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
    assert "onex.cmd.omnibase-infra.delegation-request.v1" in dep["required_topics"]
    assert "onex.evt.omnibase-infra.delegation-completed.v1" in dep["terminal_events"]
    assert "onex.evt.omnibase-infra.delegation-failed.v1" in dep["terminal_events"]
    model_names = {m["name"] for m in dep["required_models"]}
    assert "ModelDelegationRequest" in model_names


@pytest.mark.unit
def test_contract_handler_module_resolves() -> None:
    contract = _load_contract()
    handler = contract["handler"]
    parts = handler["module"].split(".")
    module_file = (
        Path(__file__).resolve().parents[4] / "src" / Path(*parts)
    ).with_suffix(".py")
    assert module_file.exists(), module_file


@pytest.mark.unit
def test_contract_event_bus_topics_match_runtime_dispatch() -> None:
    contract = _load_contract()
    rd = contract["runtime_dispatch"]
    eb = contract["event_bus"]
    assert eb["plugin_managed"] is False
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
