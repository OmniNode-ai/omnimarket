# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18265: a provider error wearing an HTTP 200 is not an empty completion.

The occasion, measured rather than reasoned about. The first real-key C9
customer-pass walk to reach ``cli_delegation`` (omniweb run ``34713319588``,
2026-09-12T19:11Z) terminalised ``failed`` and the CLI told the customer "the
runtime returned no content". On onex-dev the route was the customer's own BYOK
binding -- ``tier=tenant_overlay``, ``nvidia/nemotron-3-ultra-550b-a55b:free``
on ``https://openrouter.ai/api/v1/chat/completions`` -- and
``public.delegation_events`` recorded ``escalation_count = 0`` with
``quality_gate_detail = 'API returned empty choices array'`` and zero tokens.

What the vendor actually sent, reproduced against the SAME credential the walk
registered, same model, same endpoint: **HTTP 200**, 23.9 s, body

    {"id": "gen-...", "error": {"message": "Upstream error from Nvidia:
     Service temporarily overloaded", "code": 502,
     "metadata": {"error_type": "provider_unavailable"}}}

and an immediate re-probe of the same slug returned content. So the route is
intermittently overloaded, not dead, and the platform had a transient
availability fact in its hands and threw it away.

Two links, and both are bound here:

* the effect boundary read only ``choices`` and raised the flat "API returned
  empty choices array", discarding the ``error`` object that named a 502
  ``provider_unavailable``;
* the orchestrator lists that literal in its non-retryable markers (the
  minimal-safe classification OMN-13140 chose for a genuinely BLANK completion),
  so the escalate-or-terminate decision could only terminate.

The one-responder chain is NOT the defect and is not changed here. OMN-17082
forbids a house credential on a customer path, so a customer's only lawful
route is their own; the cheapest available next responder for a transiently
overloaded free route is that same route, retried within a contract-declared
budget.

Every negative assertion below carries a positive control, because "it did not
retry" and "the branch was never reached" look identical otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import httpx
import pytest
from omnibase_core.models.delegation.wire import (
    ModelDelegationFailed,
    ModelInferenceIntent,
)

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference.provider_response_error import (
    ModelProviderResponseError,
    provider_error_from_body,
)
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
    _inference_error_failure_class,
    _should_escalate_inference_error,
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
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)
from omnimarket.routing.byok_provider_backends import (
    byok_backend_max_retries,
    load_byok_provider_catalog,
)

pytestmark = pytest.mark.unit

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
FREE_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
BYOK_BACKEND_REF = "byok-openrouter"
# A reference, not a secret: the minter's own module states the ref carries no
# secret material and is safe to log and publish.
LIVE_TENANT_REF = (
    "cred_a0ea038d-8538-494c-b004-4ceaf9677fac_openrouter_"
    "911110ccc89c4b6db98203024ba23371"
)

#: The body OpenRouter returned for correlation c1838c39, verbatim in shape.
LIVE_PROVIDER_ERROR_BODY: dict[str, Any] = {
    "id": "gen-1789241778-mJtEMhA4W2CvqbH0cubu",
    "error": {
        "message": "Upstream error from Nvidia: Service temporarily overloaded",
        "code": 502,
        "metadata": {"error_type": "provider_unavailable"},
    },
}


# --------------------------------------------------------------------------
# The effect boundary: read the vendor's error instead of inventing one
# --------------------------------------------------------------------------


class _BodyTransport:
    """Stands in for ``httpx.Client`` and returns one prepared 200 body."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.requests: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> _BodyTransport:
        return self

    def __enter__(self) -> _BodyTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url), json=self._body)


def _bind_transport(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> _BodyTransport:
    transport = _BodyTransport(body)
    monkeypatch.setattr(
        "omnimarket.nodes.node_llm_delegation_call_effect.handlers"
        ".handler_inference_intent.httpx.Client",
        transport,
    )
    monkeypatch.setattr(
        "omnimarket.nodes.node_llm_delegation_call_effect.handlers"
        ".handler_inference_intent._resolve_api_key",
        lambda _ref: "resolved-value",
    )
    return transport


def _intent(**overrides: Any) -> ModelInferenceIntent:
    base: dict[str, Any] = {
        "base_url": OPENROUTER_URL,
        "model": FREE_MODEL,
        "system_prompt": "s",
        "prompt": "p",
        "max_tokens": 64,
        "correlation_id": uuid4(),
        "tenant_id": "a0ea038d-8538-494c-b004-4ceaf9677fac",
        "api_key_ref": LIVE_TENANT_REF,
    }
    base.update(overrides)
    return ModelInferenceIntent.model_validate(base)


def test_the_boundary_reports_the_vendor_reason_not_an_empty_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1. The 19:11:59Z shape: a 200 whose body is an availability failure."""
    transport = _bind_transport(monkeypatch, LIVE_PROVIDER_ERROR_BODY)

    response = HandlerInferenceIntent().handle(_intent())

    assert transport.requests == [OPENROUTER_URL], (
        "the provider seam was never reached, so the message below would say "
        "nothing about how a 200-with-error is read"
    )
    assert "Service temporarily overloaded" in response.error_message
    assert "provider_unavailable" in response.error_message
    assert "502" in response.error_message
    assert "empty choices array" not in response.error_message, (
        "the vendor said why; reporting 'empty choices' asserts a different "
        "fact and is what made this terminal unrecoverable"
    )


def test_an_empty_choices_body_with_no_error_object_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3. The minimal-safe classification is narrowed, never removed.

    A provider that genuinely answered with nothing still reads as nothing.
    """
    transport = _bind_transport(monkeypatch, {"id": "resp-1", "choices": []})

    response = HandlerInferenceIntent().handle(_intent())

    assert transport.requests == [OPENROUTER_URL]
    assert "API returned empty choices array" in response.error_message


def test_positive_control_a_normal_body_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for both assertions above: the seam works when the body does."""
    _bind_transport(
        monkeypatch,
        {
            "id": "resp-1",
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )

    response = HandlerInferenceIntent().handle(_intent())

    assert response.error_message == ""
    assert response.content == "OK"


# --------------------------------------------------------------------------
# The parsed error, and how both delegation paths classify it
# --------------------------------------------------------------------------


def test_the_error_object_parses_into_the_three_facts_it_carries() -> None:
    parsed = provider_error_from_body(LIVE_PROVIDER_ERROR_BODY)

    assert isinstance(parsed, ModelProviderResponseError)
    assert (
        parsed.message == "Upstream error from Nvidia: Service temporarily overloaded"
    )
    assert parsed.code == 502
    assert parsed.error_type == "provider_unavailable"


@pytest.mark.parametrize(
    "body",
    [
        {"id": "r", "choices": []},
        {"id": "r", "error": None},
        {"id": "r", "error": {}},
        {"id": "r", "choices": [{"message": {"content": "OK"}}]},
    ],
)
def test_a_body_that_declares_no_error_parses_to_none(body: dict[str, Any]) -> None:
    """Fail-closed the other way: nothing is invented from an absent field."""
    assert provider_error_from_body(body) is None


def test_the_composed_message_is_retryable_and_classifies_as_unavailable() -> None:
    """AC2. The bus orchestrator classifies on text, so the text must carry it."""
    message = provider_error_from_body(LIVE_PROVIDER_ERROR_BODY)
    assert message is not None
    text = message.as_error_message()

    assert _should_escalate_inference_error(text) is True
    assert _inference_error_failure_class(text) is (
        EnumDelegationFailureClass.MODEL_UNAVAILABLE
    )


def test_the_empty_choices_message_remains_non_retryable() -> None:
    """The control for the assertion above: the marker set still bites."""
    assert _should_escalate_inference_error("API returned empty choices array") is False


@pytest.mark.parametrize(
    ("code", "error_type", "expected"),
    [
        (502, "provider_unavailable", EnumDelegationFailureClass.MODEL_UNAVAILABLE),
        (429, "rate_limited", EnumDelegationFailureClass.RATE_LIMITED),
        (401, "auth", EnumDelegationFailureClass.PROVIDER_AUTH_FAILED),
        (None, None, EnumDelegationFailureClass.MODEL_UNAVAILABLE),
    ],
)
def test_the_typed_failure_class_matches_what_the_vendor_said(
    code: int | None,
    error_type: str | None,
    expected: EnumDelegationFailureClass,
) -> None:
    """AC7. The bus-less local port classifies on the typed class, not on text.

    Both paths must reach the same verdict or a customer's delegation behaves
    differently depending on which entry point ran it.
    """
    metadata = {"error_type": error_type} if error_type is not None else {}
    body: dict[str, Any] = {"error": {"message": "upstream boom", "metadata": metadata}}
    if code is not None:
        body["error"]["code"] = code

    parsed = provider_error_from_body(body)

    assert parsed is not None
    assert parsed.failure_class is expected


# --------------------------------------------------------------------------
# The customer's own route is retried before the workflow gives up
# --------------------------------------------------------------------------


def _request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Reply with the single word OK.",
        task_type="summarization",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
        tenant_id="a0ea038d-8538-494c-b004-4ceaf9677fac",
    )


def _customer_route(
    correlation_id: UUID,
    *,
    backend_ref: str = BYOK_BACKEND_REF,
) -> ModelRoutingDecision:
    """The decision ``_decision_from_tenant_overlay`` builds for a BYOK tenant."""
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="summarization",
        selected_model=FREE_MODEL,
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{backend_ref}"),
        selected_backend_ref=backend_ref,
        endpoint_url=OPENROUTER_URL,
        api_key_ref=LIVE_TENANT_REF,
        cost_tier="tenant_byok",
        tier_name=TENANT_OVERLAY_TIER_NAME,
        max_context_tokens=128000,
        timeout_ms=300000,
        max_tokens=65536,
        system_prompt="s",
        rationale="tenant overlay",
    )


def _provider_unavailable(correlation_id: UUID) -> ModelInferenceResponseData:
    parsed = provider_error_from_body(LIVE_PROVIDER_ERROR_BODY)
    assert parsed is not None
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="",
        model_used=FREE_MODEL,
        latency_ms=23032,
        error_message=parsed.as_error_message(),
    )


def _drive_one_attempt(
    handler: HandlerDelegationWorkflow,
    correlation_id: UUID,
    *,
    backend_ref: str = BYOK_BACKEND_REF,
) -> list[Any]:
    handler.handle_routing_decision(
        _customer_route(correlation_id, backend_ref=backend_ref)
    )
    return list(
        handler.handle_inference_response(_provider_unavailable(correlation_id))
    )


def test_the_customer_route_is_retried_rather_than_terminalised() -> None:
    """AC4. The c1838c39 shape: one responder, a transient upstream failure."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_request(cid))

    events = _drive_one_attempt(handler, cid)

    routing = [e for e in events if isinstance(e, ModelRoutingIntent)]
    assert len(routing) == 1, (
        "a transient upstream 502 on the customer's own key ended the "
        "delegation; this is the leg-7 failure"
    )
    assert not any(isinstance(e, ModelDelegationResult) for e in events)
    assert not any(
        isinstance(e, ModelLlmDelegationEscalationTriggeredEvent) for e in events
    ), "a same-route retry is not a tier escalation and emits no escalation proof"
    assert handler.workflows[cid].escalation_count == 0


def test_the_retry_budget_is_bounded_and_then_the_workflow_terminalises() -> None:
    """AC4, the other half. A bounded retry, never a loop."""
    budget = byok_backend_max_retries(BYOK_BACKEND_REF)
    assert budget is not None
    assert budget >= 1

    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_request(cid))

    for attempt in range(budget):
        events = _drive_one_attempt(handler, cid)
        assert any(isinstance(e, ModelRoutingIntent) for e in events), (
            f"retry {attempt + 1} of {budget} was refused while budget remained"
        )

    exhausted = _drive_one_attempt(handler, cid)

    assert not any(isinstance(e, ModelRoutingIntent) for e in exhausted)
    terminal = next(e for e in exhausted if isinstance(e, ModelDelegationResult))
    assert isinstance(terminal, ModelDelegationFailed)
    assert terminal.quality_passed is False


def test_a_backend_the_catalogue_does_not_declare_gets_no_retry() -> None:
    """AC6. Fail-closed: no declared budget is not an unlimited one."""
    assert byok_backend_max_retries("byok-not-in-the-catalogue") is None

    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_request(cid))

    events = _drive_one_attempt(handler, cid, backend_ref="byok-not-in-the-catalogue")

    assert not any(isinstance(e, ModelRoutingIntent) for e in events)
    assert any(isinstance(e, ModelDelegationResult) for e in events)


def test_an_empty_completion_on_a_customer_route_still_terminalises() -> None:
    """Out of scope, bound so it cannot drift in unnoticed.

    A genuinely blank completion is not an availability fact, and this change
    does not make it one.
    """
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_request(cid))
    handler.handle_routing_decision(_customer_route(cid))

    events = list(
        handler.handle_inference_response(
            ModelInferenceResponseData(
                correlation_id=cid,
                content="",
                model_used=FREE_MODEL,
                latency_ms=50,
                error_message="API returned empty choices array",
            )
        )
    )

    assert not any(isinstance(e, ModelRoutingIntent) for e in events)
    assert any(isinstance(e, ModelDelegationResult) for e in events)


def test_every_catalogue_row_declares_its_own_retry_budget() -> None:
    """The budget is contract-declared per provider, never a code default."""
    catalogue = load_byok_provider_catalog()

    assert catalogue, "empty catalogue would make the assertion below vacuous"
    for provider, backend in catalogue.items():
        assert backend.max_retries >= 0, provider
        assert byok_backend_max_retries(backend.backend_id) == backend.max_retries
