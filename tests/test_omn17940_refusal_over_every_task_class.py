# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17940 — the customer-key terminus, proven over EVERY task class.

``tests/test_omn17082_customer_key_terminus.py`` owns OMN-17372 AC2 (the typed
refusal has its own negative-path test) and AC3 (a mechanical check that no
customer-attributed delegation resolves a platform-held credential). Both were
narrower than they read, in three measured ways:

1. **Single class.** Its ``_request()`` helper hardcodes
   ``task_type="code_generation"``. Every assertion that the refusal is
   *typed* — the pinned ``error_code``, the ``reason``, the remediation — ran
   on that one class, while tier resolution is per-``task_type`` by
   construction.

2. **A class source that is wrong in both directions.** The mechanical sweep
   derives its walk set from ``use_for`` entries in ``routing_tiers.yaml``.
   That set is the same SIZE as the real denominator and not the same SET::

       submittable, never walked by the use_for sweep: {'agent_delegation'}
       walked by it, but not submittable at all:       {'simple_tasks'}

   ``use_for`` is a per-model capability tag, not a request vocabulary. The
   vocabulary is ``ModelDelegationRequest.task_type``'s ``Literal``, which
   pydantic enforces before routing is reached — and ``simple_tasks`` is not
   in it, so that case could never have exercised the terminus at all. The
   class it missed, ``agent_delegation``, is the one the contract explicitly
   declares unroutable (``routing_availability.status: pending_capability``,
   OMN-16811 / OMN-15961): "the class resolves NO backend on any of its
   declared tiers and a live delegation raises
   ``ONEX_CORE_041_INVALID_CONFIGURATION``". Whether a keyless customer asking
   for it receives the customer-facing refusal or that config error is exactly
   the question AC2 asks, and it is the one class the old sweep could never
   have answered it for.

3. **A crash scored as a pass.** The sweep's loop ends ``except Exception:
   continue``, so a class raising anything at all is scored identically to one
   that correctly refused. That proves "no decision was returned" (AC3's half)
   and cannot prove "a typed refusal was returned" (AC2's half).

This module fixes the denominator and the assertion strength together. The
class set is the request wire contract — the only vocabulary a customer can
actually submit — pinned equal to the task-class contract's universe so a
drift between the two authorities fails here rather than silently shrinking
the sweep; every ``(class, surface)`` pair asserts its own terminus by type;
and nothing is swallowed.

Ordering is the substance of the cloud proof.
``refuse_keyless_customer_on_cloud`` runs BEFORE ``_get_config()`` and
``_task_class_entry()`` in ``delta``, which is what makes the refusal reachable
for a class that has no routable tier at all. If that call ever moved below
tier resolution, ``agent_delegation`` would start terminating in
``ProtocolConfigurationError`` — "no tier has a configured endpoint for this
task type", a statement about our config — instead of "register a provider
key", a statement the customer can act on. That regression is invisible to a
sweep that walks only routable classes and swallows every exception; it fails
here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args
from uuid import uuid4

import pytest
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import ValidationError

from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta,
    house_credential_refs,
)
from omnimarket.routing.customer_key_terminus import (
    CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE,
    CustomerKeyRefusedError,
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
)
from omnimarket.routing.tenant_overlay_resolver import (
    ModelTenantRoutingOverlayBackend,
)

_CUSTOMER = "acme-corp"

# The minted tenant-credential shape (OMN-16944 /
# ``omnimarket.tenant_credential_ref``): ``cred_<tenant>_<provider>_<32 hex>``.
_MINTED_CUSTOMER_REF = "cred_acme-corp_openrouter_" + "0" * 32


# --- The denominator ----------------------------------------------------------


def _use_for_task_classes() -> frozenset[str]:
    """Task classes reachable from the shipped ladder's ``use_for`` entries.

    This is the set the pre-OMN-17940 mechanical sweep walked, reproduced here
    so the widening is measurable rather than asserted.
    """
    names: set[str] = set()
    for tier in routing._get_config().tiers:
        for model in tier.models:
            names.update(getattr(model, "use_for", ()) or ())
    return frozenset(names)


def _contract_task_classes() -> frozenset[str]:
    """The task-class contract's own closed universe."""
    return load_task_class_authority().universe


def _wire_admissible_task_classes() -> frozenset[str]:
    """Task classes a delegation request can actually CARRY.

    Derived from ``ModelDelegationRequest.task_type``'s own ``Literal``, which
    is the wire contract: pydantic rejects anything else before routing is
    reached. Read off the model rather than typed out, so a class added to the
    Literal is swept the day it lands.
    """
    return frozenset(
        get_args(ModelDelegationRequest.model_fields["task_type"].annotation)
    )


def customer_path_task_classes() -> frozenset[str]:
    """Every task class a customer request could carry.

    The wire-admissible set IS the denominator, and
    ``test_the_request_wire_contract_and_the_task_class_contract_agree`` pins
    it equal to the task-class contract's universe. The ladder's ``use_for``
    labels are deliberately NOT unioned in: ``use_for`` is a per-model
    capability tag, not a request vocabulary, and it contains
    ``simple_tasks``, which no request can carry.
    """
    return _wire_admissible_task_classes()


_TASK_CLASSES = sorted(customer_path_task_classes())


@pytest.fixture
def house_keys_planted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plant every house credential the shipped contract names.

    This is what makes the sweep falsifiable rather than vacuous: with these
    set, every house-credentialed backend is genuinely routable, so a decision
    that reaches a customer is one that WOULD have executed on OmniNode's
    provider account.
    """
    for env_name in (
        "LLM_GLM_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "LLM_GEMINI_API_KEY",
        "LLM_OPENROUTER_API_KEY",
        "LLM_VERTEX_ACCESS_TOKEN",
    ):
        monkeypatch.setenv(env_name, "house-key-planted-by-test")
    routing._load_bifrost_endpoints.cache_clear()


def _request(
    task_type: str, tenant_id: str | None = _CUSTOMER
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type=task_type,  # type: ignore[arg-type]
        prompt="x" * 100,
        emitted_at=datetime.now(tz=UTC),
        tenant_id=tenant_id,
    )


def _overlay(
    task_type: str, *, secret_ref: str | None
) -> ModelTenantRoutingOverlayBackend:
    return ModelTenantRoutingOverlayBackend(
        tenant_id=_CUSTOMER,
        task_type=task_type,
        backend_id="customer-own-provider",
        endpoint_url="https://openrouter.ai/api/v1/chat/completions",
        model_name="z-ai/glm-4.6",
        secret_ref=secret_ref,
        timeout_ms=None,
        max_tokens=None,
    )


# --- Controls on the denominator itself ---------------------------------------


def test_the_denominator_is_non_empty_so_no_sweep_passes_vacuously() -> None:
    """Positive control for every parametrised sweep below.

    A parametrisation that silently collapsed to zero cases would turn each
    assertion in this module into a green no-op. This is the guard against
    that, and it is why the count is asserted rather than assumed.
    """
    assert _use_for_task_classes(), "the shipped ladder declared no use_for classes"
    assert _contract_task_classes(), "the task-class contract declared no classes"
    assert len(_TASK_CLASSES) >= 15


def test_the_request_wire_contract_and_the_task_class_contract_agree() -> None:
    """The two authorities that define the denominator are the same set.

    ``ModelDelegationRequest.task_type``'s ``Literal`` says what a request may
    carry; ``task_class_contracts.v1.yaml`` says what the platform declares.
    A class in the contract but not the Literal is undeliverable; one in the
    Literal but not the contract routes with no declared policy. Today they
    are equal, and this fails the moment they drift — which is what makes
    sweeping either one a complete sweep.
    """
    assert _wire_admissible_task_classes() == _contract_task_classes()


def test_the_denominator_fixes_both_defects_in_the_old_use_for_class_source() -> None:
    """The pre-OMN-17940 sweep's class source was wrong in both directions.

    It walked ``use_for`` labels from ``routing_tiers.yaml``. That set is the
    same SIZE as the real denominator and not the same SET: it MISSED
    ``agent_delegation``, a genuinely submittable class, and it SPENT a case
    on ``simple_tasks``, which ``ModelDelegationRequest`` rejects outright — a
    phantom that could never have exercised the terminus at all.

    Named rather than counted, because a count is satisfied by any
    replacement.
    """
    use_for = _use_for_task_classes()
    denominator = customer_path_task_classes()

    # Direction 1 — the real class the old source could never reach.
    assert "agent_delegation" in denominator
    assert "agent_delegation" not in use_for

    # Direction 2 — the phantom the old source wasted a case on.
    assert "simple_tasks" in use_for
    assert "simple_tasks" not in denominator
    with pytest.raises(ValidationError):
        _request("simple_tasks")


def test_the_shipped_ladder_still_declares_a_house_credential_to_leak() -> None:
    """Precondition for every sweep below.

    If the shipped contract declared no house credential at all, "no customer
    reached one" would be trivially true and would prove nothing.
    """
    routing._load_bifrost_endpoints.cache_clear()
    assert "llm.glm.api_key" in house_credential_refs(routing._load_bifrost_endpoints())


# --- (AC2) The typed refusal, for every task class, on the cloud --------------


@pytest.mark.parametrize("task_type", _TASK_CLASSES)
@pytest.mark.usefixtures("house_keys_planted")
def test_every_task_class_refuses_a_keyless_customer_on_the_cloud(
    task_type: str,
) -> None:
    """Cloud + customer + no registered key -> the TYPED refusal. Every class.

    Strictly stronger than the pre-OMN-17940 sweep, which accepted any
    exception as evidence of non-leakage. Here the refusal must be the
    customer-facing one, carrying the pinned error code and the actionable
    reason — an unrelated crash fails, and so does a returned decision.
    """
    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        delta(_request(task_type), surface=EnumDelegationSurface.CLOUD)

    refusal = excinfo.value.refusal
    assert refusal.error_code == CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE
    assert refusal.reason is EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED
    assert refusal.task_type == task_type
    assert refusal.tenant_id == _CUSTOMER
    assert refusal.surface is EnumDelegationSurface.CLOUD
    assert "register a provider key" in refusal.remediation.lower()
    # Reference names are safe to surface; planted secret VALUES are not.
    assert "house-key-planted-by-test" not in str(refusal.model_dump())


@pytest.mark.parametrize("task_type", _TASK_CLASSES)
@pytest.mark.usefixtures("house_keys_planted")
def test_platform_work_is_unrefused_on_every_task_class(task_type: str) -> None:
    """Falsifier: the refusal is scoped to CUSTOMERS, not to the whole ladder.

    A guard that simply refused everything would satisfy every assertion
    above. OmniNode's own untenanted work must still route — or fail on its
    own config, never on the customer terminus.
    """
    try:
        decision = delta(
            _request(task_type, tenant_id=None), surface=EnumDelegationSurface.CLOUD
        )
    except ProtocolConfigurationError:
        # A class with no routable tier (``agent_delegation``) fails on config
        # for house work too. That is the declared behaviour, and it is NOT a
        # customer refusal — which is the distinction being asserted.
        return
    assert decision.selected_backend_ref


# --- (AC2/AC3) The customer-local terminus, for every task class --------------


@pytest.mark.parametrize("task_type", _TASK_CLASSES)
@pytest.mark.usefixtures("house_keys_planted")
def test_every_task_class_has_an_honest_customer_local_terminus(
    task_type: str,
) -> None:
    """Customer-local + no key -> a credential-free route, a typed refusal, or
    the declared config error. Never a house-credentialed decision.

    The customer-local surface is a DIFFERENT code path: the pre-ladder
    ``refuse_keyless_customer_on_cloud`` returns early there, so only the
    return-site ``enforce_customer_key_terminus`` guard stands between a
    keyless customer and whatever tier resolution picked. Cloud coverage says
    nothing about it, and before this test one class was proven.

    The three admissible outcomes are enumerated rather than swallowed, so a
    fourth — a decision carrying a platform credential — fails.
    """
    routing._load_bifrost_endpoints.cache_clear()
    house_refs = house_credential_refs(routing._load_bifrost_endpoints())

    decision = None
    refusal_reason: EnumCustomerKeyRefusalReason | None = None
    unroutable = False
    try:
        decision = delta(
            _request(task_type), surface=EnumDelegationSurface.CUSTOMER_LOCAL
        )
    except CustomerKeyRefusedError as refused:
        refusal_reason = refused.refusal.reason
    except ProtocolConfigurationError:
        # No tier serves this class at all. Nothing reached a provider, so
        # nothing pooled. Named explicitly rather than caught as `Exception`.
        unroutable = True

    if refusal_reason is not None:
        # Refusing is honest here only for the house-credential reason: a
        # keyless customer on their OWN machine has a credential-free
        # terminus, so NO_PROVIDER_KEY_REGISTERED would be the cloud rule
        # leaking onto a surface that pools nothing.
        assert (
            refusal_reason
            is EnumCustomerKeyRefusalReason.HOUSE_CREDENTIAL_ON_CUSTOMER_PATH
        )
        return
    if unroutable:
        return

    assert decision is not None
    assert decision.api_key_ref is None, (
        f"customer-local leak on task_type={task_type!r}: the route would "
        f"execute on api_key_ref={decision.api_key_ref!r} "
        f"(backend={decision.selected_backend_ref!r}, tier={decision.tier_name!r})"
    )
    assert decision.api_key_ref not in house_refs
    assert getattr(decision, "api_key_env", None) is None


# --- (AC3) The falsifier: a customer WITH a key routes, on every class --------


@pytest.mark.parametrize("task_type", _TASK_CLASSES)
@pytest.mark.usefixtures("house_keys_planted")
def test_a_customer_with_a_registered_key_is_not_refused_on_any_task_class(
    task_type: str,
) -> None:
    """Positive control for every refusal above.

    If the guard refused a keyed customer too, the sweeps would still be green
    and the product would be broken. The overlay path wholesale-replaces tier
    resolution, so this holds even for a class the ladder cannot serve.
    """
    decision = delta(
        _request(task_type),
        tenant_overlay=_overlay(task_type, secret_ref=_MINTED_CUSTOMER_REF),
        surface=EnumDelegationSurface.CLOUD,
    )
    assert decision.api_key_ref == _MINTED_CUSTOMER_REF
    assert decision.cost_tier == "tenant_byok"


@pytest.mark.parametrize("task_type", _TASK_CLASSES)
@pytest.mark.usefixtures("house_keys_planted")
def test_an_overlay_naming_a_house_credential_is_refused_on_every_task_class(
    task_type: str,
) -> None:
    """The overlay table is writable DATA, on every class.

    A row whose ``secret_ref`` names a platform credential is house pooling by
    configuration, and it bypasses the OMN-16944 minted-ref guard precisely
    because such a ref is not tenant-shaped. Proven for one class before this.
    """
    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        delta(
            _request(task_type),
            tenant_overlay=_overlay(task_type, secret_ref="llm.glm.api_key"),
            surface=EnumDelegationSurface.CLOUD,
        )
    refusal = excinfo.value.refusal
    assert (
        refusal.reason is EnumCustomerKeyRefusalReason.HOUSE_CREDENTIAL_ON_CUSTOMER_PATH
    )
    assert refusal.attempted_api_key_ref == "llm.glm.api_key"
    assert refusal.task_type == task_type
