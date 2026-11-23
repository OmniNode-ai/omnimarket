"""A house routing row is authenticated as the house, never as a customer (OMN-26365).

The defect: ``expected_credential_source_for`` returned ``CUSTOMER_KEY`` for every
decision whose tier is ``tenant_overlay``, before it looked at the reference. A
keyless house row (the h202 Qwen3.8-27B chat server: our own local model on our
own host, no key) resolves through the tenant overlay for the house tenant, so it
was stamped ``customer_key``, and the effect boundary refused the call with
``ONEX_MARKET_EFFECT_CREDENTIAL_UNRESOLVED`` because the intent carried no
credential reference.

The class under test: a decision that resolves to the HOUSE tenant is classified
as the house (``NONE`` with no reference, ``HOUSE`` with a house reference); a
customer tenant keeps requiring its own key, with or without a reference; and an
unattributed or missing tenant keeps the pre-existing result for every decision
shape, so a customer route whose reference went missing is still refused.

The existing ``test_omn18201_effect_boundary_auth_header.py`` pins the customer
arm and is not touched; this file only adds the house arm beside it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from omnibase_core.models.delegation.wire import EnumCredentialSource

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
    expected_credential_source_for,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    TENANT_OVERLAY_TIER_NAME,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    CREDENTIAL_UNRESOLVED_ONEX_CODE,
    HandlerInferenceIntent,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG, HOUSE_TENANT_UUID
from omnimarket.routing.tenant_overlay_resolver import HOUSE_OVERLAY_COST_TIER

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("stub_provider_quota_reader"),
]

# The h202 Qwen3.8-27B chat server: a local, keyless endpoint.
H202_CHAT_URL = "http://192.0.2.202:8000/v1/chat/completions"
H202_MODEL = "Qwen3.8-27B"
HOUSE_UUID = str(HOUSE_TENANT_UUID)
CUSTOMER_TENANT = "d562bf4a-a3f1-4016-8e33-7d4214f62fce"
CUSTOMER_REF = (
    "cred_d562bf4a-a3f1-4016-8e33-7d4214f62fce_openrouter_"
    "82dc9093c04a491295b86aa3d6001353"
)
HOUSE_REF = "llm.openrouter.api_key"

_EFFECT_MODULE = (
    "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent"
)


def _decision(**overrides: Any) -> ModelRoutingDecision:
    """The shape ``build_tenant_overlay_decision`` yields for a house row."""
    base: dict[str, Any] = {
        "correlation_id": uuid4(),
        "task_type": "summarization",
        "selected_model": H202_MODEL,
        "selected_backend_id": uuid4(),
        "selected_backend_ref": "local-heavy-reasoning",
        "endpoint_url": H202_CHAT_URL,
        "api_key_ref": None,
        "extra_headers": None,
        "cost_tier": HOUSE_OVERLAY_COST_TIER,
        "tier_name": TENANT_OVERLAY_TIER_NAME,
        "max_context_tokens": 32768,
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


# --------------------------------------------------------------------------
# (i) + (ii): the house tenant's overlay row
# --------------------------------------------------------------------------


@pytest.mark.parametrize("house_tenant", [HOUSE_TENANT_SLUG, HOUSE_UUID])
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_keyless_house_overlay_row_expects_no_credential(
    house_tenant: str, blank: str | None
) -> None:
    """The proof case: no reference, house tenant, tenant_overlay tier."""
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=blank), tenant_id=house_tenant
        )
        is EnumCredentialSource.NONE
    )


@pytest.mark.parametrize("house_tenant", [HOUSE_TENANT_SLUG, HOUSE_UUID])
def test_a_house_overlay_row_with_a_house_reference_stays_house(
    house_tenant: str,
) -> None:
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=HOUSE_REF), tenant_id=house_tenant
        )
        is EnumCredentialSource.HOUSE
    )


def test_a_house_overlay_row_carrying_a_minted_customer_reference_stays_customer() -> (
    None
):
    """The minted ``cred_`` shape wins wherever it appears, house tenant or not."""
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=CUSTOMER_REF), tenant_id=HOUSE_TENANT_SLUG
        )
        is EnumCredentialSource.CUSTOMER_KEY
    )


# --------------------------------------------------------------------------
# (iii): a customer tenant still requires its own key
# --------------------------------------------------------------------------


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_customer_overlay_row_with_no_reference_still_expects_a_key(
    blank: str | None,
) -> None:
    """The load-bearing case: a customer route whose reference went missing."""
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=blank, cost_tier="tenant_byok"),
            tenant_id=CUSTOMER_TENANT,
        )
        is EnumCredentialSource.CUSTOMER_KEY
    )


def test_a_customer_overlay_row_with_a_minted_reference_expects_a_key() -> None:
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=CUSTOMER_REF, cost_tier="tenant_byok"),
            tenant_id=CUSTOMER_TENANT,
        )
        is EnumCredentialSource.CUSTOMER_KEY
    )


def test_a_decision_saying_house_does_not_exempt_a_customer_tenant() -> None:
    """The house label on the decision is not the discriminator; the tenant is."""
    decision = _decision(api_key_ref=None, cost_tier=HOUSE_OVERLAY_COST_TIER)

    assert (
        expected_credential_source_for(decision, tenant_id=CUSTOMER_TENANT)
        is EnumCredentialSource.CUSTOMER_KEY
    )


# --------------------------------------------------------------------------
# (iv): an unattributed or missing tenant keeps the existing result
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tenant", [None, "", "   "])
def test_an_unattributed_tenant_keeps_the_overlay_arm_customer(
    tenant: str | None,
) -> None:
    """Missing tenant fails closed: the overlay tier still means a customer key."""
    for ref in (None, "", CUSTOMER_REF, HOUSE_REF):
        assert (
            expected_credential_source_for(_decision(api_key_ref=ref), tenant_id=tenant)
            is EnumCredentialSource.CUSTOMER_KEY
        )


def test_omitting_the_tenant_keeps_every_existing_result() -> None:
    """The default behaves as the function did before it took a tenant."""
    assert (
        expected_credential_source_for(_decision()) is EnumCredentialSource.CUSTOMER_KEY
    )
    assert (
        expected_credential_source_for(_decision(api_key_ref=CUSTOMER_REF))
        is EnumCredentialSource.CUSTOMER_KEY
    )
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=HOUSE_REF, cost_tier="cheap_cloud", tier_name="x")
        )
        is EnumCredentialSource.HOUSE
    )
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=None, cost_tier="local", tier_name="local")
        )
        is EnumCredentialSource.NONE
    )


@pytest.mark.parametrize("tenant", [None, HOUSE_TENANT_SLUG, CUSTOMER_TENANT])
def test_the_platform_tiers_are_unchanged_by_the_tenant(tenant: str | None) -> None:
    """The tenant only matters for the overlay tier; platform rungs are as before."""
    platform = {"cost_tier": "local", "tier_name": "local"}
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=None, **platform), tenant_id=tenant
        )
        is EnumCredentialSource.NONE
    )
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=HOUSE_REF, **platform), tenant_id=tenant
        )
        is EnumCredentialSource.HOUSE
    )
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=CUSTOMER_REF, **platform), tenant_id=tenant
        )
        is EnumCredentialSource.CUSTOMER_KEY
    )


# --------------------------------------------------------------------------
# (v): the dispatch seam -- orchestrator builds the intent, effect decides
# --------------------------------------------------------------------------


class _RecordingTransport:
    """Stands in for ``httpx.Client`` and records the requests it is given."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> _RecordingTransport:
        return self

    def __enter__(self) -> _RecordingTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append(url)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "id": "resp-1",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _RecordingTransport:
    recorder = _RecordingTransport()
    monkeypatch.setattr(f"{_EFFECT_MODULE}.httpx.Client", recorder)
    # No credential resolves: a keyless house row, or a customer ref gone missing.
    monkeypatch.setattr(f"{_EFFECT_MODULE}._resolve_api_key", lambda _ref: None)
    return recorder


def _dispatch(tenant_id: str, decision: ModelRoutingDecision) -> Any:
    """Run the orchestrator's routing leg for a request pinned to ``tenant_id``."""
    handler = HandlerDelegationWorkflow()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="Say ok.",
            task_type="research",  # type: ignore[arg-type]
            correlation_id=decision.correlation_id,
            emitted_at=datetime.now(UTC),
            tenant_id=tenant_id,
        )
    )
    intents = handler.handle_routing_decision(decision)
    assert len(intents) == 1, f"expected one inference intent, got {intents!r}"
    return intents[0]


@pytest.mark.parametrize("house_tenant", [HOUSE_TENANT_SLUG, HOUSE_UUID])
def test_a_house_overlay_decision_reaches_the_provider_without_a_key(
    house_tenant: str, transport: _RecordingTransport
) -> None:
    """The proof case end to end: the intent declares NONE and the effect calls."""
    intent = _dispatch(house_tenant, _decision())

    assert intent.api_key_ref is None
    assert intent.expected_credential_source is EnumCredentialSource.NONE

    response = HandlerInferenceIntent().handle(intent)

    assert CREDENTIAL_UNRESOLVED_ONEX_CODE not in response.error_message
    assert response.error_message == ""
    assert transport.requests == [H202_CHAT_URL]


def test_the_same_decision_under_a_customer_tenant_is_still_refused(
    transport: _RecordingTransport,
) -> None:
    """Control: identical decision, customer tenant -> refused, no outbound call."""
    intent = _dispatch(CUSTOMER_TENANT, _decision())

    assert intent.expected_credential_source is EnumCredentialSource.CUSTOMER_KEY

    response = HandlerInferenceIntent().handle(intent)

    assert CREDENTIAL_UNRESOLVED_ONEX_CODE in response.error_message
    assert transport.requests == []
