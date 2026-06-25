# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13335 — escalation-exhaustion terminal must emit (no negative-savings crash).

Live finding (CID 67d2bfc8): an escalating ``code_generation`` delegation walks
the ladder, ``delegation-escalation-triggered.v1`` fires, the higher-tier
inference RESPONSE arrives — but the escalation silently EXHAUSTS the ladder
without ever producing a terminal. The escalated-to-ceiling delegation can never
yield a terminal with ``escalation_count >= 1``.

Root cause (proven by these tests, environment-independent): the orchestrator's
single terminal builder computes

    total_savings_usd = cost.cost_savings_usd - prior_attempt_cost_usd   (line ~1669)

On an escalation that ran a *metered* prior tier (e.g. cheap_cloud GLM) and then
landed on a FAILED / escalation-exhausted terminal where the final-tier cost
carries no premium counterfactual (``cost_savings_usd == 0.0``), this subtraction
drives ``total_savings_usd`` NEGATIVE. The builder then constructs
``ModelTaskDelegatedEvent`` with that negative ``cost_savings_usd`` — but the core
wire DTO pins ``cost_savings_usd`` with ``ge=0.0``. The construction raises
``ValidationError``, crashing the dispatcher with NO terminal event emitted: the
silent terminal loss the live run observed.

The honest floor for savings is ``0.0`` — an escalation that burned metered
budget before terminating did not "save" negative money (the spend is already
reflected in ``cost_usd``). The terminal builder must clamp savings to ``>= 0``
so a valid terminal is ALWAYS emitted, never lost to a ValidationError.

These tests drive the orchestrator handler over its per-step entrypoints exactly
as the bus chain would (routing decision -> inference response -> quality gate
result), exercising the metered-prior-tier escalation path that exhausts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import (
    ModelRoutingIntent,
    ModelTaskDelegatedEvent,
)

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

_CLOUD_METERED_MODEL = "glm-4.5"


def _request() -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Implement a function add(a, b) that returns the sum.",
        task_type="code_generation",  # type: ignore[arg-type]
        correlation_id=uuid4(),
        max_tokens=2048,
        emitted_at=datetime.now(UTC),
    )


def _routing_decision(
    cid: object, *, tier_name: str, model: str, endpoint: str
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,  # type: ignore[arg-type]
        task_type="code_generation",
        selected_model=model,
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{tier_name}"),
        endpoint_url=endpoint,
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="sp",
        rationale="r",
        tier_name=tier_name,
    )


def _inference(cid: object, content: str, model: str) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,  # type: ignore[arg-type]
        content=content,
        model_used=model,
        latency_ms=10,
        prompt_tokens=500,
        completion_tokens=200,
        total_tokens=700,
    )


def _failing_gate_result(cid: object) -> ModelQualityGateResult:
    """A sub-bar quality result that recommends fallback (drives escalation)."""
    return ModelQualityGateResult(
        correlation_id=cid,  # type: ignore[arg-type]
        passed=False,
        quality_score=0.2,
        fail_category="fail_heuristic",
        failure_reasons=("WEAK_OUTPUT: below required_bar",),
        fallback_recommended=True,
    )


@pytest.mark.unit
class TestEscalationExhaustionTerminalEmitsOmn13335:
    """An escalation that ran a metered prior tier must still emit a terminal.

    The metered-prior-tier escalation drives ``prior_attempt_cost_usd > 0``; when
    the ladder then exhausts (no higher routable tier) the terminal builder must
    NOT crash on a negative ``cost_savings_usd`` — it must emit a valid terminal
    so the delegation's outcome is durable.
    """

    def _drive_metered_then_exhaust(
        self, handler: HandlerDelegationWorkflow, cid: object
    ) -> list[object]:
        """Run a metered cheap_cloud tier, fail its gate, then exhaust the ladder.

        Leg 1 runs the metered ``cheap_cloud`` tier (banks a positive metered
        spend into ``prior_attempt_cost_usd``). When that gate fails, the
        orchestrator escalates to whatever the live config exposes; the eventual
        ladder exhaustion produces the FAILED terminal whose savings subtraction
        goes negative. Returns the terminal events from the exhausting leg.
        """
        handler.handle_routing_decision(
            _routing_decision(
                cid,
                tier_name="cheap_cloud",
                model=_CLOUD_METERED_MODEL,
                endpoint="https://cloud.test/glm/v1/chat/completions",
            )
        )
        handler.handle_inference_response(
            _inference(cid, "x = 1", _CLOUD_METERED_MODEL)
        )
        events = handler.handle_gate_result(_failing_gate_result(cid))

        # Walk any further escalation legs until the ladder exhausts (a terminal
        # — not a re-route — is produced). Each leg reuses a metered model so the
        # cumulative prior spend stays positive.
        guard = 0
        while any(isinstance(e, ModelRoutingIntent) for e in events) and guard < 6:
            guard += 1
            next_tier = next(
                e.min_tier_name for e in events if isinstance(e, ModelRoutingIntent)
            )
            handler.handle_routing_decision(
                _routing_decision(
                    cid,
                    tier_name=next_tier or "local",
                    model=_CLOUD_METERED_MODEL,
                    endpoint="https://cloud.test/glm/v1/chat/completions",
                )
            )
            handler.handle_inference_response(
                _inference(cid, "x = 2", _CLOUD_METERED_MODEL)
            )
            events = handler.handle_gate_result(_failing_gate_result(cid))
        return events

    def test_exhausted_escalation_emits_terminal_with_non_negative_savings(
        self,
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        request = _request()
        cid = request.correlation_id
        handler.handle_delegation_request(request)

        # Must NOT raise — the live gap raised ValidationError here (negative
        # cost_savings_usd against the core wire DTO's ge=0.0), losing the
        # terminal entirely.
        terminal_events = self._drive_metered_then_exhaust(handler, cid)

        compat_events = [
            e for e in terminal_events if isinstance(e, ModelTaskDelegatedEvent)
        ]
        assert len(compat_events) == 1, (
            "an exhausted escalation must emit exactly one terminal compat event "
            "(the live gap emitted none — the builder crashed on negative savings)"
        )
        assert compat_events[0].cost_savings_usd >= 0.0, (
            "terminal cost_savings_usd must be clamped to its honest floor (>= 0); "
            "a metered prior tier cannot produce negative savings"
        )
        # The metered prior tier really ran, so the terminal carries real cost.
        assert compat_events[0].cost_usd > 0.0, (
            "the metered escalation spend must surface as positive terminal cost"
        )
        # The terminal records the escalation that actually occurred.
        assert compat_events[0].escalation_count >= 1, (
            "an escalation terminal must carry escalation_count >= 1"
        )
