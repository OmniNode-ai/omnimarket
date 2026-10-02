# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plain prose instructions must agree with the transport output boundary."""

import pytest

from omnimarket.delegation.response_contract_instruction import (
    render_extraction_marker_instruction,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as workflow_module,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from tests.unit.delegation.test_omn19434_bus_path_trace_only_refusal import (
    _UNMARKED_ANSWER,
    _drive,
)
from tests.unit.delegation.test_omn19434_bus_path_trace_only_refusal import (
    _bifrost_contract as _bifrost_contract,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]


def test_prose_marker_instruction_explains_metadata_is_removed() -> None:
    instruction = render_extraction_marker_instruction("### ANSWER")
    assert "transport metadata" in instruction
    assert "removed" in instruction
    assert "even when" in instruction


def test_unmarked_prose_preserves_gate_evidence_and_names_boundary() -> None:
    workflow, request, intent = _drive(_UNMARKED_ANSWER)
    state = workflow._workflows[request.correlation_id]
    assert intent.payload.llm_response_content == _UNMARKED_ANSWER
    raw_result = HandlerQualityGateIntent().handle(intent)
    result = workflow_module._gate_result_with_output_refusal(state, raw_result)
    assert not result.passed
    assert result.fail_category == "fail_deterministic"
    assert any("ambiguous_unmarked_deliverable" in r for r in result.failure_reasons)
    assert all("empty response" not in r for r in result.failure_reasons)
    assert state.inference_content == ""
