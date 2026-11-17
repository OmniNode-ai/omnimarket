# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run on a customer's own key is priced from the declared model price (OMN-24874).

A tenant overlay route wholesale-replaces the backend, and its decision carries the
tier name ``tenant_overlay``. The routing registry declares no such tier, so both
cost sites (the workflow terminal and the projection's re-measurement) priced the
run through ``resolve_tier_cost`` as ``no_cost_model`` and stored ``cost_usd`` 0.0,
while the catalogue declares a price for the model on that endpoint. These tests
pin both sites: the terminal is priced from the declared price, the projection keeps
that priced cost instead of overwriting it with the zero re-measurement, and a model
no table prices stays unpriced.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    EnumDelegationContentVerdict,
    EnumDelegationOperationalOutcome,
    ModelDelegationRequest,
)

from omnimarket.handlers.routing.byok_provider_backends import (
    byok_declared_price_per_1m,
    resolve_byok_provider_backend,
)
from omnimarket.models.model_tenant_routing_overlay_backend import (
    TENANT_OVERLAY_TIER_NAME,
    ModelTenantRoutingOverlayBackend,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
    TerminalEmissionInputs,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.tenant_overlay_decision import (
    build_tenant_overlay_decision,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_dod_resolution import (
    ModelDodResolution,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    ModelProjectionTaskDelegatedEvent,
    _canonical_result_to_task_delegated_payload,
    _measure_actual_cost,
    _tenant_overlay_cost_usd,
)
from omnimarket.pricing import (
    _load_routing_config,
    build_premium_counterfactual,
    resolve_tier_cost,
)

_GENERAL_ENDPOINT = "https://api.z.ai/api/paas/v4/chat/completions"
_MODEL = "glm-5.3-flash"
# Read on h053 during this review (2026-11-16): psql on the dev analytics store selected
# endpoint_url and model_name from delegation_routing_tenant_overlay for the
# R8 probe tenant. The canonical delegation-completed topic was consumed from
# its broker through rpk; terminal cdee0cf7-683e-4b67-8e6c-d981b8fd1422 carries
# the same endpoint_url and model_used. Sanitized fields and source commands
# are recorded in tests/fixtures/delegation_scenario_alarm/probe_route.json.
_PROBE_ENDPOINT = "https://api.z.ai/api/paas/v4/chat/completions"
_R8_MODEL_USED = "glm-5.3-flash"
_PROMPT_TOKENS = 300
_COMPLETION_TOKENS = 200


# Pinned literal (review finding 6): 300 prompt and 200 completion tokens of glm-5.3-flash on the general endpoint,
# at the catalogue's declared per-1M prices. Not recomputed through the code's own price lookup.
_PINNED_DECLARED_COST_USD = 0.000145


def _declared_cost() -> float:
    return _PINNED_DECLARED_COST_USD


@pytest.mark.unit
def test_the_pinned_cost_literal_is_the_catalogue_price_of_the_pinned_tokens() -> None:
    # The one place the literal is tied to the catalogue: a catalogue price change turns this red, and the pin is then re-read.
    price = byok_declared_price_per_1m(_GENERAL_ENDPOINT, _MODEL)
    assert price is not None, "the catalogue must declare the model's price"
    price_in, price_out = price
    assert float(
        (Decimal(_PROMPT_TOKENS) * price_in + Decimal(_COMPLETION_TOKENS) * price_out)
        / Decimal(1_000_000)
    ) == pytest.approx(_PINNED_DECLARED_COST_USD)


@pytest.mark.unit
def test_the_tenant_overlay_tier_has_no_registry_cost_model() -> None:
    # The premise of the defect: the tier name the overlay decision carries is not a registry tier. Read the loaded registry's own
    # tier names, so a registry that gains a tenant_overlay tier turns this red (resolve_tier_cost returns None for the name
    # whatever the registry holds, so its result alone cannot show the premise).
    registry_tiers = {tier.name for tier in _load_routing_config().tiers}
    assert registry_tiers, "the routing registry loaded no tier"
    assert (
        "local" in registry_tiers
    )  # positive control: the registry read is the real one
    assert TENANT_OVERLAY_TIER_NAME not in registry_tiers
    assert resolve_tier_cost(TENANT_OVERLAY_TIER_NAME) is None


@pytest.mark.unit
def test_terminal_cost_on_a_tenant_overlay_route_is_the_declared_price() -> None:
    counterfactual = build_premium_counterfactual(
        prompt_tokens=_PROMPT_TOKENS, completion_tokens=_COMPLETION_TOKENS
    )
    cost = HandlerDelegationWorkflow._measure_terminal_cost(
        tier_name=TENANT_OVERLAY_TIER_NAME,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
        premium_counterfactual=counterfactual,
        endpoint_url=_GENERAL_ENDPOINT,
        model_id=_MODEL,
    )
    expected = _declared_cost()
    assert expected > 0.0
    assert cost.cash_cost_usd == pytest.approx(expected)
    assert cost.cost_measurement_source == "metered"
    assert cost.cost_tier_type == "metered"
    assert cost.cost_tier_name == TENANT_OVERLAY_TIER_NAME
    assert counterfactual is not None
    assert cost.cost_savings_usd == pytest.approx(
        float(counterfactual.counterfactual_cost_usd) - expected
    )


@pytest.mark.unit
def test_an_undeclared_model_on_a_tenant_overlay_route_stays_unpriced() -> None:
    cost = HandlerDelegationWorkflow._measure_terminal_cost(
        tier_name=TENANT_OVERLAY_TIER_NAME,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
        premium_counterfactual=None,
        endpoint_url=_GENERAL_ENDPOINT,
        model_id="glm-undeclared-model",
    )
    assert cost.cash_cost_usd == 0.0
    assert cost.cost_measurement_source == "no_cost_model"


@pytest.mark.unit
def test_a_zero_token_tenant_overlay_terminal_returns_the_measurement_not_a_metered_zero() -> (
    None
):
    # A declared price over no served tokens is a cash of 0: the terminal returns the registry measurement (the projection
    # re-measures the same run as no_cost_model), never a ``metered`` zero the projection stores differently.
    cost = HandlerDelegationWorkflow._measure_terminal_cost(
        tier_name=TENANT_OVERLAY_TIER_NAME,
        prompt_tokens=0,
        completion_tokens=0,
        premium_counterfactual=None,
        endpoint_url=_GENERAL_ENDPOINT,
        model_id=_MODEL,
    )
    assert cost.cash_cost_usd == 0.0
    assert cost.cost_measurement_source == "no_cost_model"
    assert cost.cost_tier_type != "metered"


@pytest.mark.unit
def test_the_terminal_cost_measure_takes_the_endpoint_and_model_with_no_default() -> (
    None
):
    # A caller that leaves them out is a TypeError, not a silent "" that reads as an unpriced model.
    measure: Any = HandlerDelegationWorkflow._measure_terminal_cost
    with pytest.raises(TypeError):
        measure(
            tier_name=TENANT_OVERLAY_TIER_NAME,
            prompt_tokens=_PROMPT_TOKENS,
            completion_tokens=_COMPLETION_TOKENS,
            premium_counterfactual=None,
        )


@pytest.mark.unit
def test_projection_keeps_the_priced_terminal_cost_of_a_tenant_overlay_run() -> None:
    counterfactual = build_premium_counterfactual(
        prompt_tokens=_PROMPT_TOKENS, completion_tokens=_COMPLETION_TOKENS
    )
    assert counterfactual is not None
    priced = _declared_cost()
    event = ModelProjectionTaskDelegatedEvent(
        correlation_id="omn-24874-organic-control",
        task_type="summarization",
        delegated_to=_MODEL,
        model_name=_MODEL,
        quality_gate_passed=True,
        cost_usd=priced,
        tokens_input=_PROMPT_TOKENS,
        tokens_output=_COMPLETION_TOKENS,
        cost_tier_name=TENANT_OVERLAY_TIER_NAME,
        cost_tier_type="metered",
        cost_measurement_source="metered",
        premium_counterfactual=counterfactual,
    )
    measured = _measure_actual_cost(event)
    assert measured.cost_usd == pytest.approx(priced)
    assert measured.cost_savings_usd == pytest.approx(
        float(counterfactual.counterfactual_cost_usd) - priced
    )
    assert measured.cost_measurement_source == "metered"
    assert measured.cost_tier_name == TENANT_OVERLAY_TIER_NAME


def _probe_overlay_event(model: str) -> ModelProjectionTaskDelegatedEvent:
    """A zero-cost tenant overlay terminal on the probe tenant's own endpoint, the shape the live R8 terminal has."""
    return ModelProjectionTaskDelegatedEvent(
        correlation_id="omn-24874-probe",
        task_type="summarization",
        delegated_to=model,
        model_name=model,
        endpoint_url=_PROBE_ENDPOINT,
        quality_gate_passed=True,
        cost_usd=0.0,
        tokens_input=_PROMPT_TOKENS,
        tokens_output=_COMPLETION_TOKENS,
        cost_tier_name=TENANT_OVERLAY_TIER_NAME,
        premium_counterfactual=build_premium_counterfactual(
            prompt_tokens=_PROMPT_TOKENS, completion_tokens=_COMPLETION_TOKENS
        ),
    )


@pytest.mark.unit
def test_projection_still_floors_an_unpriced_unmodelled_tier_to_zero() -> None:
    # The probe carries the endpoint the real terminal carries, so the model alone decides: the declared model is priced on
    # it (the control) and the undeclared one is not. A probe with no endpoint prices nothing and cannot tell them apart.
    control = _measure_actual_cost(_probe_overlay_event(_MODEL))
    assert control.cost_usd == pytest.approx(_PINNED_DECLARED_COST_USD)
    assert control.cost_measurement_source == "metered"
    measured = _measure_actual_cost(_probe_overlay_event("glm-undeclared-model"))
    assert measured.cost_usd == 0.0
    assert measured.cost_measurement_source == "no_cost_model"


@pytest.mark.unit
def test_the_probe_without_an_endpoint_prices_nothing_so_it_needs_the_endpoint() -> (
    None
):
    # Why the probe sets the endpoint: with it removed even the declared model reads unpriced.
    event = _probe_overlay_event(_MODEL).model_copy(update={"endpoint_url": None})
    assert _measure_actual_cost(event).cost_usd == 0.0


@pytest.mark.unit
def test_the_projection_floor_rounds_to_ten_places_like_the_workflow_terminal() -> None:
    # A declared price with more places than the catalogue's, so the unrounded quotient and the ten-place one differ.
    price = (Decimal("0.123456789123"), Decimal("0.987654321987"))
    projection = (
        "omnimarket.nodes.node_projection_delegation.handlers."
        "handler_projection_delegation.byok_declared_price_per_1m"
    )
    workflow = (
        "omnimarket.nodes.node_delegation_orchestrator.handlers."
        "handler_delegation_workflow.byok_declared_price_per_1m"
    )
    event = _probe_overlay_event(_MODEL).model_copy(
        update={"tokens_input": 7, "tokens_output": 11}
    )
    with patch(projection, return_value=price), patch(workflow, return_value=price):
        floor = _tenant_overlay_cost_usd(event)
        terminal = HandlerDelegationWorkflow._measure_terminal_cost(
            tier_name=TENANT_OVERLAY_TIER_NAME,
            prompt_tokens=7,
            completion_tokens=11,
            premium_counterfactual=None,
            endpoint_url=_PROBE_ENDPOINT,
            model_id=_MODEL,
        )
    assert terminal.cash_cost_usd == round(terminal.cash_cost_usd, 10)
    assert floor == terminal.cash_cost_usd


_UNMODELLED_TIER = "a_tier_the_registry_never_declared"


def _overlay_decision() -> Any:
    """The decision a tenant overlay row yields: the catalogue's default GLM backend, as the probe tenant's route is minted."""
    backend = resolve_byok_provider_backend("glm", plan=None)
    assert backend is not None
    assert backend.endpoint_url == _PROBE_ENDPOINT
    overlay = ModelTenantRoutingOverlayBackend(
        tenant_id="readiness-probe",
        task_type="summarization",
        backend_id=backend.backend_id,
        endpoint_url=_PROBE_ENDPOINT,
        model_name=_R8_MODEL_USED,
        models=(_R8_MODEL_USED,),
        provider=backend.provider,
        secret_ref="tenant-credential-ref",
    )
    return build_tenant_overlay_decision(
        ModelDelegationRequest(
            correlation_id=uuid4(),
            task_type="summarization",
            prompt="x" * 100,
            emitted_at=datetime.now(tz=UTC),
            tenant_id="readiness-probe",
        ),
        task_type="summarization",
        overlay=overlay,
        estimated_tokens=500,
        dod_resolution=ModelDodResolution(),
        system_prompt="",
        backend_id=uuid4(),
    )


@pytest.mark.unit
def test_the_probe_tenant_route_endpoint_resolves_a_declared_price() -> None:
    # The probe tenant's route is minted from the catalogue's default GLM plan; its endpoint must resolve a price.
    decision = _overlay_decision()
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.endpoint_url == _PROBE_ENDPOINT
    assert decision.selected_model == _R8_MODEL_USED
    assert byok_declared_price_per_1m(_PROBE_ENDPOINT, _R8_MODEL_USED) is not None


def _terminal_inputs(
    *,
    tier_name: str,
    endpoint_url: str,
    model: str,
    prompt_tokens: int = _PROMPT_TOKENS,
    completion_tokens: int = _COMPLETION_TOKENS,
) -> TerminalEmissionInputs:
    return TerminalEmissionInputs(
        completed=True,
        correlation_id=uuid4(),
        task_type="summarization",
        model_used=model,
        endpoint_url=endpoint_url,
        content="a one sentence summary",
        quality_passed=True,
        quality_score=1.0,
        operational_outcome=EnumDelegationOperationalOutcome.COMPLETED,
        content_verdict=EnumDelegationContentVerdict.USABLE,
        latency_ms=900,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        fallback_to_claude=False,
        failure_reason="",
        tokens_to_compliance=0,
        compliance_attempts=1,
        cost_tier_name=tier_name,
        premium_counterfactual=build_premium_counterfactual(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
        escalation_count=0,
        escalation_history=(),
        terminal_failure_reason=None,
        routing_tiers_hash=None,
        escalation_config_hash=None,
        attempts_count=1,
        model_name=model,
        session_id=None,
        quality_gates_checked=[],
        quality_gates_failed=[],
        llm_call_id="chatcmpl-omn24918",
        context_pack_hash="",
    )


@pytest.mark.unit
def test_the_terminal_builder_prices_a_real_tenant_overlay_decision() -> None:
    # The call site: _emit_terminal hands the decision's endpoint and model to the pricing fallback.
    decision = _overlay_decision()
    events = HandlerDelegationWorkflow()._emit_terminal(
        _terminal_inputs(
            tier_name=decision.tier_name,
            endpoint_url=_PROBE_ENDPOINT,
            model=_R8_MODEL_USED,
        )
    )
    canonical = next(e for e in events if isinstance(e, ModelDelegationResult))
    assert canonical.cumulative_attempt_cost > 0.0
    assert canonical.cumulative_attempt_cost == pytest.approx(_declared_cost())


@pytest.mark.unit
def test_a_tier_the_registry_does_not_model_is_not_priced_from_the_declared_price() -> (
    None
):
    # Only the tenant overlay tier takes the declared price; any other unmodelled tier stays no_cost_model.
    cost = HandlerDelegationWorkflow._measure_terminal_cost(
        tier_name=_UNMODELLED_TIER,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
        premium_counterfactual=None,
        endpoint_url=_GENERAL_ENDPOINT,
        model_id=_MODEL,
    )
    assert cost.cash_cost_usd == 0.0
    assert cost.cost_measurement_source == "no_cost_model"


def _projection_event(
    *,
    tier: str,
    cost: float,
    source: str,
    model: str = _MODEL,
    backend_id: str | None = None,
    headroom: float = 0.0,
) -> ModelProjectionTaskDelegatedEvent:
    return ModelProjectionTaskDelegatedEvent(
        correlation_id=str(uuid4()),
        task_type="summarization",
        delegated_to=model,
        model_name=model,
        quality_gate_passed=True,
        cost_usd=cost,
        tokens_input=_PROMPT_TOKENS,
        tokens_output=_COMPLETION_TOKENS,
        cost_tier_name=tier,
        cost_tier_type="metered" if cost > 0 else "",
        cost_measurement_source=source,
        premium_counterfactual=build_premium_counterfactual(
            prompt_tokens=_PROMPT_TOKENS, completion_tokens=_COMPLETION_TOKENS
        ),
        backend_id=backend_id,
        budget_headroom_consumed_usd=headroom,
    )


@pytest.mark.unit
def test_projection_does_not_trust_a_producer_cost_on_a_tier_without_a_cost_model() -> (
    None
):
    # A producer's positive cost on a tier that is not the tenant overlay is no reason to skip the registry re-measurement.
    measured = _measure_actual_cost(
        _projection_event(tier=_UNMODELLED_TIER, cost=5.0, source="metered")
    )
    assert measured.cost_usd == 0.0
    assert measured.cost_measurement_source == "no_cost_model"


@pytest.mark.unit
def test_a_positive_cost_tenant_overlay_event_marked_no_cost_model_is_stored_metered() -> (
    None
):
    # The mixed case: a priced cost carrying the zero re-measurement's source must not keep that source.
    priced = _declared_cost()
    measured = _measure_actual_cost(
        _projection_event(
            tier=TENANT_OVERLAY_TIER_NAME, cost=priced, source="no_cost_model"
        )
    )
    assert measured.cost_usd == pytest.approx(priced)
    assert measured.cost_measurement_source == "metered"
    assert measured.cost_tier_type == "metered"


@pytest.mark.unit
def test_headroom_on_the_event_of_a_tenant_overlay_run_is_not_stored() -> None:
    # Own-key cost never draws down the platform's ceiling headroom. The event carries headroom above 0, so a
    # pass-through of it (handler_projection_delegation.py, the overlay branch) turns this red; with the default 0.0
    # on the event the same assertion could never fail (review finding 2).
    priced = _declared_cost()
    event = _projection_event(
        tier=TENANT_OVERLAY_TIER_NAME,
        cost=priced,
        source="no_cost_model",
        headroom=2.5,
    )
    assert event.budget_headroom_consumed_usd == 2.5
    measured = _measure_actual_cost(event)
    assert measured.cost_usd == pytest.approx(priced)
    assert measured.headroom_consumed_usd == 0.0


@pytest.mark.unit
def test_headroom_on_the_emit_terminal_result_of_an_overlay_run_is_not_stored() -> None:
    # The workflow draws no headroom for a metered tier type: pricing.py books ``metered`` cash with no headroom,
    # handler_delegation_workflow.py never names headroom, and the canonical terminal has no headroom field. So the
    # converted terminal carries none, and a producer that stamps one anyway is not believed on an overlay run.
    decision = _overlay_decision()
    events = HandlerDelegationWorkflow()._emit_terminal(
        _terminal_inputs(
            tier_name=decision.tier_name,
            endpoint_url=_PROBE_ENDPOINT,
            model=_R8_MODEL_USED,
        )
    )
    canonical = next(e for e in events if isinstance(e, ModelDelegationResult))
    assert "budget_headroom_consumed_usd" not in type(canonical).model_fields
    converted = _canonical_result_to_task_delegated_payload(
        canonical.model_dump(mode="json")
    )
    assert converted.get("budget_headroom_consumed_usd") in (None, 0, 0.0)
    stamped = ModelProjectionTaskDelegatedEvent.model_validate(
        {**converted, "budget_headroom_consumed_usd": 1.75}
    )
    measured = _measure_actual_cost(stamped)
    assert measured.cost_usd == pytest.approx(_declared_cost())
    assert measured.headroom_consumed_usd == 0.0


def _zero_cost_terminal_event(
    *, endpoint_url: str, model: str
) -> ModelProjectionTaskDelegatedEvent:
    """The projection event of a terminal that priced nothing, built through the decision's terminal and the converter.

    omnimarket#6331's case: a terminal from an older runtime or another producer arrives with its cost 0. The terminal is
    the real ``_emit_terminal`` result of an overlay decision, its cost fields zeroed the way that producer left them,
    and it reaches the projection only through ``_canonical_result_to_task_delegated_payload`` (review finding 1).
    """
    decision = _overlay_decision()
    events = HandlerDelegationWorkflow()._emit_terminal(
        _terminal_inputs(
            tier_name=decision.tier_name,
            endpoint_url=endpoint_url,
            model=model,
        )
    )
    canonical = next(e for e in events if isinstance(e, ModelDelegationResult))
    payload = canonical.model_dump(mode="json")
    payload["cumulative_attempt_cost"] = 0.0
    payload["final_attempt_cost"] = 0.0
    return ModelProjectionTaskDelegatedEvent.model_validate(
        _canonical_result_to_task_delegated_payload(payload)
    )


@pytest.mark.unit
def test_a_zero_cost_overlay_terminal_from_the_decision_is_priced_through_the_converter() -> (
    None
):
    event = _zero_cost_terminal_event(
        endpoint_url=_PROBE_ENDPOINT, model=_R8_MODEL_USED
    )
    # What a real terminal carries: the canonical result has no backend_id, so the event's is empty; the endpoint is not.
    assert event.backend_id is None
    assert event.endpoint_url == _PROBE_ENDPOINT
    assert event.cost_usd == 0.0
    measured = _measure_actual_cost(event)
    assert measured.cost_usd == pytest.approx(_PINNED_DECLARED_COST_USD)
    assert measured.cost_measurement_source == "metered"
    assert measured.cost_tier_type == "metered"


@pytest.mark.unit
def test_a_zero_cost_terminal_on_a_house_endpoint_or_undeclared_model_stays_unpriced() -> (
    None
):
    house = _measure_actual_cost(
        _zero_cost_terminal_event(
            endpoint_url="http://house-rung.invalid:8000/v1/chat/completions",
            model=_R8_MODEL_USED,
        )
    )
    undeclared = _measure_actual_cost(
        _zero_cost_terminal_event(
            endpoint_url=_PROBE_ENDPOINT, model="glm-undeclared-model"
        )
    )
    for measured in (house, undeclared):
        assert measured.cost_usd == 0.0
        assert measured.cost_measurement_source == "no_cost_model"


@pytest.mark.unit
def test_the_canonical_terminal_cost_reaches_the_projection_from_the_overlay_decision() -> (
    None
):
    # End to end through the converter: terminal built from the decision, projected, stored cost above 0.
    decision = _overlay_decision()
    events = HandlerDelegationWorkflow()._emit_terminal(
        _terminal_inputs(
            tier_name=decision.tier_name,
            endpoint_url=decision.endpoint_url,
            model=decision.selected_model,
        )
    )
    canonical = next(e for e in events if isinstance(e, ModelDelegationResult))
    converted = _canonical_result_to_task_delegated_payload(
        canonical.model_dump(mode="json")
    )
    measured = _measure_actual_cost(
        ModelProjectionTaskDelegatedEvent.model_validate(converted)
    )
    assert measured.cost_usd == pytest.approx(_declared_cost())
    assert measured.cost_measurement_source == "metered"


@pytest.mark.unit
def test_probe_literals_match_the_host_overlay_and_terminal_reading() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "fixtures/delegation_scenario_alarm/probe_route.json"
    )
    reading = json.loads(path.read_text(encoding="utf-8"))
    assert reading["overlay"]["endpoint_url"] == _PROBE_ENDPOINT
    assert reading["terminal"]["endpoint_url"] == _PROBE_ENDPOINT
    assert reading["terminal"]["model_used"] == _R8_MODEL_USED
    backend = resolve_byok_provider_backend("glm", plan=None)
    assert backend is not None
    assert backend.endpoint_url == reading["overlay"]["endpoint_url"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("endpoint", "model"), [(None, _MODEL), (_GENERAL_ENDPOINT, None)]
)
def test_projection_with_missing_route_identity_stays_unpriced(
    endpoint: str | None, model: str | None
) -> None:
    event = _projection_event(
        tier=TENANT_OVERLAY_TIER_NAME, cost=0.0, source="no_cost_model"
    )
    event = event.model_copy(update={"endpoint_url": endpoint, "model_name": model})
    measured = _measure_actual_cost(event)
    assert measured.cost_usd == 0.0
    assert measured.cost_measurement_source == "no_cost_model"


@pytest.mark.unit
def test_omn24874_tenant_overlay_attempt_is_priced_at_the_declared_price() -> None:
    # Review finding 5: the attempt-pricing site hard-coded endpoint_url=None, so a
    # tenant_overlay attempt recorded before a retry or escalation was priced
    # no_cost_model (0.0) and the cumulative cost undercounted. The attempt is the
    # current route's, so the site reads the endpoint from the routing decision.
    from omnimarket.enums.enum_delegation_acceptance import (
        EnumDelegationAcceptanceDecision,
        EnumDelegationAcceptanceReason,
    )
    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        DelegationWorkflowState,
    )
    from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_escalation_attempt import (
        ModelDelegationEscalationAttempt,
    )

    decision = _overlay_decision()
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    workflow = DelegationWorkflowState(
        correlation_id=uuid4(),
        routing_decision=decision,
        current_tier_name=TENANT_OVERLAY_TIER_NAME,
    )
    attempt = ModelDelegationEscalationAttempt(
        tier_name=TENANT_OVERLAY_TIER_NAME,
        model_used=_R8_MODEL_USED,
        quality_score=0.0,
        latency_ms=10,
        fallback_recommended=True,
        attempted_at=datetime.now(UTC),
        acceptance_decision=EnumDelegationAcceptanceDecision.CLIMB,
        acceptance_reason=EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED,
        routing_decision_id=decision.selected_backend_id,
    )
    handler = HandlerDelegationWorkflow()
    cost = handler._record_escalation_attempt(
        workflow,
        attempt,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
    )
    assert cost == pytest.approx(_declared_cost())
    assert workflow.escalation_history[-1].cost_usd == pytest.approx(_declared_cost())
    # Positive control: a platform tier's attempt is still priced from the registry
    # (free_local -> 0.0), not from the declared price.
    local = ModelDelegationEscalationAttempt(
        tier_name="local",
        model_used="Qwen3.6-35B-A3B",
        quality_score=0.0,
        latency_ms=10,
        fallback_recommended=True,
        attempted_at=datetime.now(UTC),
        acceptance_decision=EnumDelegationAcceptanceDecision.CLIMB,
        acceptance_reason=EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED,
    )
    assert handler._record_escalation_attempt(
        workflow,
        local,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
    ) == pytest.approx(0.0)


def _escalation_attempt(routing_decision_id: Any) -> Any:
    from omnimarket.enums.enum_delegation_acceptance import (
        EnumDelegationAcceptanceDecision,
        EnumDelegationAcceptanceReason,
    )
    from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_escalation_attempt import (
        ModelDelegationEscalationAttempt,
    )

    return ModelDelegationEscalationAttempt(
        tier_name=TENANT_OVERLAY_TIER_NAME,
        model_used=_R8_MODEL_USED,
        quality_score=0.0,
        latency_ms=10,
        fallback_recommended=True,
        attempted_at=datetime.now(UTC),
        acceptance_decision=EnumDelegationAcceptanceDecision.CLIMB,
        acceptance_reason=EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED,
        routing_decision_id=routing_decision_id,
    )


@pytest.mark.unit
def test_omn24874_an_attempt_is_priced_at_its_own_endpoint_not_the_current_one() -> (
    None
):
    # Review finding 3 (OMN-25411): the attempt was priced from whatever decision
    # the workflow held, not the decision the attempt names. An attempt whose
    # routing_decision_id names another decision takes no endpoint from the
    # current one, so it is not priced at the current route's declared price.
    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        DelegationWorkflowState,
    )

    decision = _overlay_decision()
    workflow = DelegationWorkflowState(
        correlation_id=uuid4(),
        routing_decision=decision,
        current_tier_name=TENANT_OVERLAY_TIER_NAME,
    )
    handler = HandlerDelegationWorkflow()
    foreign = handler._record_escalation_attempt(
        workflow,
        _escalation_attempt(uuid4()),
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
    )
    assert foreign == 0.0
    assert workflow.escalation_history[-1].cost_usd == 0.0
    # Positive control: the attempt that names the current decision is priced.
    own = handler._record_escalation_attempt(
        workflow,
        _escalation_attempt(decision.selected_backend_id),
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
    )
    assert own == pytest.approx(_declared_cost())


_GLM_BACKEND_REF = "byok-glm-general"
_TENANT_ID = "a0ea038d-8538-494c-b004-4ceaf9677fac"
_TENANT_KEY_REF = f"cred_{_TENANT_ID}_glm_911110ccc89c4b6db98203024ba23371"


def _glm_customer_route(correlation_id: Any) -> Any:
    from uuid import NAMESPACE_DNS, uuid5

    from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
        ModelRoutingDecision,
    )

    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="summarization",
        selected_model=_R8_MODEL_USED,
        selected_backend_id=uuid5(
            NAMESPACE_DNS, f"omninode.ai/backends/{_GLM_BACKEND_REF}"
        ),
        selected_backend_ref=_GLM_BACKEND_REF,
        endpoint_url=_PROBE_ENDPOINT,
        api_key_ref=_TENANT_KEY_REF,
        cost_tier="tenant_byok",
        tier_name=TENANT_OVERLAY_TIER_NAME,
        max_context_tokens=128000,
        timeout_ms=300000,
        max_tokens=65536,
        system_prompt="s",
        rationale="tenant overlay",
    )


@pytest.mark.unit
@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_omn24874_driven_tenant_overlay_retries_reach_the_terminal_cumulative_and_skip_the_budget() -> (
    None
):
    # Review finding 3 (OMN-25411): drive the real call site (an inference error on
    # the customer's own route, retried through _maybe_retry_customer_route until
    # the catalogue's retry budget ends), emit the terminal, and read the
    # cumulative cost over a non-empty escalation_history and the budget row it
    # would draw.
    from omnibase_core.models.delegation.wire import ModelDelegationFailed

    from omnimarket.handlers.routing.byok_provider_backends import (
        byok_backend_max_retries,
    )
    from omnimarket.inference.provider_response_error import provider_error_from_body
    from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
        ModelDelegationRequest as OrchestratorRequest,
    )
    from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
        ModelInferenceResponseData,
    )
    from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
        ModelRoutingIntent,
    )
    from omnimarket.nodes.node_projection_delegation.handlers.handler_budget_state import (
        TABLE,
        ModelDelegationBudgetStateEvent,
        materialize_budget_state,
    )
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    budget = byok_backend_max_retries(_GLM_BACKEND_REF)
    assert budget is not None
    assert budget >= 1
    error = provider_error_from_body(
        {
            "id": "gen-omn25411",
            "error": {
                "message": "Upstream error: Service temporarily overloaded",
                "code": 502,
                "metadata": {"error_type": "provider_unavailable"},
            },
        }
    )
    assert error is not None
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(
        OrchestratorRequest(
            prompt="Reply with the single word OK.",
            task_type="summarization",  # type: ignore[arg-type]
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
            tenant_id=_TENANT_ID,
        )
    )

    def attempt() -> list[Any]:
        handler.handle_routing_decision(_glm_customer_route(cid))
        return list(
            handler.handle_inference_response(
                ModelInferenceResponseData(
                    correlation_id=cid,
                    content="",
                    model_used=_R8_MODEL_USED,
                    latency_ms=900,
                    error_message=error.as_error_message(),
                    prompt_tokens=_PROMPT_TOKENS,
                    completion_tokens=_COMPLETION_TOKENS,
                    total_tokens=_PROMPT_TOKENS + _COMPLETION_TOKENS,
                )
            )
        )

    for _ in range(budget):
        assert any(isinstance(e, ModelRoutingIntent) for e in attempt())
    events = attempt()
    terminal = next(e for e in events if isinstance(e, ModelDelegationResult))
    assert isinstance(terminal, ModelDelegationFailed)

    history = handler.workflows[cid].escalation_history
    assert len(history) == budget + 1
    for priced in history:
        assert priced.tier_name == TENANT_OVERLAY_TIER_NAME
        assert priced.cost_usd == pytest.approx(_declared_cost())
    # Cumulative: every banked retry plus the final attempt the terminal re-prices.
    assert terminal.cumulative_attempt_cost == pytest.approx(
        _declared_cost() * (budget + 1)
    )

    # Budget row: the own-key cumulative never draws the platform ceiling, even
    # over a registry that (wrongly) caps the tenant_overlay tier.
    from types import SimpleNamespace

    from omnibase_core.models.delegation.wire import EnumTierCostType, ModelTierCost

    capped = ModelTierCost(
        cost_type=EnumTierCostType.BUDGETED,
        rate_per_1k_usd=0.01,
        monthly_cap_usd=100.0,
        overage_rate_per_1k_usd=0.02,
    )
    registry = SimpleNamespace(
        tiers=[SimpleNamespace(name=TENANT_OVERLAY_TIER_NAME, cost=capped)]
    )
    db = InmemoryDatabaseAdapter()
    with patch("omnimarket.pricing._load_routing_config", return_value=registry):
        result = materialize_budget_state(
            ModelDelegationBudgetStateEvent(
                correlation_id=str(cid),
                cost_tier_name=TENANT_OVERLAY_TIER_NAME,
                cost_measurement_source="metered",
                budget_headroom_consumed_usd=Decimal("0"),
                cost_usd=Decimal(str(terminal.cumulative_attempt_cost)),
                tenant_id=_TENANT_ID,
                timestamp="2026-11-17T16:00:00+00:00",
            ),
            db,
        )
    assert result.rows_upserted == 0
    assert result.skipped_reason == "tier_not_budgeted"
    assert db.query(TABLE, {"tenant_id": _TENANT_ID}) == []


@pytest.mark.unit
def test_omn24874_a_tenant_overlay_run_has_no_platform_successor_tier() -> None:
    # Review finding 3 (OMN-25411), the mixed case: a run whose tenant_overlay
    # attempt then ends on a platform tier would book own-key cost against that
    # tier's ceiling. The ladder walk never leaves tenant_overlay for any task
    # class the routing contract declares, so the case is unreachable.
    from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
        _get_task_class_contract,
        next_eligible_tier,
    )

    contract = _get_task_class_contract()
    assert contract is not None
    task_classes = contract.get("task_classes")
    assert isinstance(task_classes, dict)
    task_types = sorted(str(name) for name in task_classes)
    assert task_types, "the routing contract declares no task class to walk"
    assert TENANT_OVERLAY_TIER_NAME not in {
        tier.name for tier in _load_routing_config().tiers
    }
    for task_type in task_types:
        assert (
            next_eligible_tier(
                TENANT_OVERLAY_TIER_NAME, frozenset(), task_type=task_type
            )
            is None
        ), task_type
