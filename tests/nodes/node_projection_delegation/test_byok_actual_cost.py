# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24859: a call on a customer's own key is priced by the key's model in the delegation row.

The tenant Runs list reads ``delegation_events.cost_usd``. A BYOK terminal carries the
``tenant_overlay`` serving tier, which has no typed cost model in the routing registry, so the
projection booked ``cost_usd=0.0`` with ``cost_measurement_source=no_cost_model`` for a model whose
price the BYOK catalogue declares. The R8 delegation scenario then failed its
``organic_run_has_values`` assertion on every tick. The projection now prices such a call from the
catalogue's declared price for the backend and model; a model with no declared price stays
unpriced, and a call with no BYOK backend is untouched.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
    validate_actual_cost_provenance,
)
from omnimarket.pricing import build_premium_counterfactual
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    seed_tenant_registry,
)

HANDLER = HandlerProjectionDelegation()

# glm-5.3-flash on the general API plan is declared at 0.15 in / 0.50 out USD per 1M tokens.
PROMPT_TOKENS = 231
COMPLETION_TOKENS = 277
DECLARED_COST = (
    Decimal(PROMPT_TOKENS) * Decimal("0.15")
    + Decimal(COMPLETION_TOKENS) * Decimal("0.50")
) / Decimal("1000000")


def _cost(row: dict[str, object]) -> Decimal:
    """The row's cost; an unpriced call is stored as NULL, which reads here as zero."""
    value = row["cost_usd"]
    return Decimal("0") if value is None else Decimal(str(value))


def _project(
    *,
    correlation_id: str,
    model_name: str,
    backend_id: str | None,
) -> dict[str, object]:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    counterfactual = build_premium_counterfactual(
        prompt_tokens=PROMPT_TOKENS,
        completion_tokens=COMPLETION_TOKENS,
        premium_model="claude-opus-4-6",
    )
    assert counterfactual is not None
    event = ModelTaskDelegatedEvent(
        tenant_id=PROJECTION_TENANT_SLUG,
        correlation_id=correlation_id,
        task_type="summarization",
        delegated_to=model_name,
        model_name=model_name,
        backend_id=backend_id,
        quality_gate_passed=True,
        cost_usd=0.0,
        cost_savings_usd=0.0,
        tokens_input=PROMPT_TOKENS,
        tokens_output=COMPLETION_TOKENS,
        cost_tier_name="tenant_overlay",
        premium_counterfactual=counterfactual,
    )
    assert HANDLER.project(event, db).rows_upserted == 1
    row = db.query("delegation_events", {"correlation_id": correlation_id})[0]
    row["_counterfactual_cost_usd"] = counterfactual.counterfactual_cost_usd
    return row


@pytest.mark.unit
class TestByokDeclaredPriceReachesDelegationRow:
    def test_declared_price_is_booked_as_cost(self) -> None:
        row = _project(
            correlation_id="corr-byok-priced",
            model_name="glm-5.3-flash",
            backend_id="byok-glm-general",
        )
        assert _cost(row) == pytest.approx(DECLARED_COST)
        assert _cost(row) > 0
        assert row["cost_measurement_source"] == "metered"
        assert row["cost_tier_name"] == "tenant_overlay"
        counterfactual_cost = row["_counterfactual_cost_usd"]
        assert isinstance(counterfactual_cost, Decimal)
        assert Decimal(str(row["cost_savings_usd"])) == pytest.approx(
            counterfactual_cost - DECLARED_COST
        )
        validate_actual_cost_provenance(row)

    def test_model_with_no_declared_price_stays_unpriced(self) -> None:
        row = _project(
            correlation_id="corr-byok-unpriced",
            model_name="glm-model-nobody-declared",
            backend_id="byok-glm-general",
        )
        assert _cost(row) == Decimal("0")
        assert row["cost_measurement_source"] == "no_cost_model"
        # Contrast: the same call on a declared model is priced, so this
        # test fails where the pricing is absent (merge base).
        priced = _project(
            correlation_id="corr-byok-unpriced-contrast",
            model_name="glm-5.3-flash",
            backend_id="byok-glm-general",
        )
        assert _cost(priced) > 0

    def test_call_with_no_byok_backend_is_untouched(self) -> None:
        row = _project(
            correlation_id="corr-no-backend",
            model_name="glm-5.3-flash",
            backend_id=None,
        )
        assert _cost(row) == Decimal("0")
        assert row["cost_measurement_source"] == "no_cost_model"
        priced = _project(
            correlation_id="corr-no-backend-contrast",
            model_name="glm-5.3-flash",
            backend_id="byok-glm-general",
        )
        assert _cost(priced) > 0
