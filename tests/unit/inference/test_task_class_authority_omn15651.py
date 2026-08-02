# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Market-local task-class authority and consumer-totality tests for OMN-15651."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from omnimarket.adapters.claude_code.delegate import _ALLOWED_TASK_TYPES
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_task_execution_orchestrator.handlers.handler_task_execution_orchestrator import (
    _DelegateTaskType,
)

_PROJECT_ROOT = Path(__file__).parents[3]
_DELEGATE_CONTRACT = (
    _PROJECT_ROOT
    / "src/omnimarket/nodes/node_delegate_skill_orchestrator/contract.yaml"
)
_ORCHESTRATOR_REQUEST_CONTRACT = (
    _PROJECT_ROOT
    / "src/omnimarket/nodes/node_delegation_orchestrator/contracts"
    / "delegation_request_v1.yaml"
)

_EXPECTED_PUBLIC = frozenset(
    {
        "code_generation",
        "code_review",
        "complex_reasoning",
        "document",
        "planning",
        "reasoning",
        "refactor",
        "research",
        "review",
        "summarization",
        "test",
    }
)
_EXPECTED_INTERNAL = frozenset(
    {"agent_delegation", "documentation", "escalation", "validator_generation"}
)


def _yaml_mapping(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _required_task_type_enum(path: Path) -> frozenset[str]:
    document = _yaml_mapping(path)
    fields = document["required_fields"]
    task_type = next(field for field in fields if field["name"] == "task_type")
    return frozenset(task_type["constraints"]["enum"])


@pytest.mark.unit
def test_committed_authority_has_accepted_total_exposure_partition() -> None:
    authority = load_task_class_authority()

    assert len(authority.universe) == 15
    assert authority.public_task_classes == _EXPECTED_PUBLIC
    assert authority.internal_task_classes == _EXPECTED_INTERNAL
    assert (
        authority.public_task_classes | authority.internal_task_classes
        == authority.universe
    )
    assert not authority.public_task_classes & authority.internal_task_classes


@pytest.mark.unit
@pytest.mark.parametrize("gateway_exposure", [None, "partner"])
def test_authority_loader_fails_closed_on_missing_or_unknown_exposure(
    tmp_path: Path,
    gateway_exposure: str | None,
) -> None:
    entry = {} if gateway_exposure is None else {"gateway_exposure": gateway_exposure}
    path = tmp_path / "task_class_contracts.yaml"
    path.write_text(
        yaml.safe_dump({"task_classes": {"test": entry}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="task-class authority validation failed"):
        load_task_class_authority(path)


@pytest.mark.unit
def test_every_market_full_universe_consumer_matches_registry() -> None:
    universe = load_task_class_authority().universe
    delegate_contract = _yaml_mapping(_DELEGATE_CONTRACT)
    consumers = {
        "delegate_contract": frozenset(delegate_contract["allowed_task_types"]),
        "delegate_model": frozenset(
            get_args(ModelDelegateSkillRequest.model_fields["task_type"].annotation)
        ),
        "claude_adapter": frozenset(_ALLOWED_TASK_TYPES),
        "orchestrator_contract": _required_task_type_enum(
            _ORCHESTRATOR_REQUEST_CONTRACT
        ),
        "runtime_model": frozenset(
            get_args(ModelDelegationRequest.model_fields["task_type"].annotation)
        ),
        "task_execution_alias": frozenset(get_args(_DelegateTaskType)),
    }

    mismatches = {
        name: {
            "missing": sorted(universe - members),
            "extra": sorted(members - universe),
        }
        for name, members in consumers.items()
        if members != universe
    }
    assert not mismatches, f"Market task-class consumer drift: {mismatches}"
