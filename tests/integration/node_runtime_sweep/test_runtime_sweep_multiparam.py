# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_runtime_sweep (OMN-13676).

COMPUTE node, Variant A. Despite the Wave-2 "self-collect" caveat, this handler
is already pure: ``RuntimeSweepRequest`` carries the *pre-collected* census
(contracts, producer/consumer topic sets, entry-point probes, workflow
observations). The harness/skill does the I/O; the node only classifies. So we
feed synthetic typed inputs directly — no seam, no subprocess monkeypatching.

Each case asserts typed result fields (status, by_type finding counts); the
negative-control cases each force a distinct finding class.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    EnumFindingType,
    EnumSweepCheck,
    ModelContractInput,
    ModelEntryPointProbe,
    ModelWorkflowObservation,
    NodeRuntimeSweep,
    RuntimeSweepRequest,
)

_REAL_DESC = "A real, sufficiently long node description."


def _clean_contract() -> ModelContractInput:
    return ModelContractInput(
        node_name="node_clean",
        description=_REAL_DESC,
        handler_module="omnimarket.nodes.node_clean.handlers.handler_clean",
        handler_exists=True,
        publish_topics=["onex.evt.omnimarket.clean.v1"],
        subscribe_topics=["onex.evt.omnimarket.clean.v1"],
    )


# (id, request, expected_status, expected_finding_type | None)
CASES = [
    pytest.param(
        RuntimeSweepRequest(contracts=[_clean_contract()]),
        "clean",
        None,
        id="all-wired-symmetric-clean",
    ),
    pytest.param(
        RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_unwired",
                    description=_REAL_DESC,
                    handler_module="omnimarket.nodes.node_unwired.handlers.missing",
                    handler_exists=False,
                )
            ]
        ),
        "findings",
        EnumFindingType.UNWIRED_HANDLER,
        id="unwired-handler",
    ),
    pytest.param(
        RuntimeSweepRequest(
            topic_producers=["onex.evt.omnimarket.orphan.v1"],
            topic_consumers=[],
        ),
        "findings",
        EnumFindingType.PRODUCER_ONLY,
        id="topic-producer-without-consumer",
    ),
    pytest.param(
        RuntimeSweepRequest(
            topic_producers=[],
            topic_consumers=["onex.evt.omnimarket.dangling.v1"],
        ),
        "findings",
        EnumFindingType.CONSUMER_ONLY,
        id="topic-consumer-without-producer",
    ),
    pytest.param(
        RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_placeholder",
                    description="compute+deadbeef",
                    handler_exists=True,
                )
            ]
        ),
        "findings",
        EnumFindingType.PLACEHOLDER_DESCRIPTION,
        id="placeholder-description",
    ),
    pytest.param(
        RuntimeSweepRequest(
            workflow_observations=[
                ModelWorkflowObservation(
                    correlation_id=uuid4(),
                    archetype="orchestrator",
                    workflow_state="RUNNING",
                    elapsed_ms=900_000,
                    reached_terminal=False,
                )
            ]
        ),
        "findings",
        EnumFindingType.STRANDED_WORKFLOW,
        id="stranded-workflow-past-sla",
    ),
    pytest.param(
        RuntimeSweepRequest(
            entry_point_probes=[
                ModelEntryPointProbe(
                    node_name="node_broken",
                    module_path="omnimarket.nodes.node_broken",
                    ok=False,
                    reason="contract.yaml missing",
                )
            ]
        ),
        "findings",
        EnumFindingType.BROKEN_ENTRY_POINT,
        id="broken-entry-point-probe",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("request_model", "expected_status", "expected_finding_type"),
    [(c.values[0], c.values[1], c.values[2]) for c in CASES],
    ids=[c.id for c in CASES],
)
def test_runtime_sweep_multiparam(
    request_model: RuntimeSweepRequest,
    expected_status: str,
    expected_finding_type: EnumFindingType | None,
) -> None:
    result = NodeRuntimeSweep().handle(request_model)

    assert result.status == expected_status
    assert result.contracts_checked == len(request_model.contracts)
    assert result.entry_points_checked == len(request_model.entry_point_probes)
    assert result.workflows_checked == len(request_model.workflow_observations)

    if expected_finding_type is None:
        assert result.findings == [], f"expected clean, got {result.by_type}"
        assert result.total_findings == 0
    else:
        # Negative control: the specific finding class must be present and typed.
        assert expected_finding_type in result.by_type, (
            f"expected {expected_finding_type}, got {result.by_type}"
        )
        for finding in result.findings:
            assert finding.subject
            assert finding.message
            assert finding.severity in {"CRITICAL", "WARNING", "INFO"}


@pytest.mark.integration
def test_runtime_sweep_enabled_checks_scopes_phases() -> None:
    """enabled_checks subset runs only the named phase.

    A contract that is BOTH unwired AND has a placeholder description yields two
    finding classes under a full sweep, but scoping to [WIRING] must emit only
    the UNWIRED_HANDLER finding — proving the phase-selection param axis.
    """
    contract = ModelContractInput(
        node_name="node_double_fault",
        description="compute+abc123",  # would trip PLACEHOLDER_DESCRIPTION
        handler_module="omnimarket.nodes.node_double_fault.handlers.missing",
        handler_exists=False,  # would trip UNWIRED_HANDLER
    )

    full = NodeRuntimeSweep().handle(RuntimeSweepRequest(contracts=[contract]))
    assert EnumFindingType.UNWIRED_HANDLER in full.by_type
    assert EnumFindingType.PLACEHOLDER_DESCRIPTION in full.by_type

    scoped = NodeRuntimeSweep().handle(
        RuntimeSweepRequest(
            contracts=[contract], enabled_checks=[EnumSweepCheck.WIRING]
        )
    )
    assert set(scoped.by_type) == {EnumFindingType.UNWIRED_HANDLER}
