# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15193: contract-declared response validation replaces keyword heuristics.

Direct unit coverage of the ``delta`` reducer's ``response_contract`` branch --
the cross-boundary seam (wire -> handler -> dispatch port -> this reducer) is
covered separately in
tests/unit/nodes/node_delegate_skill_orchestrator/test_wire_response_contract_reaches_quality_gate_omn15193.py.
These tests pin the reducer's own contract: given a declared schema (passed
here as an explicit kwarg, ``delta``'s own API -- callers resolve it from
either an explicit per-request override or the task-class default, see
``resolve_task_class_response_contract``), structural validation REPLACES the
task-class DoD/legacy checks for that request, and ``response_contract=None``
is byte-identical to pre-OMN-15193 behavior FOR THAT PARAMETER -- ``delta``
itself never resolves a task-class default; that resolution is the CALLER's
job (dispatch port / bus-intent handler), one layer up.

OMN-15196 separately retired the ``agent_delegation`` task class's
``sub_tasks_verified``/``no_refusal`` DoD-heuristic entries in
task_class_contracts.v1.yaml (the keyword-heuristic dispatch-table entries
are deleted from handler_quality_gate.py too) -- a DIRECT ``delta`` call for
this task class with no ``response_contract`` at all now runs a SMALLER DoD
than before OMN-15196 (see
``test_same_response_without_declared_contract_passes_retired_dod`` below).
"""

from __future__ import annotations

import json
from uuid import uuid4

import jsonschema
import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

_TACTICAL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "action_params": {"type": "object"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["action", "action_params", "confidence", "rationale"],
}

_GOOD_TACTICAL_RESPONSE_WITH_I_CANNOT_RATIONALE = json.dumps(
    {
        "action": "hold_position",
        "action_params": {"unit_id": "u-17"},
        "confidence": 0.82,
        "rationale": (
            "i cannot confirm the enemy flank is clear from current sensor "
            "coverage, so holding position is the lower-risk tactical choice."
        ),
    }
)


def _agent_delegation_gate_input(content: str) -> ModelQualityGateInput:
    """Build a gate input carrying the REAL agent_delegation task-class DoD,
    mirroring what LocalDelegationDispatchPort._evaluate_quality_gate resolves."""
    dod_deterministic, dod_heuristic = resolve_task_class_dod_checks("agent_delegation")
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="agent_delegation",
        llm_response_content=content,
        dod_deterministic=dod_deterministic,
        dod_heuristic=dod_heuristic,
    )


@pytest.mark.unit
def test_declared_contract_passes_response_with_i_cannot_rationale() -> None:
    """The live-reproduced defect fix: a rationale containing "i cannot" as
    coherent prose passes when a declared schema is the acceptance authority --
    the no_refusal keyword heuristic is never consulted."""
    result = quality_gate_delta(
        _agent_delegation_gate_input(_GOOD_TACTICAL_RESPONSE_WITH_I_CANNOT_RATIONALE),
        response_contract=_TACTICAL_SCHEMA,
    )

    assert result.passed is True
    assert result.fail_category == "pass"
    assert result.quality_score == 1.0
    assert result.failure_reasons == ()
    assert result.fallback_recommended is False


@pytest.mark.unit
def test_same_response_without_declared_contract_passes_retired_dod() -> None:
    """OMN-15196: ``agent_delegation``'s task-class DoD (dod_deterministic=
    task_completed, dod_heuristic=semantic_adequacy -- ``resolve_task_class_
    dod_checks``'s SOURCE OF TRUTH, task_class_contracts.v1.yaml) no longer
    declares ``no_refusal``/``sub_tasks_verified`` -- both retired -- so a call
    into ``delta`` with NO ``response_contract`` at all (bypassing the
    dispatch-port/bus-intent layers that resolve the class's declared DEFAULT
    schema, OMN-15196's other seam) now has only ``task_completed``
    (non-empty) and ``semantic_adequacy`` (not truncated) as its DoD, and the
    "i cannot" tactical response satisfies both -- this is `delta`'s OWN
    contract given the (now-retired) YAML DoD, proving the keyword-heuristic
    entries are truly gone, not merely bypassed for this request."""
    result = quality_gate_delta(
        _agent_delegation_gate_input(_GOOD_TACTICAL_RESPONSE_WITH_I_CANNOT_RATIONALE)
    )

    assert result.passed is True
    assert not any("REFUSAL" in reason for reason in result.failure_reasons)
    assert not any("sub_tasks_verified" in reason for reason in result.failure_reasons)


@pytest.mark.unit
def test_same_response_empty_still_rejected_by_retired_dod() -> None:
    """The retired-heuristic DoD still hard-blocks a genuinely empty/truncated
    answer via task_completed/semantic_adequacy -- retiring the keyword
    heuristics did not silently disable every check for this class."""
    result = quality_gate_delta(_agent_delegation_gate_input("   "))

    assert result.passed is False
    assert any("empty" in reason for reason in result.failure_reasons)


@pytest.mark.unit
def test_declared_contract_missing_required_field_reports_specific_reason() -> None:
    content = json.dumps({"action": "hold_position", "confidence": 0.5})
    result = quality_gate_delta(
        _agent_delegation_gate_input(content),
        response_contract=_TACTICAL_SCHEMA,
    )

    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert result.fallback_recommended is True
    assert any(
        "SCHEMA_VIOLATION" in reason and "action_params" in reason
        for reason in result.failure_reasons
    )
    assert any(
        "SCHEMA_VIOLATION" in reason and "rationale" in reason
        for reason in result.failure_reasons
    )


@pytest.mark.unit
def test_declared_contract_wrong_type_reports_specific_reason() -> None:
    content = json.dumps(
        {
            "action": "hold_position",
            "action_params": {},
            "confidence": "very confident",  # wrong type
            "rationale": "clear conditions",
        }
    )
    result = quality_gate_delta(
        _agent_delegation_gate_input(content),
        response_contract=_TACTICAL_SCHEMA,
    )

    assert result.passed is False
    assert len(result.failure_reasons) == 1
    assert "SCHEMA_VIOLATION" in result.failure_reasons[0]
    assert "confidence" in result.failure_reasons[0]
    assert "is not of type 'number'" in result.failure_reasons[0]


@pytest.mark.unit
def test_declared_contract_rejects_malformed_json() -> None:
    result = quality_gate_delta(
        _agent_delegation_gate_input("{not valid json"),
        response_contract=_TACTICAL_SCHEMA,
    )

    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert len(result.failure_reasons) == 1
    assert result.failure_reasons[0].startswith("MALFORMED: response is not valid JSON")


@pytest.mark.unit
def test_declared_contract_rejects_empty_response() -> None:
    result = quality_gate_delta(
        _agent_delegation_gate_input("   "),
        response_contract=_TACTICAL_SCHEMA,
    )

    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert result.failure_reasons == (
        "MALFORMED: empty response fails response_contract validation",
    )


@pytest.mark.unit
def test_declared_contract_strips_thinking_traces_before_parsing() -> None:
    """A thinking-capable model's <think> preamble must not break JSON parsing,
    mirroring the legacy-path behavior (_strip_thinking_traces)."""
    content = (
        "<think>considering the tactical options here</think>"
        + _GOOD_TACTICAL_RESPONSE_WITH_I_CANNOT_RATIONALE
    )
    result = quality_gate_delta(
        _agent_delegation_gate_input(content),
        response_contract=_TACTICAL_SCHEMA,
    )

    assert result.passed is True


@pytest.mark.unit
def test_declared_contract_violation_reasons_are_deterministically_ordered() -> None:
    """Multiple violations sort by JSON-pointer path -- replay-stable ordering,
    not dict/set iteration order."""
    content = json.dumps({"confidence": "nope"})
    result = quality_gate_delta(
        _agent_delegation_gate_input(content),
        response_contract=_TACTICAL_SCHEMA,
    )

    assert result.passed is False
    # 'action' (missing, reported at root) sorts alongside the other missing
    # required fields; 'confidence' (a leaf path) sorts after root-level
    # violations. Assert stability across repeated calls rather than a single
    # brittle exact ordering.
    repeat = quality_gate_delta(
        _agent_delegation_gate_input(content),
        response_contract=_TACTICAL_SCHEMA,
    )
    assert result.failure_reasons == repeat.failure_reasons


@pytest.mark.unit
def test_invalid_response_contract_schema_raises_loudly() -> None:
    """A caller-authored invalid JSON Schema is a contract-authoring bug and
    must surface loudly (SchemaError), never silently pass every candidate."""
    with pytest.raises(jsonschema.exceptions.SchemaError):
        quality_gate_delta(
            _agent_delegation_gate_input("{}"),
            response_contract={"type": "not-a-real-json-schema-type"},
        )


@pytest.mark.unit
def test_response_contract_none_is_byte_identical_to_prior_behavior() -> None:
    """Explicitly passing response_contract=None (the default) must produce the
    exact same result as omitting the kwarg entirely."""
    gate_input = _agent_delegation_gate_input(
        _GOOD_TACTICAL_RESPONSE_WITH_I_CANNOT_RATIONALE
    )

    with_default = quality_gate_delta(gate_input)
    with_explicit_none = quality_gate_delta(gate_input, response_contract=None)

    assert with_default == with_explicit_none
