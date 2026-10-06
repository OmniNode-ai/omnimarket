# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stop the recorded prose/compilation mismatch after one retry."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationFailed,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.state_codec import decode, encode
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
    ModelQualityRuleEvaluation,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

_OPENING = (
    "Write a GitHub PR body in markdown from these facts. No preamble, no "
    "commentary, output only the body."
)


@pytest.mark.parametrize("opening", [_OPENING, _OPENING.replace("Write a", "Draft a")])
def test_falsifier_pair_routes_to_document(opening: str) -> None:
    authority = load_task_class_authority()
    assert authority.resolve_task_type(opening, explicit=None).task_type == "document"


@pytest.fixture
def ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[hw.HandlerDelegationWorkflow, UUID]:
    handler = hw.HandlerDelegationWorkflow(workflows={})
    monkeypatch.setattr(hw, "is_free_tier", lambda _tier: True)
    monkeypatch.setattr(hw, "tier_max_retries", lambda _tier: 2)
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt=_OPENING,
            task_type="code_generation",
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
        )
    )
    return handler, cid


def _answer(
    handler: hw.HandlerDelegationWorkflow,
    cid: UUID,
    *,
    failed_rule: str | None = "compiles_without_errors",
    error: str = "MALFORMED: response does not compile as Python: invalid decimal literal",
    finish_reason: EnumProviderFinishReason = EnumProviderFinishReason.STOP,
    passed: bool = False,
    tier: str = "local",
    real_gate: bool = False,
) -> list[BaseModel]:
    handler.handle_routing_decision(
        ModelRoutingDecision(
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
            rationale="Controlled route for the recorded mismatch.",
            tier_name=tier,
            dod_deterministic=("compiles_without_errors", "response_non_empty"),
        )
    )
    contract = handler.workflows[cid].effective_deliverable_contract
    assert contract is not None
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content=f"{contract.render_start_marker}\nThe change adds retry checks.",
            model_used="fixture-model",
            latency_ms=10,
        )
    )
    assert handler.workflows[cid].state is EnumDelegationState.INFERENCE_COMPLETED
    evaluations = (
        (
            ModelQualityRuleEvaluation(
                rule=failed_rule, enforcement="blocking", passed=False, detail=error
            ),
        )
        if failed_rule is not None and not passed
        else ()
    )
    if real_gate:
        result = delta(
            ModelQualityGateInput(
                correlation_id=cid,
                task_type="code_generation",
                llm_response_content=handler.workflows[cid].inference_content or "",
                dod_deterministic=("compiles_without_errors", "response_non_empty"),
                min_response_length=1,
            )
        )
        assert result.fail_category == "fail_deterministic"
        assert any(
            e.rule == "compiles_without_errors" and not e.passed
            for e in result.rule_evaluations
        )
        return handler.handle_gate_result(result)
    return handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=passed,
            quality_score=1.0 if passed else 0.433,
            fail_category="pass" if passed else "fail_deterministic",
            failure_reasons=() if passed else (error,),
            fallback_recommended=not passed,
            rule_evaluations=evaluations,
            finish_reason=finish_reason,
        )
    )


@pytest.mark.parametrize("retry_budget", [0, 2])
def test_second_identical_floor_stops_and_names_class_after_state_roundtrip(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
    monkeypatch: pytest.MonkeyPatch,
    retry_budget: int,
) -> None:
    handler, cid = ladder
    monkeypatch.setattr(hw, "tier_max_retries", lambda _tier: retry_budget)
    monkeypatch.setattr(
        hw, "next_eligible_tier", lambda *_args, **_kwargs: "cheap_cloud"
    )
    first = _answer(handler, cid)
    route = next(event for event in first if isinstance(event, ModelRoutingIntent))
    handler.workflows[cid] = decode(encode(handler.workflows[cid]))

    second = _answer(
        handler,
        cid,
        error="MALFORMED: response does not compile as Python: invalid syntax",
        tier=route.min_tier_name or "local",
    )

    assert not any(isinstance(event, ModelRoutingIntent) for event in second)
    failed = next(event for event in second if isinstance(event, ModelDelegationFailed))
    assert failed.terminal_failure_reason == "repeated_deterministic_floor"
    assert "task_class=code_generation" in failed.failure_reason
    assert "review task classification" in failed.failure_reason
    assert "invalid syntax" not in failed.failure_reason
    attempts = handler.workflows[cid].escalation_history
    assert len(attempts) == 2
    assert attempts[-1].acceptance_decision == "terminate"
    assert attempts[-1].acceptance_reason == "deterministic_floor_failed"


@pytest.mark.parametrize(
    ("first_rule", "second_rule", "finish_reason"),
    [
        (
            "compiles_without_errors",
            "response_non_empty",
            EnumProviderFinishReason.STOP,
        ),
        (
            "compiles_without_errors",
            "compiles_without_errors",
            EnumProviderFinishReason.LENGTH,
        ),
        (None, "compiles_without_errors", EnumProviderFinishReason.STOP),
        ("compiles_without_errors", None, EnumProviderFinishReason.STOP),
        ("semantic_adequacy", "semantic_adequacy", EnumProviderFinishReason.STOP),
    ],
)
def test_changing_truncated_or_unproven_failures_keep_retrying(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
    first_rule: str | None,
    second_rule: str | None,
    finish_reason: EnumProviderFinishReason,
) -> None:
    handler, cid = ladder
    _answer(handler, cid, failed_rule=first_rule, finish_reason=finish_reason)
    second = _answer(handler, cid, failed_rule=second_rule)
    assert any(isinstance(event, ModelRoutingIntent) for event in second)
    assert not any(isinstance(event, ModelDelegationFailed) for event in second)


def test_a_successful_retry_is_accepted(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
) -> None:
    handler, cid = ladder
    _answer(handler, cid)
    _answer(handler, cid, passed=True)
    assert handler.workflows[cid].state is EnumDelegationState.COMPLETED


def test_prose_rejected_by_the_real_compilation_floor_stops_after_one_retry(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
) -> None:
    handler, cid = ladder
    first = _answer(handler, cid, real_gate=True)
    assert any(isinstance(event, ModelRoutingIntent) for event in first)
    second = _answer(handler, cid, real_gate=True)
    failed = next(event for event in second if isinstance(event, ModelDelegationFailed))
    assert failed.terminal_failure_reason == "repeated_deterministic_floor"
    assert "task_class=code_generation" in failed.failure_reason
    assert len(handler.workflows[cid].escalation_history) == 2
