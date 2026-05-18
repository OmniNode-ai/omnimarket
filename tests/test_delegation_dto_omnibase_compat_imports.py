# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Graduation tests for delegation DTO imports (OMN-11183).

Verifies that omnimarket shim paths resolve to models whose canonical home is
omnimarket.models.delegation.wire — not the temporary omnibase_compat staging area.
"""

from __future__ import annotations

from importlib import import_module, resources
from pathlib import Path
from typing import Any

import pytest
import yaml

DTO_IMPORTS = {
    "omnimarket.events.delegation.ModelDelegationRequest": "omnimarket.models.delegation.wire.model_delegation_request.ModelDelegationRequest",
    "omnimarket.events.delegation.ModelDelegationResult": "omnimarket.models.delegation.wire.model_delegation_result.ModelDelegationResult",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelBaselineIntent": "omnimarket.models.delegation.wire.model_orchestrator_intents.ModelBaselineIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.model_compliance_loop_result.ModelComplianceLoopResult": "omnimarket.models.delegation.wire.model_orchestrator_intents.ModelComplianceLoopResult",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelDelegationRequest": "omnimarket.models.delegation.wire.model_delegation_request.ModelDelegationRequest",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelDelegationResult": "omnimarket.models.delegation.wire.model_delegation_result.ModelDelegationResult",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelDelegationEvent": "omnimarket.models.delegation.wire.model_event_envelope.ModelDelegationEventEnvelope",
    "omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event.ModelDelegationEvent": "omnimarket.models.delegation.wire.model_event_envelope.ModelDelegationEventEnvelope",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelInferenceIntent": "omnimarket.models.delegation.wire.model_orchestrator_intents.ModelInferenceIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelInferenceResponseData": "omnimarket.models.delegation.wire.model_orchestrator_intents.ModelInferenceResponseData",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelQualityGateIntent": "omnimarket.models.delegation.wire.model_orchestrator_intents.ModelQualityGateIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelRoutingIntent": "omnimarket.models.delegation.wire.model_orchestrator_intents.ModelRoutingIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelTaskDelegatedEvent": "omnimarket.models.delegation.wire.model_task_delegated_event.ModelTaskDelegatedEvent",
    "omnimarket.nodes.node_delegation_quality_gate_reducer.models.ModelQualityGateInput": "omnimarket.models.delegation.wire.model_quality_gate.ModelQualityGateInput",
    "omnimarket.nodes.node_delegation_quality_gate_reducer.models.ModelQualityGateResult": "omnimarket.models.delegation.wire.model_quality_gate.ModelQualityGateResult",
    "omnimarket.nodes.node_delegation_routing_reducer.models.ModelDelegationConfig": "omnimarket.models.delegation.wire.model_routing_config.ModelDelegationConfig",
    "omnimarket.nodes.node_delegation_routing_reducer.models.ModelRoutingTier": "omnimarket.models.delegation.wire.model_routing_config.ModelRoutingTier",
    "omnimarket.nodes.node_delegation_routing_reducer.models.ModelTierModel": "omnimarket.models.delegation.wire.model_routing_config.ModelTierModel",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelBifrostDelegationConfig": "omnimarket.models.delegation.wire.model_bifrost_delegation_config.ModelBifrostDelegationConfig",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationBackendConfig": "omnimarket.models.delegation.wire.model_bifrost_delegation_config.ModelDelegationBackendConfig",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationCircuitBreakerConfig": "omnimarket.models.delegation.wire.model_bifrost_delegation_config.ModelDelegationCircuitBreakerConfig",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationFailoverConfig": "omnimarket.models.delegation.wire.model_bifrost_delegation_config.ModelDelegationFailoverConfig",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationFallbackPolicy": "omnimarket.models.delegation.wire.model_bifrost_delegation_config.ModelDelegationFallbackPolicy",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationRoutingRule": "omnimarket.models.delegation.wire.model_bifrost_delegation_config.ModelDelegationRoutingRule",
    "omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config.ModelDelegationShadowConfig": "omnimarket.models.delegation.wire.model_bifrost_delegation_config.ModelDelegationShadowConfig",
    "omnimarket.nodes.node_budget_policy_compute.models.model_budget_limits.ModelBudgetLimits": "omnimarket.models.delegation.wire.model_budget.ModelBudgetLimits",
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
@pytest.mark.parametrize(("market_ref", "canonical_ref"), DTO_IMPORTS.items())
def test_delegation_dto_market_paths_resolve_to_omnimarket_wire(
    market_ref: str, canonical_ref: str
) -> None:
    """omnimarket shim paths resolve to canonical delegation wire DTOs in omnimarket (OMN-11183)."""
    market_model = _resolve(market_ref)
    canonical_model = _resolve(canonical_ref)

    assert market_model is canonical_model
    assert market_model.__module__.startswith("omnimarket.models.delegation.wire.")


@pytest.mark.unit
@pytest.mark.parametrize("node_name", DELEGATION_CONTRACTS)
def test_delegation_contract_model_refs_keep_compatibility_paths(
    node_name: str,
) -> None:
    """Contract model refs resolve through omnimarket shims to omnimarket.models.delegation.wire."""
    contract_path = Path(
        resources.files("omnimarket.nodes") / node_name / "contract.yaml"  # type: ignore[arg-type]
    )
    contract = yaml.safe_load(contract_path.read_text())
    omnimarket_refs = [
        ref
        for ref in _model_refs(contract)
        if ref.startswith("omnimarket.") and not ref.endswith(".ModelRoutingDecision")
    ]
    unmapped_refs = sorted({ref for ref in omnimarket_refs if ref not in DTO_IMPORTS})

    assert not unmapped_refs
    model_refs = [ref for ref in omnimarket_refs if ref in DTO_IMPORTS]
    assert model_refs
    assert all(
        _resolve(model_ref).__module__.startswith("omnimarket.models.delegation.wire.")
        for model_ref in model_refs
    )
