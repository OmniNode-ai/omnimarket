# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18191 — a withdrawn credential is ABSENT, not a keyless route.

The defect, measured as a controlled A/B on the onex-lab lane on 2026-09-11
(two runs, same lane, same sha, same provider key, minutes apart; the only
variable was the tenant's credential history):

* a tenant that had NEVER held a provider key was refused correctly, with the
  typed ``CustomerKeyRefusedError`` and no outbound call;
* the SAME tenant, after holding one and withdrawing it, reached OpenRouter
  with no credential attached and got the vendor's ``401 No cookie auth
  credentials found`` — the response to a request carrying no Authorization
  header at all.

Mechanism: withdrawal does not remove the tenant's overlay row. It keeps the
row and blanks ``secret_ref`` (``handler_tenant_credentials_projection.
_revoke_routing_overlay``). ``delta()`` found a row, concluded the tenant had
a route, and passed ``customer_declared_backend=True`` to the terminus, which
reads an absent ref on a customer-declared backend as "the customer's own
auth-free endpoint". So the absent-key branch was never taken: the condition
selecting it was "no overlay ROW", not "no usable CREDENTIAL".

A keyless delegation that reaches the vendor has already done the thing the
refusal exists to prevent. The customer-visible symptom is mild — the call
fails either way — but the platform made an unauthenticated outbound request
on a customer's route and then blamed the vendor for a condition it could
have refused locally.

What this module binds:

* AC1 — the reducer returns the typed refusal for a tenant whose overlay row
  names no usable credential, on the same terms as a tenant with no row.
* AC2 — no outbound provider call is made in that case, asserted at the
  transport seam the effect boundary posts through, with the credential-present
  arm as the positive control that the seam is genuinely reachable.
* AC4 — the refusal carries the refusal class the receipt names.

AC3 (the C7 error leg green on the lab and on staging) is a live-chain proof
and is cited on the ticket, not here.

The test is written against the ABSENCE OF A USABLE REF rather than against a
withdrawal marker, deliberately. Keying on "was this row withdrawn" would bind
only the one writer we know about and would let the next path that leaves a
partial row route keylessly again. ``None``, ``""`` and whitespace are one
condition: the resolver does not strip (``_optional_str``), and all three mean
the same thing to the effect boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    TENANT_OVERLAY_TIER_NAME,
    delta,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.routing.customer_key_terminus import (
    CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE,
    CUSTOMER_PROVIDER_KEY_ABSENT_ONEX_CODE,
    CustomerKeyRefusedError,
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
)
from omnimarket.routing.tenant_overlay_resolver import (
    TENANT_OVERLAY_TABLE,
    resolve_tenant_overlay,
)

_CUSTOMER = "0e263b9c-withdrawn-credential-tenant"
_TASK_TYPE = "code_generation"
_LIVE_REF = "cred_0e263b9c_openrouter_9f1c4ab27e5d4e0aa1b3c6d8e2f40517"

# The three shapes a withdrawal or a partial write can leave behind. NULL is
# what ``_revoke_routing_overlay`` writes today; the empty string is what the
# C7 chain's own post-withdrawal read observed on the lane; whitespace is
# neither, and is here so the guard cannot be narrowed to the two we have
# happened to see.
_KEYLESS_SECRET_REFS: tuple[str | None, ...] = (None, "", "   ")


def _request(tenant_id: str | None = _CUSTOMER) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type=_TASK_TYPE,  # type: ignore[arg-type]
        prompt="Write a function that returns the nth triangular number." * 4,
        emitted_at=datetime.now(tz=UTC),
        tenant_id=tenant_id,
    )


def _seed_overlay_row(db: InmemoryDatabaseAdapter, *, secret_ref: str | None) -> None:
    """Seed the tenant's BYOK overlay row via a pure DATA write.

    Mirrors what ``_project_routing_overlay`` writes from a
    ``credential-registered`` event, so the row under test is the shape the
    real projection produces rather than one invented here.
    """
    db.upsert(
        TENANT_OVERLAY_TABLE,
        "tenant_id,task_type",
        {
            "tenant_id": _CUSTOMER,
            "task_type": _TASK_TYPE,
            "backend_id": "openrouter-byok",
            "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
            "model_name": "nvidia/nemotron-3-ultra-550b-a55b:free",
            "secret_ref": secret_ref,
            "timeout_ms": None,
            "max_tokens": None,
        },
    )


@pytest.fixture
def posted_endpoints(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every URL the effect boundary's transport seam is asked to POST.

    ``transport.post_chat_completion`` is the one function the delegation call
    effect posts a chat completion through, on both the httpx and the LAN-curl
    profiles. Patching it here means an outbound call cannot be made without
    this list growing, and it never reaches the network from a unit test.
    """
    recorded: list[str] = []

    def _spy(*, endpoint_url: str, **_kwargs: Any) -> None:
        recorded.append(endpoint_url)
        raise AssertionError(
            "transport.post_chat_completion was reached; this spy never "
            "returns a response, so any caller that got here is recorded "
            "before it can proceed."
        )

    monkeypatch.setattr(transport, "post_chat_completion", _spy)
    return recorded


def _route_then_call(
    request: ModelDelegationRequest,
    *,
    overlay_secret_ref: str | None,
) -> None:
    """Resolve a route the way the request-time boundary does, then post it.

    Both arms of the AC2 proof run through this one function so the arms
    differ in exactly one input: what the tenant's overlay row carries in
    ``secret_ref``. The read goes through the real ``resolve_tenant_overlay``
    against the same in-memory adapter the sibling overlay suites use, so a
    stored NULL becomes ``None`` by the real mapping rather than by a value
    typed into the test.

    The POST at the end stands in for the delegation call effect's own
    dispatch, which is a separate node joined by the bus and not reachable in
    process. What it faithfully reproduces is the invariant under test: a
    routing decision is the thing the effect posts, so a reducer that returns
    one has authorised an outbound call, and a reducer that raises has not.
    The end-to-end proof across the real bus is the C7 chain on the lab (AC3).
    """
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(db, secret_ref=overlay_secret_ref)
    overlay = resolve_tenant_overlay(db, tenant_id=_CUSTOMER, task_type=_TASK_TYPE)
    assert overlay is not None, (
        "the row must resolve for this proof to mean anything: the defect is "
        "about a row that EXISTS and names no credential, not about a missing "
        "row"
    )
    decision = delta(
        request,
        tenant_overlay=overlay,
        surface=EnumDelegationSurface.CLOUD,
    )
    transport.post_chat_completion(
        endpoint_url=decision.endpoint_url,
        payload={"model": decision.selected_model, "messages": []},
        timeout_seconds=decision.timeout_ms / 1000,
    )


# --- AC1 + AC4: the typed refusal, on the same terms as a never-keyed tenant --


@pytest.mark.unit
@pytest.mark.parametrize("secret_ref", _KEYLESS_SECRET_REFS)
def test_overlay_row_naming_no_credential_is_refused(secret_ref: str | None) -> None:
    """RED before OMN-18191: this returned a routable decision instead.

    The row exists, names a real backend and a real model, and carries no
    usable credential. That is the state a withdrawal leaves behind, and on
    the cloud it is the same fact as having no row at all.
    """
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(db, secret_ref=secret_ref)
    overlay = resolve_tenant_overlay(db, tenant_id=_CUSTOMER, task_type=_TASK_TYPE)
    assert overlay is not None

    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        delta(
            _request(),
            tenant_overlay=overlay,
            surface=EnumDelegationSurface.CLOUD,
        )

    refusal = excinfo.value.refusal
    # AC4: the class the receipt names, in both the public and the boundary
    # spellings, because the two travel on different surfaces.
    assert refusal.error_code == CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE
    assert excinfo.value.error_code == CUSTOMER_PROVIDER_KEY_ABSENT_ONEX_CODE
    assert refusal.reason is EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED
    assert refusal.tenant_id == _CUSTOMER
    assert refusal.task_type == _TASK_TYPE
    assert refusal.surface is EnumDelegationSurface.CLOUD
    assert "register a provider key" in refusal.remediation.lower()


@pytest.mark.unit
def test_withdrawn_and_never_keyed_tenants_reach_the_same_refusal() -> None:
    """The two states must be ONE state, not two similar ones.

    This is the assertion that would have caught the defect. Before the fix
    the never-keyed tenant raised and the withdrawn tenant returned a
    decision, so the two paths did not merely differ in wording — one refused
    and one called the vendor.
    """
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(db, secret_ref=None)
    withdrawn_overlay = resolve_tenant_overlay(
        db, tenant_id=_CUSTOMER, task_type=_TASK_TYPE
    )

    never_keyed_overlay = resolve_tenant_overlay(
        InmemoryDatabaseAdapter(), tenant_id=_CUSTOMER, task_type=_TASK_TYPE
    )
    assert never_keyed_overlay is None

    with pytest.raises(CustomerKeyRefusedError) as withdrawn:
        delta(
            _request(),
            tenant_overlay=withdrawn_overlay,
            surface=EnumDelegationSurface.CLOUD,
        )
    with pytest.raises(CustomerKeyRefusedError) as never_keyed:
        delta(
            _request(),
            tenant_overlay=never_keyed_overlay,
            surface=EnumDelegationSurface.CLOUD,
        )

    withdrawn_payload = withdrawn.value.refusal.model_dump(mode="json")
    never_keyed_payload = never_keyed.value.refusal.model_dump(mode="json")
    # correlation_id is per-request by construction; everything that describes
    # WHY the delegation was refused must be identical.
    withdrawn_payload.pop("correlation_id")
    never_keyed_payload.pop("correlation_id")
    assert withdrawn_payload == never_keyed_payload


# --- AC2: nothing leaves the platform, with a positive control ----------------


@pytest.mark.unit
@pytest.mark.parametrize("secret_ref", _KEYLESS_SECRET_REFS)
def test_no_outbound_provider_call_for_a_keyless_overlay(
    secret_ref: str | None, posted_endpoints: list[str]
) -> None:
    """AC2. The refusal happens before anything is posted anywhere."""
    with pytest.raises(CustomerKeyRefusedError):
        _route_then_call(_request(), overlay_secret_ref=secret_ref)

    assert posted_endpoints == [], (
        "a delegation for a tenant with no usable credential reached the "
        "transport seam; on the lane this is the request OpenRouter answered "
        "401 'No cookie auth credentials found'"
    )


@pytest.mark.unit
def test_positive_control_a_credential_present_does_make_the_call(
    posted_endpoints: list[str],
) -> None:
    """AC2's positive control — without it the zero above proves nothing.

    Same tenant, same row, same code path; the only change is that the
    overlay carries the customer's own minted credential reference. The
    transport seam IS reached, which is what makes the empty list in the test
    above a fact about the fix rather than about an unreachable seam or a
    harness that never posts.
    """
    with pytest.raises(AssertionError, match="post_chat_completion was reached"):
        _route_then_call(_request(), overlay_secret_ref=_LIVE_REF)

    assert posted_endpoints == ["https://openrouter.ai/api/v1/chat/completions"]


@pytest.mark.unit
def test_a_credential_present_still_routes_to_the_tenants_own_backend() -> None:
    """The guard is scoped to the absence of a credential, not to BYOK.

    Falsifier for a fix that refuses every overlay row: a tenant whose row
    carries their minted ref must still resolve their own backend, on their
    own tier, against their own reference.
    """
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(db, secret_ref=_LIVE_REF)
    overlay = resolve_tenant_overlay(db, tenant_id=_CUSTOMER, task_type=_TASK_TYPE)

    decision = delta(
        _request(),
        tenant_overlay=overlay,
        surface=EnumDelegationSurface.CLOUD,
    )

    assert decision.api_key_ref == _LIVE_REF
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.cost_tier == "tenant_byok"
    assert decision.endpoint_url == "https://openrouter.ai/api/v1/chat/completions"


@pytest.mark.unit
def test_customer_local_surface_is_unchanged() -> None:
    """The cloud guard does not reach the customer's own machine.

    An uncredentialed backend on the customer's own hardware pools nothing of
    ours, and ``CUSTOMER_LOCAL`` is the one surface where an auth-free
    endpoint is the honest terminus. Narrowing the guard to ``CLOUD`` is
    deliberate, so it is asserted rather than left to be rediscovered.
    """
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(db, secret_ref=None)
    overlay = resolve_tenant_overlay(db, tenant_id=_CUSTOMER, task_type=_TASK_TYPE)

    decision = delta(
        _request(),
        tenant_overlay=overlay,
        surface=EnumDelegationSurface.CUSTOMER_LOCAL,
    )

    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.api_key_ref in (None, "")
