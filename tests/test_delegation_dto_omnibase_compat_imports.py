# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility tests for delegation DTO imports from omnibase_compat."""

from __future__ import annotations

from importlib import import_module, resources
from pathlib import Path
from typing import Any

import pytest
import yaml

DTO_IMPORTS = {
    "omnimarket.events.delegation.ModelDelegationRequest": "omnibase_compat.contracts.delegation.wire.ModelDelegationRequest",
    "omnimarket.events.delegation.ModelDelegationResult": "omnibase_compat.contracts.delegation.wire.ModelDelegationResult",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelBaselineIntent": "omnibase_compat.contracts.delegation.wire.ModelBaselineIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.model_compliance_loop_result.ModelComplianceLoopResult": "omnibase_compat.contracts.delegation.wire.ModelComplianceLoopResult",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelDelegationRequest": "omnibase_compat.contracts.delegation.wire.ModelDelegationRequest",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelDelegationResult": "omnibase_compat.contracts.delegation.wire.ModelDelegationResult",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelInferenceIntent": "omnibase_compat.contracts.delegation.wire.ModelInferenceIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelInferenceResponseData": "omnibase_compat.contracts.delegation.wire.ModelInferenceResponseData",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelQualityGateIntent": "omnibase_compat.contracts.delegation.wire.ModelQualityGateIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelRoutingIntent": "omnibase_compat.contracts.delegation.wire.ModelRoutingIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelTaskDelegatedEvent": "omnibase_compat.contracts.delegation.wire.ModelTaskDelegatedEvent",
    "omnimarket.nodes.node_delegation_quality_gate_reducer.models.ModelQualityGateInput": "omnibase_compat.contracts.delegation.wire.ModelQualityGateInput",
    "omnimarket.nodes.node_delegation_quality_gate_reducer.models.ModelQualityGateResult": "omnibase_compat.contracts.delegation.wire.ModelQualityGateResult",
    "omnimarket.nodes.node_delegation_routing_reducer.models.ModelDelegationConfig": "omnibase_compat.contracts.delegation.wire.ModelDelegationConfig",
    "omnimarket.nodes.node_delegation_routing_reducer.models.ModelRoutingTier": "omnibase_compat.contracts.delegation.wire.ModelRoutingTier",
    "omnimarket.nodes.node_delegation_routing_reducer.models.ModelTierModel": "omnibase_compat.contracts.delegation.wire.ModelTierModel",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelBifrostDelegationConfig": "omnibase_compat.contracts.delegation.wire.ModelBifrostDelegationConfig",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationBackendConfig": "omnibase_compat.contracts.delegation.wire.ModelDelegationBackendConfig",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationCircuitBreakerConfig": "omnibase_compat.contracts.delegation.wire.ModelDelegationCircuitBreakerConfig",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationFailoverConfig": "omnibase_compat.contracts.delegation.wire.ModelDelegationFailoverConfig",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationFallbackPolicy": "omnibase_compat.contracts.delegation.wire.ModelDelegationFallbackPolicy",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationRoutingRule": "omnibase_compat.contracts.delegation.wire.ModelDelegationRoutingRule",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationShadowConfig": "omnibase_compat.contracts.delegation.wire.ModelDelegationShadowConfig",
    "omnimarket.nodes.node_budget_policy_compute.models.model_budget_limits.ModelBudgetLimits": "omnibase_compat.contracts.delegation.wire.ModelBudgetLimits",
}

DELEGATION_CONTRACTS = [
    "node_delegation_orchestrator",
    "node_delegation_quality_gate_reducer",
    "node_delegation_routing_reducer",
]


def _resolve(dotted_ref: str) -> Any:
    module_name, _, attr = dotted_ref.rpartition(".")
    return getattr(import_module(module_name), attr)


def _model_refs(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        name = value.get("name")
        module = value.get("module")
        if isinstance(name, str) and isinstance(module, str):
            refs.append(f"{module}.{name}")
        for child in value.values():
            refs.extend(_model_refs(child))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_model_refs(item))
    return refs


@pytest.mark.unit
@pytest.mark.parametrize(("market_ref", "compat_ref"), DTO_IMPORTS.items())
def test_delegation_dto_market_paths_reexport_omnibase_compat_classes(
    market_ref: str, compat_ref: str
) -> None:
    """Existing omnimarket module paths resolve to canonical delegation wire DTOs."""
    market_model = _resolve(market_ref)
    compat_model = _resolve(compat_ref)

    assert market_model is compat_model
    assert market_model.__module__.startswith("omnibase_compat.")


@pytest.mark.unit
@pytest.mark.parametrize("node_name", DELEGATION_CONTRACTS)
def test_delegation_contract_model_refs_keep_compatibility_paths(
    node_name: str,
) -> None:
    """Contract model refs can keep omnimarket paths through compatibility shims."""
    contract_path = Path(
        resources.files("omnimarket.nodes") / node_name / "contract.yaml"  # type: ignore[arg-type]
    )
    contract = yaml.safe_load(contract_path.read_text())
    model_refs = [
        ref
        for ref in _model_refs(contract)
        if ref in DTO_IMPORTS and not ref.endswith(".ModelRoutingDecision")
    ]

    assert model_refs
    assert all(
        _resolve(model_ref).__module__.startswith("omnibase_compat.")
        for model_ref in model_refs
    )
