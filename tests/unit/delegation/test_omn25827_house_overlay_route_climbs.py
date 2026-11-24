# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25827: a house overlay route that fails is climbed past, never re-issued.

The dev delegation-regression-nightly failed the live golden cases I1, I3, I5 and I7
with ``terminal=failed`` on ``claude-sonnet-5-5`` and 0/0 tokens. A probe delegation
on the dev lane read the cause: the house tenant's overlay row names the
``byok-anthropic`` backend, the key answered HTTP 401, and the run made three
attempts on that one backend (``tier=tenant_overlay``) before ending with
``every_rung_failed``. The platform ladder (local, then OpenRouter, then Claude API)
never ran.

Three seams made the house route a pinned model:

* ``next_customer_retry`` treats every ``tenant_overlay`` decision whose backend the
  BYOK catalogue declares as a customer's own-key route and re-issues the same
  responder (OMN-18265). The house tenant has no customer key to protect.
* ``next_eligible_tier`` returns ``None`` for ``tenant_overlay`` (not a ladder tier), so
  the escalation decision had nowhere to go.
* ``delta`` rebuilds the overlay decision on every re-route, whatever
  ``min_tier_name`` or ``excluded_backend_refs`` say.

The recorded case output is ``fixtures/omn25827_house_claude_401/recorded_case_output.json``.
A customer's own-key route keeps its rule: it talks only to the provider the customer chose.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.models.model_tenant_routing_overlay_backend import (
    ModelTenantRoutingOverlayBackend,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
    _inference_error_failure_class,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    TENANT_OVERLAY_TIER_NAME,
    delta,
    first_eligible_tier,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.tenant_overlay_decision import (
    build_tenant_overlay_decision,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_dod_resolution import (
    ModelDodResolution,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "omn25827_house_claude_401"
    / "recorded_case_output.json"
)
ANTHROPIC_URL = "https://api.anthropic.com/v1/chat/completions"
BACKEND_REF = "byok-anthropic"
MODEL = "claude-sonnet-5-5"
TASK = "test"
# A reference, not a secret: the minter's own module states the ref carries no
# secret material and is safe to log and publish.
CUSTOMER_REF = (
    "cred_a0ea038d-8538-494c-b004-4ceaf9677fac_anthropic_"
    "911110ccc89c4b6db98203024ba23371"
)
CUSTOMER_TENANT = "a0ea038d-8538-494c-b004-4ceaf9677fac"


def _recorded() -> dict[str, Any]:
    recorded: dict[str, Any] = json.loads(FIXTURE.read_text())
    return recorded


def _recorded_401() -> str:
    attempts = _recorded()["probe_attempts"]
    assert {a["backend_id"] for a in attempts} == {BACKEND_REF}
    return str(attempts[0]["error_message"])


def _request(cid: UUID, tenant_id: str) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write a test for a function that adds two numbers.",
        task_type=TASK,
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
        tenant_id=tenant_id,
    )


def _overlay(
    tenant_id: str, secret_ref: str | None
) -> ModelTenantRoutingOverlayBackend:
    return ModelTenantRoutingOverlayBackend(
        tenant_id=tenant_id,
        task_type=TASK,
        backend_id=BACKEND_REF,
        provider="anthropic",
        endpoint_url=ANTHROPIC_URL,
        model_name=MODEL,
        models=(MODEL,),
        secret_ref=secret_ref,
    )


def _route(cid: UUID, tenant_id: str, secret_ref: str | None) -> ModelRoutingDecision:
    """The decision the reducer builds from the stored row, not a hand-made one."""
    return build_tenant_overlay_decision(
        _request(cid, tenant_id),
        task_type=TASK,
        overlay=_overlay(tenant_id, secret_ref),
        estimated_tokens=10,
        dod_resolution=ModelDodResolution(),
        system_prompt="s",
        backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{BACKEND_REF}"),
    )


def _failed_call(cid: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="",
        model_used=MODEL,
        latency_ms=210,
        error_message=_recorded_401(),
    )


def _drive_one_failed_attempt(
    tenant_id: str, secret_ref: str | None
) -> tuple[HandlerDelegationWorkflow, UUID, list[Any]]:
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_request(cid, tenant_id))
    handler.handle_routing_decision(_route(cid, tenant_id, secret_ref))
    return handler, cid, list(handler.handle_inference_response(_failed_call(cid)))


def test_the_recorded_cases_all_ended_on_the_one_anthropic_rung() -> None:
    """The record: four cases failed on one model, and the probe never left its backend."""
    recorded = _recorded()
    assert set(recorded["cases"]) == {"I1", "I3", "I5", "I7"}
    for case in recorded["cases"].values():
        assert case["terminal"] == "failed"
        assert case["model"] == MODEL
        assert any(
            f.startswith("tier_behavior: expected free_local_first")
            for f in case["failures"]
        )
    assert [a["tier"] for a in recorded["probe_attempts"]] == [
        TENANT_OVERLAY_TIER_NAME
    ] * 3
    assert recorded["probe_terminal"]["terminal_failure_reason"] == "every_rung_failed"


def test_the_recorded_error_is_a_real_401_not_a_local_credential_refusal() -> None:
    """Positive control: the answered 401 stays ``provider_auth_failed``.

    It is not the local ``CredentialUnresolvedError`` the sibling classification fix
    moves to ``provider_credential_missing``: the provider answered, so the key is
    wrong at the provider and the chain must move on rather than re-ask it.
    """
    assert (
        _inference_error_failure_class(_recorded_401())
        is EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
    )


def test_a_failed_house_overlay_route_climbs_to_the_platform_ladder(
    frontier_unconfigured_bifrost: None,
) -> None:
    """RED at the base: the house route was re-issued to the same backend, no climb."""
    handler, cid, events = _drive_one_failed_attempt(str(HOUSE_TENANT_UUID), None)

    assert not [e for e in events if isinstance(e, ModelInferenceIntent)], (
        "the house route was re-issued to the same backend that answered 401"
    )
    routing = [e for e in events if isinstance(e, ModelRoutingIntent)]
    assert len(routing) == 1
    assert not any(isinstance(e, ModelDelegationResult) for e in events), (
        "the house route's 401 ended the delegation instead of trying the ladder"
    )
    expected_tier = first_eligible_tier(
        TASK, correlation_id=cid, prompt=_request(cid, "x").prompt
    )
    assert expected_tier is not None
    assert routing[0].min_tier_name == expected_tier, (
        "the re-route asked for the same house route again, not the ladder's first tier"
    )
    escalations = [
        e for e in events if isinstance(e, ModelLlmDelegationEscalationTriggeredEvent)
    ]
    assert len(escalations) == 1
    assert (
        escalations[0].failure_class is EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
    )
    assert handler.workflows[cid].escalation_count == 1
    assert handler.workflows[cid].customer_route_retry_count == 0


def test_a_failed_customer_route_is_still_reissued_to_the_customers_own_key(
    frontier_unconfigured_bifrost: None,
) -> None:
    """Control: a customer talks only to the provider the customer chose (no climb)."""
    handler, cid, events = _drive_one_failed_attempt(CUSTOMER_TENANT, CUSTOMER_REF)

    assert not [e for e in events if isinstance(e, ModelRoutingIntent)]
    reissued = [e for e in events if isinstance(e, ModelInferenceIntent)]
    assert len(reissued) == 1
    assert reissued[0].base_url == ANTHROPIC_URL
    assert handler.workflows[cid].escalation_count == 0
    assert handler.workflows[cid].customer_route_retry_count == 1


def test_the_house_overlay_row_is_not_returned_again_once_its_backend_failed(
    frontier_unconfigured_bifrost: None,
) -> None:
    """RED at the base: ``delta`` rebuilt the overlay decision on every re-route."""
    cid = uuid4()
    tier = first_eligible_tier(
        TASK, correlation_id=cid, prompt=_request(cid, "x").prompt
    )
    assert tier is not None
    decision = delta(
        _request(cid, str(HOUSE_TENANT_UUID)),
        min_tier_name=tier,
        excluded_backend_refs=frozenset({BACKEND_REF}),
        tenant_overlay=_overlay(str(HOUSE_TENANT_UUID), None),
    )
    assert decision.tier_name != TENANT_OVERLAY_TIER_NAME
    assert decision.selected_backend_ref != BACKEND_REF


def test_the_house_overlay_row_is_the_first_rung_when_nothing_failed(
    frontier_unconfigured_bifrost: None,
) -> None:
    """Control: the first ask still takes the house row, so the lab's rows keep working."""
    cid = uuid4()
    decision = delta(
        _request(cid, str(HOUSE_TENANT_UUID)),
        tenant_overlay=_overlay(str(HOUSE_TENANT_UUID), None),
    )
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.selected_backend_ref == BACKEND_REF


def test_a_customer_overlay_row_is_returned_again_whatever_failed(
    frontier_unconfigured_bifrost: None,
) -> None:
    """Control: a customer is never placed on the house ladder (INV-068)."""
    cid = uuid4()
    decision = delta(
        _request(cid, CUSTOMER_TENANT),
        min_tier_name="local",
        excluded_backend_refs=frozenset({BACKEND_REF}),
        tenant_overlay=_overlay(CUSTOMER_TENANT, CUSTOMER_REF),
    )
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.selected_backend_ref == BACKEND_REF
