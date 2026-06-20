# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Import identity tests for delegation DTO shim paths (OMN-12659).

Verifies that omnimarket shim paths resolve to models whose canonical home is
omnibase_core.models.delegation.wire — the single source of truth for
platform-primitive delegation wire DTOs (graduated from omnibase_compat, OMN-12126).

Omnimarket-specific projection models (ModelDelegateSkillResponse,
ModelDelegateSkillTerminalProjection, etc.) remain in omnimarket.
"""

from __future__ import annotations

from importlib import import_module, resources
from pathlib import Path
from typing import Any

import pytest
import yaml

# Each entry maps an omnimarket shim import path to its canonical core path.
# The test verifies that following the shim yields the exact same class object
# as importing directly from core.
DTO_IMPORTS = {
    "omnimarket.events.delegation.ModelDelegationRequest": "omnibase_core.models.delegation.wire.model_delegation_wire_request.ModelDelegationRequest",
    "omnimarket.events.delegation.ModelDelegationResult": "omnibase_core.models.delegation.wire.model_delegation_result.ModelDelegationResult",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelBaselineIntent": "omnibase_core.models.delegation.wire.model_orchestrator_intents.ModelBaselineIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.model_compliance_loop_result.ModelComplianceLoopResult": "omnibase_core.models.delegation.wire.model_orchestrator_intents.ModelComplianceLoopResult",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelDelegationRequest": "omnibase_core.models.delegation.wire.model_delegation_wire_request.ModelDelegationRequest",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelDelegationResult": "omnibase_core.models.delegation.wire.model_delegation_result.ModelDelegationResult",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelDelegationEvent": "omnibase_core.models.delegation.wire.model_delegation_wire_envelope.ModelDelegationEventEnvelope",
    "omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event.ModelDelegationEvent": "omnibase_core.models.delegation.wire.model_delegation_wire_envelope.ModelDelegationEventEnvelope",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelInferenceIntent": "omnibase_core.models.delegation.wire.model_orchestrator_intents.ModelInferenceIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelInferenceResponseData": "omnibase_core.models.delegation.wire.model_orchestrator_intents.ModelInferenceResponseData",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelQualityGateIntent": "omnibase_core.models.delegation.wire.model_orchestrator_intents.ModelQualityGateIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelRoutingIntent": "omnibase_core.models.delegation.wire.model_orchestrator_intents.ModelRoutingIntent",
    "omnimarket.nodes.node_delegation_orchestrator.models.ModelTaskDelegatedEvent": "omnibase_core.models.delegation.wire.model_task_delegated_event.ModelTaskDelegatedEvent",
    "omnimarket.nodes.node_delegation_quality_gate_reducer.models.ModelQualityGateInput": "omnibase_core.models.delegation.wire.model_quality_gate.ModelQualityGateInput",
    "omnimarket.nodes.node_delegation_routing_reducer.models.ModelDelegationConfig": "omnibase_core.models.delegation.wire.model_routing_config.ModelDelegationConfig",
    "omnimarket.nodes.node_delegation_routing_reducer.models.ModelRoutingTier": "omnibase_core.models.delegation.wire.model_routing_config.ModelRoutingTier",
    "omnimarket.nodes.node_delegation_routing_reducer.models.ModelTierModel": "omnibase_core.models.delegation.wire.model_routing_config.ModelTierModel",
    "omnimarket.nodes.node_budget_policy_compute.models.model_budget_limits.ModelBudgetLimits": "omnibase_core.models.delegation.wire.model_budget.ModelBudgetLimits",
}
OMNIMARKET_OWNED_DTO_IMPORTS = {
    "omnimarket.nodes.node_delegation_quality_gate_reducer.models.ModelQualityGateResult": "omnimarket.models.delegation.wire.model_quality_gate.ModelQualityGateResult",
}

DELEGATION_CONTRACTS = [
    "node_delegation_orchestrator",
    "node_delegation_quality_gate_reducer",
    "node_delegation_routing_reducer",
]
FORBIDDEN_COMPAT_WIRE_PREFIX = "omnibase_compat.contracts.delegation.wire"
FORBIDDEN_COMPAT_WIRE_BYTES = FORBIDDEN_COMPAT_WIRE_PREFIX.encode()
REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve(dotted_ref: str) -> Any:
    module_name, _, attr = dotted_ref.rpartition(".")
    return getattr(import_module(module_name), attr)


def _model_refs(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        name = value.get("name")
        module = value.get("module")
        if (
            isinstance(name, str)
            and isinstance(module, str)
            and (".models" in module or module.startswith("omnimarket.models."))
        ):
            refs.append(f"{module}.{name}")
        for child in value.values():
            refs.extend(_model_refs(child))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_model_refs(item))
    return refs


@pytest.mark.unit
@pytest.mark.parametrize(("market_ref", "canonical_ref"), DTO_IMPORTS.items())
def test_delegation_dto_market_paths_resolve_to_core(
    market_ref: str, canonical_ref: str
) -> None:
    """omnimarket shim paths resolve to canonical delegation wire DTOs in core (OMN-12659)."""
    market_model = _resolve(market_ref)
    canonical_model = _resolve(canonical_ref)

    assert market_model is canonical_model
    assert market_model.__module__.startswith("omnibase_core.models.delegation.wire.")


@pytest.mark.unit
def test_bifrost_delegation_config_adapter_shim_is_deleted() -> None:
    with pytest.raises(ModuleNotFoundError):
        import_module("omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config")


@pytest.mark.unit
def test_no_omnibase_compat_delegation_wire_refs_under_src() -> None:
    forbidden_refs = sorted(
        path
        for path in (REPO_ROOT / "src").rglob("*")
        if path.is_file() and FORBIDDEN_COMPAT_WIRE_BYTES in path.read_bytes()
    )

    assert forbidden_refs == []


@pytest.mark.unit
@pytest.mark.parametrize("node_name", DELEGATION_CONTRACTS)
def test_delegation_contract_model_refs_resolve_to_core_paths(
    node_name: str,
) -> None:
    """Contract model refs resolve through omnimarket shims to core wire DTOs (OMN-12659)."""
    contract_path = Path(
        resources.files("omnimarket.nodes") / node_name / "contract.yaml"  # type: ignore[arg-type]
    )
    contract = yaml.safe_load(contract_path.read_text())
    omnimarket_refs = [
        ref
        for ref in _model_refs(contract)
        if ref.startswith("omnimarket.") and not ref.endswith(".ModelRoutingDecision")
    ]
    allowed_refs = DTO_IMPORTS | OMNIMARKET_OWNED_DTO_IMPORTS
    unmapped_refs = sorted({ref for ref in omnimarket_refs if ref not in allowed_refs})

    assert not unmapped_refs
    model_refs = [ref for ref in omnimarket_refs if ref in DTO_IMPORTS]
    assert model_refs
    assert all(
        _resolve(model_ref).__module__.startswith(
            "omnibase_core.models.delegation.wire."
        )
        for model_ref in model_refs
    )


@pytest.mark.unit
def test_quality_gate_result_is_omnimarket_owned_until_core_evidence_promotion() -> (
    None
):
    market_model = _resolve(
        "omnimarket.nodes.node_delegation_quality_gate_reducer.models.ModelQualityGateResult"
    )
    local_model = _resolve(
        "omnimarket.models.delegation.wire.model_quality_gate.ModelQualityGateResult"
    )

    assert market_model is local_model
    assert market_model.__module__.startswith("omnimarket.models.delegation.wire.")
