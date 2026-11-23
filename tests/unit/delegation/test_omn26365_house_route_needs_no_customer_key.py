# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26365: a house-tier route demands no customer key; a customer route still does.

The dev runtime delegates under the house tenant, and a house routing row is
read through the tenant-overlay tier (OMN-19186). ``expected_credential_source_for``
used to classify every ``tenant_overlay`` decision ``CUSTOMER_KEY`` before it
looked at whose row it was, so a house row that declares no credential reference
(a local model, which needs no key) was refused at the effect boundary with
``ONEX_MARKET_EFFECT_CREDENTIAL_UNRESOLVED``.

The reducer already stamps the row's owner on the decision: ``cost_tier`` is
``house_overlay`` for the house tenant and ``tenant_byok`` for any customer. These
tests pin both halves: a house overlay route that declares no reference is
auth-free, and the OMN-18201 fail-closed refusal is untouched for a customer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import EnumCredentialSource

from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.models.model_tenant_routing_overlay_backend import (
    ModelTenantRoutingOverlayBackend,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    expected_credential_source_for,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    TENANT_OVERLAY_TIER_NAME,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.tenant_overlay_decision import (
    build_tenant_overlay_decision,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_dod_resolution import (
    ModelDodResolution,
)
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
)
from omnimarket.routing.tenant_overlay_resolver import HOUSE_OVERLAY_COST_TIER
from tests.unit.delegation.test_omn18201_effect_boundary_auth_header import (
    HOUSE_REF,
    LIVE_TENANT_REF,
)

CUSTOMER_TENANT = "d562bf4a-a3f1-4016-8e33-7d4214f62fce"
LOCAL_URL = "http://local.test:8000/v1/chat/completions"


def _decision(**overrides: Any) -> ModelRoutingDecision:
    base: dict[str, Any] = {
        "correlation_id": uuid4(),
        "task_type": "summarization",
        "selected_model": "qwen3.8-27b",
        "selected_backend_id": uuid4(),
        "selected_backend_ref": "house-local",
        "endpoint_url": LOCAL_URL,
        "api_key_ref": None,
        "extra_headers": None,
        "cost_tier": HOUSE_OVERLAY_COST_TIER,
        "tier_name": TENANT_OVERLAY_TIER_NAME,
        "max_context_tokens": 128000,
        "timeout_ms": 120000,
        "max_tokens": 4096,
        "system_prompt": "s",
        "rationale": "r",
        "dod_deterministic": (),
        "dod_heuristic": (),
        "requested_shape": "unconstrained",
        "dod_deterministic_source": "class_definition_of_done",
        "dod_heuristic_source": "class_definition_of_done",
    }
    base.update(overrides)
    return ModelRoutingDecision.model_validate(base)


def _built_from_overlay_row(
    tenant_id: str, secret_ref: str | None
) -> ModelRoutingDecision:
    """The decision the reducer builds from a stored overlay row, not a hand-made one."""
    from omnibase_core.models.delegation.wire import ModelDelegationRequest

    row = ModelTenantRoutingOverlayBackend(
        tenant_id=tenant_id,
        task_type="summarization",
        backend_id="route-under-test",
        endpoint_url=LOCAL_URL,
        model_name="qwen3.8-27b",
        provider="local",
        secret_ref=secret_ref,
    )
    request = ModelDelegationRequest(
        correlation_id=uuid4(),
        prompt="p",
        task_type="summarization",
        tenant_id=tenant_id,
        emitted_at=datetime.now(UTC),
    )
    return build_tenant_overlay_decision(
        request,
        task_type="summarization",
        overlay=row,
        estimated_tokens=10,
        dod_resolution=ModelDodResolution(),
        system_prompt="s",
        backend_id=uuid4(),
    )


# --------------------------------------------------------------------------
# House and local routes: no customer key
# --------------------------------------------------------------------------


def test_a_house_overlay_route_with_no_reference_needs_no_key() -> None:
    """The failing shape: house tenant, tenant_overlay tier, no credential reference."""
    assert expected_credential_source_for(_decision()) is EnumCredentialSource.NONE, (
        "a house route that names no credential is an auth-free backend"
    )


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_house_overlay_route_with_a_blank_reference_needs_no_key(
    blank: str | None,
) -> None:
    assert (
        expected_credential_source_for(_decision(api_key_ref=blank))
        is EnumCredentialSource.NONE
    )


def test_a_house_overlay_route_with_a_house_reference_expects_a_house_key() -> None:
    assert (
        expected_credential_source_for(_decision(api_key_ref=HOUSE_REF))
        is EnumCredentialSource.HOUSE
    )


@pytest.mark.parametrize("house_tenant", [HOUSE_TENANT_SLUG, str(HOUSE_TENANT_UUID)])
def test_the_reducer_built_house_decision_needs_no_key(house_tenant: str) -> None:
    """Both representations of the house identity, through the producer of the decision."""
    decision = _built_from_overlay_row(house_tenant, secret_ref=None)
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.cost_tier == HOUSE_OVERLAY_COST_TIER
    assert expected_credential_source_for(decision) is EnumCredentialSource.NONE


# --------------------------------------------------------------------------
# Customer routes: OMN-18201 fail-closed behaviour is kept
# --------------------------------------------------------------------------


def test_a_customer_overlay_route_with_a_minted_reference_expects_the_customer_key() -> (
    None
):
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=LIVE_TENANT_REF, cost_tier="tenant_byok")
        )
        is EnumCredentialSource.CUSTOMER_KEY
    )


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_customer_overlay_route_with_no_reference_still_expects_one(
    blank: str | None,
) -> None:
    """The load-bearing refusal: a customer's missing reference is not auth-free."""
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=blank, cost_tier="tenant_byok")
        )
        is EnumCredentialSource.CUSTOMER_KEY
    )


def test_the_reducer_built_customer_decision_still_expects_the_customer_key() -> None:
    decision = _built_from_overlay_row(CUSTOMER_TENANT, secret_ref=None)
    assert decision.cost_tier == "tenant_byok"
    assert expected_credential_source_for(decision) is EnumCredentialSource.CUSTOMER_KEY


def test_a_minted_reference_is_a_customer_key_even_on_a_house_labelled_decision() -> (
    None
):
    """The reference shape wins over the label: a cred_ reference is never house or auth-free."""
    assert (
        expected_credential_source_for(_decision(api_key_ref=LIVE_TENANT_REF))
        is EnumCredentialSource.CUSTOMER_KEY
    )
