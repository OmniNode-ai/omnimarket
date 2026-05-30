# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the producer-side market-contract -> ModelHandlerContract adapter.

OMN-12463: the adapter must transform market node ``contract.yaml`` shapes into a
payload that validates as ``omnibase_core``'s ``ModelHandlerContract`` and that
the runtime descriptor parser (``omnibase_infra`` ``ContractYamlParser.parse``)
can materialize into a handler descriptor pointing at the omnimarket handler.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import yaml
from omnibase_core.models.contracts.model_handler_contract import (
    ModelHandlerContract,
)
from omnibase_infra.runtime.kafka_contract_source import ContractYamlParser

from omnimarket.nodes.node_contract_registry.market_contract_adapter import (
    MarketContractAdapterError,
    to_handler_contract_payload,
)

# Compute archetype: top-level handler block + handler_routing, FQ input_model
# string, no output_model declared.
_COMPUTE_CONTRACT: dict[str, Any] = {
    "name": "aislop_sweep",
    "node_type": "compute",
    "contract_version": {"major": 1, "minor": 0, "patch": 0},
    "description": "Detect AI-generated quality anti-patterns across repos.",
    "handler": {
        "module": "omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep",
        "class": "NodeAislopSweep",
        "input_model": (
            "omnimarket.nodes.node_aislop_sweep.handlers."
            "handler_aislop_sweep.AislopSweepRequest"
        ),
    },
    "handler_routing": {
        "routing_strategy": "operation_match",
        "handlers": [
            {
                "operation": "sweep",
                "handler": {
                    "name": "NodeAislopSweep",
                    "module": (
                        "omnimarket.nodes.node_aislop_sweep.handlers."
                        "handler_aislop_sweep"
                    ),
                },
            }
        ],
    },
    "descriptor": {
        "node_archetype": "compute",
        "purity": "pure",
        "idempotent": True,
        "timeout_ms": 120000,
    },
}

# Orchestrator archetype: no top-level handler block; handler + input model live
# in handler_routing (mapping-shaped event_model); non-canonical purity value.
_ORCHESTRATOR_CONTRACT: dict[str, Any] = {
    "name": "ab_compare_orchestrator",
    "node_type": "orchestrator",
    "contract_version": {"major": 1, "minor": 0, "patch": 0},
    "handler_routing": {
        "routing_strategy": "operation_match",
        "handlers": [
            {
                "handler": {
                    "name": "HandlerAbCompareOrchestrator",
                    "module": (
                        "omnimarket.nodes.node_ab_compare_orchestrator.handlers."
                        "handler_ab_compare_orchestrator"
                    ),
                },
                "event_model": {
                    "name": "ModelAbCompareStart",
                    "module": (
                        "omnimarket.nodes.node_ab_compare_orchestrator.models."
                        "model_ab_compare_start"
                    ),
                },
            }
        ],
    },
    "descriptor": {
        "node_archetype": "orchestrator",
        "purity": "effectful",
        "idempotent": False,
        "timeout_ms": 300000,
    },
}


@pytest.mark.unit
def test_compute_contract_maps_to_valid_handler_contract() -> None:
    payload = to_handler_contract_payload(_COMPUTE_CONTRACT, "node_aislop_sweep")

    contract = ModelHandlerContract.model_validate(payload)

    assert contract.handler_id == "node.aislop_sweep"
    assert contract.name == "aislop_sweep"
    assert contract.descriptor.node_archetype.value == "compute"
    assert contract.descriptor.purity == "pure"
    assert contract.descriptor.idempotent is True
    assert contract.input_model == (
        "omnimarket.nodes.node_aislop_sweep.handlers."
        "handler_aislop_sweep.AislopSweepRequest"
    )
    # No output_model declared -> canonical handler return type.
    assert contract.output_model == (
        "omnibase_core.models.dispatch.model_handler_output.ModelHandlerOutput"
    )
    # handler_class is carried both top-level and (the parser-read) metadata key.
    expected_class = (
        "omnimarket.nodes.node_aislop_sweep.handlers."
        "handler_aislop_sweep.NodeAislopSweep"
    )
    assert payload["metadata"]["handler_class"] == expected_class
    assert contract.handler_class == expected_class


@pytest.mark.unit
def test_orchestrator_contract_maps_and_normalizes_purity() -> None:
    payload = to_handler_contract_payload(
        _ORCHESTRATOR_CONTRACT, "node_ab_compare_orchestrator"
    )

    contract = ModelHandlerContract.model_validate(payload)

    assert contract.handler_id == "node.ab_compare_orchestrator"
    assert contract.descriptor.node_archetype.value == "orchestrator"
    # 'effectful' is normalized to the canonical 'side_effecting' literal.
    assert contract.descriptor.purity == "side_effecting"
    # input_model derived from handler_routing event_model mapping -> module.name
    assert contract.input_model == (
        "omnimarket.nodes.node_ab_compare_orchestrator.models."
        "model_ab_compare_start.ModelAbCompareStart"
    )
    assert contract.handler_class == (
        "omnimarket.nodes.node_ab_compare_orchestrator.handlers."
        "handler_ab_compare_orchestrator.HandlerAbCompareOrchestrator"
    )


@pytest.mark.unit
def test_generic_node_type_normalizes_to_archetype() -> None:
    contract_data = dict(_COMPUTE_CONTRACT)
    contract_data["node_type"] = "COMPUTE_GENERIC"
    # Remove descriptor so archetype must be derived from node_type.
    contract_data = {k: v for k, v in contract_data.items() if k != "descriptor"}

    payload = to_handler_contract_payload(contract_data, "node_aislop_sweep")
    contract = ModelHandlerContract.model_validate(payload)

    assert contract.descriptor.node_archetype.value == "compute"
    # No declared purity + compute archetype -> defaults to pure.
    assert contract.descriptor.purity == "pure"


@pytest.mark.unit
def test_missing_input_model_fails_fast() -> None:
    contract_data: dict[str, Any] = {
        "name": "no_input",
        "node_type": "effect",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "handler": {
            "module": "omnimarket.nodes.node_no_input.handlers.handler_no_input",
            "class": "HandlerNoInput",
        },
        "descriptor": {"node_archetype": "effect"},
    }

    with pytest.raises(MarketContractAdapterError, match="no input model"):
        to_handler_contract_payload(contract_data, "node_no_input")


@pytest.mark.unit
def test_missing_handler_class_fails_fast() -> None:
    contract_data: dict[str, Any] = {
        "name": "no_handler",
        "node_type": "orchestrator",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "handler_routing": {
            "routing_strategy": "operation_match",
            "handlers": [
                {
                    "routing_key": "subscribe",
                    "handler_key": "HandlerSubscription.subscribe",
                }
            ],
        },
        "input_model": "omnimarket.nodes.node_no_handler.models.ModelStart",
        "descriptor": {"node_archetype": "orchestrator"},
    }

    with pytest.raises(MarketContractAdapterError, match="no handler module/class"):
        to_handler_contract_payload(contract_data, "node_no_handler")


@pytest.mark.unit
def test_missing_contract_version_fails_fast() -> None:
    contract_data = {
        k: v for k, v in _COMPUTE_CONTRACT.items() if k != "contract_version"
    }

    with pytest.raises(MarketContractAdapterError, match="contract_version"):
        to_handler_contract_payload(contract_data, "node_aislop_sweep")


@pytest.mark.unit
def test_unmappable_archetype_fails_fast() -> None:
    contract_data = dict(_COMPUTE_CONTRACT)
    contract_data["descriptor"] = {"node_archetype": "totally_unknown"}
    contract_data["node_type"] = "totally_unknown"

    with pytest.raises(MarketContractAdapterError, match="unmappable archetype"):
        to_handler_contract_payload(contract_data, "node_aislop_sweep")


@pytest.mark.unit
def test_round_trip_through_contract_yaml_parser() -> None:
    """The adapted payload materializes into a handler descriptor end-to-end.

    Proves the producer output is consumable by the runtime descriptor parser:
    serialized adapted YAML -> ContractYamlParser.parse -> ModelHandlerDescriptor
    with the omnimarket handler_class.
    """
    payload = to_handler_contract_payload(_COMPUTE_CONTRACT, "node_aislop_sweep")
    contract_yaml = yaml.safe_dump(payload, sort_keys=False)

    parser = ContractYamlParser(environment="test")
    descriptor = parser.parse("node_aislop_sweep", contract_yaml, uuid.uuid4())

    assert descriptor.handler_id == "node.aislop_sweep"
    assert descriptor.handler_kind == "compute"
    assert descriptor.handler_class == (
        "omnimarket.nodes.node_aislop_sweep.handlers."
        "handler_aislop_sweep.NodeAislopSweep"
    )
    assert descriptor.input_model == (
        "omnimarket.nodes.node_aislop_sweep.handlers."
        "handler_aislop_sweep.AislopSweepRequest"
    )
