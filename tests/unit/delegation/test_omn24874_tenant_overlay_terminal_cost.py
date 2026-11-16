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

from datetime import UTC, datetime
from decimal import Decimal
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
)
from omnimarket.pricing import build_premium_counterfactual, resolve_tier_cost
from omnimarket.routing.tenant_overlay_resolver import TENANT_OVERLAY_TIER_NAME

_GENERAL_ENDPOINT = "https://api.z.ai/api/paas/v4/chat/completions"
_MODEL = "glm-5.3-flash"
_PROMPT_TOKENS = 300
_COMPLETION_TOKENS = 200


def _declared_cost() -> float:
    price = byok_declared_price_per_1m(_GENERAL_ENDPOINT, _MODEL)
    assert price is not None, "the catalogue must declare the model's price"
    price_in, price_out = price
    return float(
        (Decimal(_PROMPT_TOKENS) * price_in + Decimal(_COMPLETION_TOKENS) * price_out)
        / Decimal(1_000_000)
    )


@pytest.mark.unit
def test_the_tenant_overlay_tier_has_no_registry_cost_model() -> None:
    # The premise of the defect: the tier name the overlay decision carries is not a registry tier.
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


@pytest.mark.unit
def test_projection_still_floors_an_unpriced_unmodelled_tier_to_zero() -> None:
    counterfactual = build_premium_counterfactual(
        prompt_tokens=_PROMPT_TOKENS, completion_tokens=_COMPLETION_TOKENS
    )
    event = ModelProjectionTaskDelegatedEvent(
        correlation_id="omn-24874-unpriced",
        task_type="summarization",
        delegated_to="glm-undeclared-model",
        model_name="glm-undeclared-model",
        quality_gate_passed=True,
        cost_usd=0.0,
        tokens_input=_PROMPT_TOKENS,
        tokens_output=_COMPLETION_TOKENS,
        cost_tier_name=TENANT_OVERLAY_TIER_NAME,
        premium_counterfactual=counterfactual,
    )
    measured = _measure_actual_cost(event)
    assert measured.cost_usd == 0.0
    assert measured.cost_measurement_source == "no_cost_model"


_BYOK_BACKEND_ID = "byok-glm-general"
_UNMODELLED_TIER = "a_tier_the_registry_never_declared"


def _overlay_decision() -> object:
    """The decision a tenant overlay row yields: the catalogue's default GLM backend, as the probe tenant's route is minted."""
    backend = resolve_byok_provider_backend("glm", plan=None)
    assert backend is not None
    overlay = ModelTenantRoutingOverlayBackend(
        tenant_id="readiness-probe",
        task_type="summarization",
        backend_id=backend.backend_id,
        endpoint_url=backend.endpoint_url,
        model_name=_MODEL,
        models=(_MODEL,),
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
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME  # type: ignore[attr-defined]
    assert byok_declared_price_per_1m(decision.endpoint_url, _MODEL) is not None  # type: ignore[attr-defined]


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
            tier_name=decision.tier_name,  # type: ignore[attr-defined]
            endpoint_url=decision.endpoint_url,  # type: ignore[attr-defined]
            model=decision.selected_model,  # type: ignore[attr-defined]
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
    # Own-key cost never draws down the platform's ceiling headroom (review finding 10).
    assert measured.headroom_consumed_usd == 0.0


@pytest.mark.unit
def test_a_zero_cost_tenant_overlay_terminal_is_priced_from_the_declared_price_cost_usd_zero() -> (
    None
):
    # omnimarket#6331's case: a terminal from an older runtime or another producer arrives with cost_usd 0 on a BYOK backend.
    measured = _measure_actual_cost(
        _projection_event(
            tier=TENANT_OVERLAY_TIER_NAME,
            cost=0.0,
            source="no_cost_model",
            backend_id=_BYOK_BACKEND_ID,
        )
    )
    assert measured.cost_usd == pytest.approx(_declared_cost())
    assert measured.cost_measurement_source == "metered"


@pytest.mark.unit
def test_a_zero_cost_terminal_on_a_house_backend_or_undeclared_model_stays_unpriced() -> (
    None
):
    house = _measure_actual_cost(
        _projection_event(
            tier=TENANT_OVERLAY_TIER_NAME,
            cost=0.0,
            source="no_cost_model",
            backend_id="a-house-rung",
        )
    )
    undeclared = _measure_actual_cost(
        _projection_event(
            tier=TENANT_OVERLAY_TIER_NAME,
            cost=0.0,
            source="no_cost_model",
            model="glm-undeclared-model",
            backend_id=_BYOK_BACKEND_ID,
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
            tier_name=decision.tier_name,  # type: ignore[attr-defined]
            endpoint_url=decision.endpoint_url,  # type: ignore[attr-defined]
            model=decision.selected_model,  # type: ignore[attr-defined]
        )
    )
    canonical = next(e for e in events if isinstance(e, ModelDelegationResult))
    converted = _canonical_result_to_task_delegated_payload(
        canonical.model_dump(mode="json")
    )
    measured = _measure_actual_cost(ModelProjectionTaskDelegatedEvent(**converted))
    assert measured.cost_usd == pytest.approx(_declared_cost())
    assert measured.cost_measurement_source == "metered"
