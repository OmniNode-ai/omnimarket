# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployed orchestrator runs a request's named tests before its gate (OMN-17427).

The bus request now carries the tests a code answer is graded on, inline. The
orchestrator runs them in the named-test sandbox against the answer it is about
to gate, and the gate reducer reads the run from the intent. These tests drive
the deployed chain: request, routing decision, inference response, gate intent,
gate reducer. A passing answer is accepted, a failing one rejected, a request
that named no tests keeps its UNTESTED decision, and a request that named tests
no runner ran is refused as INSUFFICIENT, never accepted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelDelegationNamedTests

from omnimarket.models.delegation.wire.model_named_test_run_request import (
    EnumNamedTestRunStatus,
    ModelNamedTestRunRequest,
    ModelNamedTestRunResult,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    NO_NAMED_TEST_RUN_REASON,
    NO_NAMED_TESTS_UNTESTED_REASON,
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
)
from omnimarket.nodes.node_delegation_orchestrator.models import (
    ModelDelegationRequest,
    ModelInferenceResponseData,
    ModelQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = pytest.mark.unit

PROMPT = "add two ints"
CORRECT = "def add(a, b):\n    return a + b"
WRONG = "def add(a, b):\n    return a - b"
_TEST_ID = "test_add.py::test_add"
_TEST_SOURCE = (
    "from add_impl import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
)


def _named_tests() -> ModelDelegationNamedTests:
    return ModelDelegationNamedTests.model_validate(
        {
            "test_ids": [_TEST_ID],
            "answer_path": "add_impl.py",
            "test_files": [{"path": "test_add.py", "content": _TEST_SOURCE}],
        }
    )


def _no_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw, "next_eligible_tier", lambda *_a, **_k: None)
    monkeypatch.setattr(hw, "tier_max_retries", lambda _tier: 0)
    monkeypatch.setattr(hw, "is_free_tier", lambda _tier: False)


def _routing(cid: UUID) -> ModelRoutingDecision:
    deterministic, heuristic = resolve_task_class_dod_checks(
        "code_generation", prompt=PROMPT
    )
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="code_generation",
        selected_model="fixture-model",
        selected_backend_id=uuid4(),
        selected_backend_ref="fixture-backend",
        endpoint_url="http://provider.invalid:8000",
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=2048,
        system_prompt="Return the requested artifact.",
        rationale="Controlled single-tier route.",
        tier_name="local",
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )


async def _gate_intent(
    code: str,
    *,
    named_tests: ModelDelegationNamedTests | None,
    handler: hw.HandlerDelegationWorkflow | None = None,
) -> ModelQualityGateIntent:
    handler = handler or hw.HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt=PROMPT,
            task_type="code_generation",
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
            named_tests=named_tests,
        )
    )
    handler.handle_routing_decision(_routing(cid))
    contract = handler.workflows[cid].effective_deliverable_contract
    assert contract is not None
    body = f"### ANSWER\n```python\n{code}\n```"
    events = await handler.handle(
        ModelInferenceResponseData(
            correlation_id=cid,
            content=(
                f"{contract.render_start_marker}\n{body}"
                if contract.render_start_marker
                else body
            ),
            model_used="fixture-model",
            latency_ms=10,
        )
    )
    return next(e for e in events if isinstance(e, ModelQualityGateIntent))


def _verdict(intent: ModelQualityGateIntent) -> ModelQualityGateResult:
    return HandlerQualityGateIntent().handle(intent)


@pytest.mark.asyncio
async def test_a_passing_answer_is_accepted_on_the_tests_the_request_carries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_escalation(monkeypatch)
    intent = await _gate_intent(CORRECT, named_tests=_named_tests())
    run = intent.payload.named_test_run
    assert run is not None
    assert run.passed
    assert run.test_ids == (_TEST_ID,)
    assert intent.payload.named_tests_requested is True
    assert _verdict(intent).passed is True


@pytest.mark.asyncio
async def test_a_failing_answer_is_rejected_on_the_tests_the_request_carries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_escalation(monkeypatch)
    intent = await _gate_intent(WRONG, named_tests=_named_tests())
    run = intent.payload.named_test_run
    assert run is not None
    assert not run.passed
    assert run.failed_test_ids == (_TEST_ID,)
    assert _verdict(intent).passed is False


@pytest.mark.asyncio
async def test_a_request_naming_no_tests_stays_untested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_escalation(monkeypatch)
    intent = await _gate_intent(CORRECT, named_tests=None)
    assert intent.payload.named_test_run is None
    assert intent.payload.named_tests_requested is False
    verdict = _verdict(intent)
    assert verdict.passed is True
    assert [
        evaluation.detail
        for evaluation in verdict.rule_evaluations
        if evaluation.rule == "passes_existing_tests"
    ] == [NO_NAMED_TESTS_UNTESTED_REASON]


@pytest.mark.asyncio
async def test_named_tests_no_runner_ran_are_refused_never_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_escalation(monkeypatch)
    seen: list[ModelNamedTestRunRequest] = []

    def refusing(request: ModelNamedTestRunRequest) -> ModelNamedTestRunResult:
        seen.append(request)
        return ModelNamedTestRunResult(
            status=EnumNamedTestRunStatus.REFUSED, detail="pytest is not installed"
        )

    handler = hw.HandlerDelegationWorkflow(workflows={}, named_test_runner=refusing)
    intent = await _gate_intent(CORRECT, named_tests=_named_tests(), handler=handler)
    assert len(seen) == 1
    assert seen[0].spec.test_ids == (_TEST_ID,)
    assert seen[0].spec.workspace_root is None
    assert intent.payload.named_test_run is None
    assert intent.payload.named_tests_requested is True
    verdict = _verdict(intent)
    assert verdict.passed is False
    assert NO_NAMED_TEST_RUN_REASON in verdict.failure_reasons
