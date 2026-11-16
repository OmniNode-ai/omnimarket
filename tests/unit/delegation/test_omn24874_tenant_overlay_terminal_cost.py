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

from decimal import Decimal

import pytest

from omnimarket.handlers.routing.byok_provider_backends import (
    byok_declared_price_per_1m,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.tenant_overlay_decision import (
    TENANT_OVERLAY_TIER_NAME,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    ModelProjectionTaskDelegatedEvent,
    _measure_actual_cost,
)
from omnimarket.pricing import build_premium_counterfactual, resolve_tier_cost

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
