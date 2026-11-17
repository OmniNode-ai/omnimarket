# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus delegation ladder stops inside the caller's own budget [OMN-25384].

The delegate-skill handler sends ``requested_timeout_seconds`` (the task class's
execution budget, measured from the request's ``emitted_at``) and stops waiting
for the terminal at that budget plus its delivery margin, committing a
budget-timeout terminal of its own. The bus workflow read only its node's 900 s
completion bound, so it kept re-drafting and escalating past the caller's
budget. Measured on the dev lane: golden case I8 (correlation 7e493b70) ran five
attempts over 279.5 s of workflow time, its real FAILED terminal reached the
broker at 16:40:13Z, and the handler's budget-timeout terminal had reached it at
16:39:04Z, 303.9 s after pickup. Both were projected onto the same
``delegation_events`` row, and the nightly scored whichever it read.

The in-process port already bounds its ladder by the run deadline: a same-tier
re-draft leaves the class's next-rung reserve, and a rung is reached only with
its rung floor left. These tests hold the bus workflow to the same two rules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.inference.task_class_authority import (
    resolve_task_class_execution_budget,
    resolve_task_class_rung_floor_seconds,
)
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
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
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = pytest.mark.usefixtures("stub_provider_quota_reader")

_TASK_TYPE = "code_generation"
_BUDGET_SECONDS = resolve_task_class_execution_budget(
    _TASK_TYPE
).task_class_timeout_ceiling_seconds
_RESERVE_SECONDS = resolve_task_class_execution_budget(
    _TASK_TYPE
).next_rung_reserve_seconds
_FLOOR_SECONDS = resolve_task_class_rung_floor_seconds(_TASK_TYPE)
_LADDER_NEXT: dict[str, str] = {"local": "cheap_cloud", "cheap_cloud": "claude"}


@pytest.fixture
def ladder_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``local`` is free with two re-drafts; paid tiers climb a fixed ladder."""
    monkeypatch.setattr(hw, "is_free_tier", lambda tier: tier == "local")
    monkeypatch.setattr(
        hw, "tier_max_retries", lambda tier: 2 if tier == "local" else 0
    )
    monkeypatch.setattr(
        hw,
        "next_eligible_tier",
        lambda current, _excluded, **_kwargs: _LADDER_NEXT.get(current),
    )


def _request(cid: UUID, *, seconds_left: float | None) -> ModelDelegationRequest:
    """A request whose caller budget has ``seconds_left`` of it remaining now."""
    if seconds_left is None:
        return ModelDelegationRequest(
            prompt="Produce an impossible artifact.",
            task_type=_TASK_TYPE,  # type: ignore[arg-type]
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
        )
    return ModelDelegationRequest(
        prompt="Produce an impossible artifact.",
        task_type=_TASK_TYPE,  # type: ignore[arg-type]
        correlation_id=cid,
        requested_timeout_seconds=_BUDGET_SECONDS,
        emitted_at=datetime.now(UTC)
        - timedelta(seconds=_BUDGET_SECONDS - seconds_left),
    )


def _routing(cid: UUID, tier_name: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type=_TASK_TYPE,
        selected_model="Qwen3.8-27B",
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{tier_name}"),
        endpoint_url="http://localhost:8000",
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a code generation assistant.",
        rationale=f"Task routed via tier '{tier_name}'.",
        tier_name=tier_name,
    )


def _inference(cid: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="This task is impossible: the halting problem is undecidable.",
        model_used="Qwen3.8-27B",
        latency_ms=42,
        prompt_tokens=100,
        completion_tokens=200,
        total_tokens=300,
    )


def _fail_gate(cid: UUID) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=cid,
        passed=False,
        quality_score=0.5,
        failure_reasons=("score_below_required_bar",),
        fallback_recommended=True,
    )


def _first_draft_failed(
    seconds_left: float | None, tier_name: str = "local"
) -> tuple[HandlerDelegationWorkflow, UUID, list[object]]:
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(_request(cid, seconds_left=seconds_left))
    handler.handle_routing_decision(_routing(cid, tier_name))
    handler.handle_inference_response(_inference(cid))
    assert handler.workflows[cid].state == EnumDelegationState.INFERENCE_COMPLETED
    return handler, cid, list(handler.handle_gate_result(_fail_gate(cid)))


@pytest.mark.unit
class TestBusLadderHonorsCallerBudget:
    def test_budget_values_are_the_ones_the_rules_read(self) -> None:
        assert _FLOOR_SECONDS is not None
        assert _RESERVE_SECONDS > _FLOOR_SECONDS

    def test_no_caller_budget_keeps_the_redraft(self, ladder_env: None) -> None:
        """A request that names no budget is unchanged: the free tier re-drafts."""
        handler, cid, intents = _first_draft_failed(seconds_left=None)
        routing = [i for i in intents if isinstance(i, ModelRoutingIntent)]
        assert [r.min_tier_name for r in routing] == ["local"]
        assert handler.workflows[cid].local_retry_count == 1

    def test_redraft_that_leaves_the_reserve_is_sent(self, ladder_env: None) -> None:
        handler, cid, intents = _first_draft_failed(seconds_left=_RESERVE_SECONDS + 30)
        routing = [i for i in intents if isinstance(i, ModelRoutingIntent)]
        assert [r.min_tier_name for r in routing] == ["local"]
        assert handler.workflows[cid].state == EnumDelegationState.ROUTED

    def test_redraft_without_the_reserve_climbs_instead(self, ladder_env: None) -> None:
        """Less than the reserve left, at least the floor: the next rung, no re-draft."""
        assert _FLOOR_SECONDS is not None
        handler, cid, intents = _first_draft_failed(
            seconds_left=(_FLOOR_SECONDS + _RESERVE_SECONDS) / 2
        )
        routing = [i for i in intents if isinstance(i, ModelRoutingIntent)]
        assert [r.min_tier_name for r in routing] == ["cheap_cloud"]
        assert any(
            isinstance(i, ModelLlmDelegationEscalationTriggeredEvent) for i in intents
        )
        wf = handler.workflows[cid]
        assert wf.local_retry_count == 0
        assert wf.escalation_count == 1

    @pytest.mark.parametrize("tier_name", ["local", "cheap_cloud"])
    def test_less_than_the_floor_left_closes_the_run_failed(
        self, ladder_env: None, tier_name: str
    ) -> None:
        """The I8 shape: the run ends FAILED now, inside the caller's budget."""
        assert _FLOOR_SECONDS is not None
        handler, cid, intents = _first_draft_failed(
            seconds_left=_FLOOR_SECONDS - 15, tier_name=tier_name
        )
        assert not [i for i in intents if isinstance(i, ModelRoutingIntent)]
        terminals = [i for i in intents if isinstance(i, ModelDelegationFailed)]
        assert len(terminals) == 1
        terminal = terminals[0]
        assert terminal.quality_passed is False
        assert str(terminal.terminal_failure_cause) in {"timeout", "TIMEOUT"} or (
            getattr(terminal.terminal_failure_cause, "value", None) == "timeout"
        )
        assert terminal.terminal_failure_reason is not None
        assert terminal.terminal_failure_reason.startswith("caller_budget_spent")
        # The budget-timeout marker belongs to the handler's own cancellation;
        # this terminal is the workflow's FAILED verdict, read as ``failed``.
        assert "handler execution budget" not in (terminal.failure_reason or "")
        assert handler.workflows[cid].state == EnumDelegationState.FAILED

    def test_budget_already_spent_closes_the_run_failed(self, ladder_env: None) -> None:
        handler, cid, intents = _first_draft_failed(seconds_left=-5)
        assert not [i for i in intents if isinstance(i, ModelRoutingIntent)]
        assert handler.workflows[cid].state == EnumDelegationState.FAILED
