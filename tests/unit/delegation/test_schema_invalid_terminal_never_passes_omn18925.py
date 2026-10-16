# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18925 AC-2: an accepted terminal is never structurally invalid and quality-passing.

A response that does not satisfy the response contract its request declared
must end as a failed, schema-rejected, unusable terminal with
``quality_passed=False``. The two places that could let it through are both
driven for real here: the orchestrator (a declared contract through the quality
gate to the terminal) and the terminal model's own invariant (a completed
terminal cannot be built over an unusable content verdict).

This file is a pre-merge gate, not only a test: ci.yml's lint job and the
``schema-invalid-terminal-never-passes`` pre-commit hook run it by name, and
``tests/ci/test_schema_invalid_terminal_gate_wiring_omn18925.py`` fails the
build if either is removed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import (
    EnumDelegationContentVerdict,
    EnumDelegationOperationalOutcome,
)
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationCompleted,
    ModelDelegationFailed,
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_quality_gate_intent import (
    ModelQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = pytest.mark.unit

# Higher than any task class's escalation ceiling, so a rejection ends as the
# FAILED terminal instead of another routing intent.
_LADDER_EXHAUSTED = 99

_CONTRACT: dict[str, object] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}

_STRUCTURALLY_INVALID = [
    pytest.param('{"unrelated": "field"}', id="required-field-missing"),
    pytest.param('{"summary": 5}', id="wrong-type"),
    pytest.param("{}", id="empty-object"),
    pytest.param("[]", id="wrong-root-type"),
    pytest.param("not json at all", id="not-json"),
]


def _request(cid: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
        response_contract=_CONTRACT,
    )


def _routing_decision(cid: UUID) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="test",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(
            NAMESPACE_DNS, "omninode.ai/backends/qwen3-coder-30b"
        ),
        endpoint_url="http://lab-llm.invalid:8000/v1/chat/completions",
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=4096,
        system_prompt="You are a test generation assistant.",
        rationale="Task 'test' routed to qwen3-coder-30b.",
    )


async def _terminal_for(content: str) -> ModelDelegationResult:
    """Drive *content* from the provider through the real gate to the terminal."""
    cid = uuid4()
    handler = HandlerDelegationWorkflow()
    await handler.handle(_request(cid))
    await handler.handle(_routing_decision(cid))
    events = await handler.handle(
        ModelInferenceResponseData(
            correlation_id=cid,
            content=content,
            model_used="qwen3-coder-30b",
            latency_ms=50,
        )
    )
    intent = next(e for e in events if isinstance(e, ModelQualityGateIntent))
    gate = HandlerQualityGateIntent().handle(intent)
    handler.workflows[cid].escalation_count = _LADDER_EXHAUSTED
    terminals = [
        e for e in await handler.handle(gate) if isinstance(e, ModelDelegationResult)
    ]
    assert len(terminals) == 1, f"expected exactly one terminal, got {terminals!r}"
    return terminals[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", _STRUCTURALLY_INVALID)
async def test_structurally_invalid_response_never_ends_quality_passing(
    content: str,
) -> None:
    terminal = await _terminal_for(content)

    assert not isinstance(terminal, ModelDelegationCompleted)
    assert isinstance(terminal, ModelDelegationFailed)
    assert terminal.quality_passed is False
    assert (
        terminal.operational_outcome is EnumDelegationOperationalOutcome.SCHEMA_REJECTED
    )
    assert terminal.content_verdict is EnumDelegationContentVerdict.UNUSABLE


@pytest.mark.asyncio
async def test_conforming_response_ends_completed_and_usable() -> None:
    # Positive control: the matrix above discriminates, because a response that
    # satisfies the same contract does pass.
    terminal = await _terminal_for('{"summary": "valid artifact"}')

    assert isinstance(terminal, ModelDelegationCompleted)
    assert terminal.quality_passed is True
    assert terminal.operational_outcome is EnumDelegationOperationalOutcome.COMPLETED
    assert terminal.content_verdict is EnumDelegationContentVerdict.USABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        EnumDelegationOperationalOutcome.SCHEMA_REJECTED,
        EnumDelegationOperationalOutcome.QUALITY_REJECTED,
        EnumDelegationOperationalOutcome.COMPLETED,
    ],
)
async def test_completed_terminal_cannot_be_built_over_an_unusable_verdict(
    outcome: EnumDelegationOperationalOutcome,
) -> None:
    completed = await _terminal_for('{"summary": "valid artifact"}')
    assert isinstance(completed, ModelDelegationCompleted)

    with pytest.raises(ValidationError, match="quality_passed requires usable"):
        ModelDelegationCompleted.model_validate(
            {
                **completed.model_dump(),
                "operational_outcome": outcome,
                "content_verdict": EnumDelegationContentVerdict.UNUSABLE,
            }
        )
