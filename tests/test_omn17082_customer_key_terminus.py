# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17082 — the customer-path delegation terminus is a typed refusal.

**Operator ruling 2026-08-31 (supersedes this ticket's originally-recorded
scope).** There are no keyless customers and no house-credential execution,
ever. The recorded defect (SS-04) was narrower: the tenant-scoped BYOK
resolver guard had zero callers, so a *tenant-minted* ``secret_ref`` that
missed fell through to the house ``LLM_GLM_API_KEY``. OMN-16944 closed that
half by routing every minted ref through
``resolve_tenant_scoped_api_key_async`` at the resolver choke point.

The half that stayed open is the one this module proves, and it is the
dangerous one: a customer with **no key at all** never produces a
tenant-shaped ref, so the OMN-16944 guard is structurally unreachable on that
path. The routing authority happily resolved the platform ladder for them —
``cloud-glm`` with ``secret_ref: llm.glm.api_key`` and
``api_key_env: LLM_GLM_API_KEY`` — and the effect boundary authenticated the
call with OmniNode's own key. That is a dishonest terminus: the customer's
traffic and cost ran on the house provider account and every surface reported
success.

The required terminus, per the ruling:

* **cloud + no customer key → a typed, honest refusal** carrying a stable
  error contract that tells the customer to register a provider key. Never a
  house credential, and never OmniNode's own GPUs either — we do not offer
  inference.
* **customer-local CLI + no customer key → the typed local terminus** is
  kept. That path executes on the customer's own machine against an
  uncredentialed local backend; no OmniNode credential and no OmniNode
  compute is involved, so there is nothing to pool.

The two proofs this ticket owes:

(a) ``test_cloud_customer_without_a_key_is_refused_with_the_stable_contract``
    and its siblings — the typed refusal, its error code, and its remediation.
(b) ``test_no_house_credential_is_reachable_on_the_customer_path`` — the
    mechanical one. It plants every house credential the shipped bifrost
    contract names into the environment, walks every task class the routing
    ladder declares, and fails if ANY of them yields a decision a customer's
    work could execute on. It fails if a house credential would execute
    customer work, which is the assertion the ruling actually asks for; it
    does not merely assert that today's code takes today's branch.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

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
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.customer_key_terminus import (
    CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE,
    CustomerKeyRefusedError,
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
    is_customer_attributed,
)
from omnimarket.routing.tenant_overlay_resolver import (
    ModelTenantRoutingOverlayBackend,
)

_CUSTOMER = "acme-corp"

# A ref carrying the minted tenant-credential shape (OMN-16944 /
# ``omnimarket.tenant_credential_ref``): ``cred_<tenant>_<provider>_<32 hex>``.
_MINTED_CUSTOMER_REF = "cred_acme-corp_openrouter_" + "0" * 32


# --- Fixtures -----------------------------------------------------------------

# A two-backend contract: one uncredentialed local backend (the customer-local
# terminus) and one house-credentialed cloud backend (the pooling hazard).
_BIFROST_LOCAL_AND_HOUSE = textwrap.dedent(
    """\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        endpoint_url: "http://local.test:8000/v1/chat/completions"
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
      - backend_id: cloud-glm
        endpoint_url: "https://api.z.ai/api/coding/paas/v4/chat/completions"
        model_name: glm-5.3-flash
        secret_ref: llm.glm.api_key
        api_key_env: LLM_GLM_API_KEY
        tier: cheap_cloud
        timeout_ms: 30000
        max_tokens: 65536
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "c0ffee00-0011-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder, cloud-glm]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0012-4000-8000-000000000001"
    default_backends:
      - local-coder
    circuit_breaker:
      failure_threshold: 5
      window_seconds: 30
    failover:
      max_attempts: 3
      backoff_base_ms: 500
    shadow_mode:
      enabled: false
      policy_version: "test"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)


@pytest.fixture
def local_and_house_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Route against a contract with one free-local and one house-key backend."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_LOCAL_AND_HOUSE)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


@pytest.fixture
def house_keys_planted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plant every house credential the shipped contract names.

    This is what makes the proofs falsifiable rather than vacuous: with these
    set, every house-credentialed backend is genuinely routable, so a decision
    that reaches a customer is a decision that WOULD have executed on
    OmniNode's provider account.
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


def _request(
    task_type: str = "code_generation",
    tenant_id: str | None = None,
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type=task_type,  # type: ignore[arg-type]
        prompt="x" * 100,
        emitted_at=datetime.now(tz=UTC),
        tenant_id=tenant_id,
    )


def _overlay(
    *,
    secret_ref: str | None,
    tenant_id: str = _CUSTOMER,
    task_type: str = "code_generation",
) -> ModelTenantRoutingOverlayBackend:
    return ModelTenantRoutingOverlayBackend(
        tenant_id=tenant_id,
        task_type=task_type,
        backend_id="customer-own-provider",
        endpoint_url="https://openrouter.ai/api/v1/chat/completions",
        model_name="z-ai/glm-4.6",
        secret_ref=secret_ref,
        timeout_ms=None,
        max_tokens=None,
    )


# --- (a) The typed refusal and its stable error contract ----------------------


@pytest.mark.usefixtures("local_and_house_backends", "house_keys_planted")
def test_cloud_customer_without_a_key_is_refused_with_the_stable_contract() -> None:
    """Cloud + customer + no registered key -> typed refusal, not a decision.

    The house key is present in the environment and the house backend is
    routable. Before this ticket this call returned a ModelRoutingDecision
    carrying ``api_key_ref='llm.glm.api_key'``, which the effect boundary
    resolved to the planted value.
    """
    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        delta(
            _request(tenant_id=_CUSTOMER),
            surface=EnumDelegationSurface.CLOUD,
        )

    refusal = excinfo.value.refusal
    assert refusal.error_code == CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE
    assert refusal.reason is EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED
    assert refusal.tenant_id == _CUSTOMER
    assert refusal.surface is EnumDelegationSurface.CLOUD
    assert refusal.task_type == "code_generation"
    # The refusal must TELL the customer what to do; an opaque failure is the
    # dishonest terminus in a different costume.
    assert "register a provider key" in refusal.remediation.lower()
    # It must carry no secret material — refs are safe to surface, values are not.
    assert "house-key-planted-by-test" not in str(refusal.model_dump())


@pytest.mark.usefixtures("local_and_house_backends", "house_keys_planted")
def test_the_refusal_error_code_is_stable_and_machine_readable() -> None:
    """The error contract is a pinned literal, not a prose message.

    A customer-facing surface routes on this code. Changing it is a breaking
    API change and must fail here first.
    """
    assert CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE == (
        "delegation.customer_provider_key.absent"
    )
    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        delta(_request(tenant_id=_CUSTOMER), surface=EnumDelegationSurface.CLOUD)
    payload = excinfo.value.refusal.model_dump(mode="json")
    assert payload["error_code"] == "delegation.customer_provider_key.absent"
    assert payload["reason"] == "no_provider_key_registered"
    assert payload["surface"] == "cloud"


@pytest.mark.usefixtures("local_and_house_backends", "house_keys_planted")
def test_customer_with_a_registered_key_is_not_refused() -> None:
    """The refusal is scoped to the absence of a key, not to being a customer.

    Falsifier for a guard that refuses everything: a customer whose overlay
    carries their own minted credential ref routes normally.
    """
    decision = delta(
        _request(tenant_id=_CUSTOMER),
        tenant_overlay=_overlay(secret_ref=_MINTED_CUSTOMER_REF),
        surface=EnumDelegationSurface.CLOUD,
    )
    assert decision.api_key_ref == _MINTED_CUSTOMER_REF
    assert decision.cost_tier == "tenant_byok"


@pytest.mark.usefixtures("local_and_house_backends", "house_keys_planted")
def test_an_overlay_naming_a_house_credential_is_refused() -> None:
    """A registered overlay is not a licence to name OmniNode's own key.

    The overlay table is operator/customer-writable data. A row whose
    ``secret_ref`` names a platform credential is house pooling by
    configuration — the exact outcome this ticket exists to make impossible —
    and it bypasses the OMN-16944 minted-ref guard because the ref is not
    tenant-shaped.
    """
    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        delta(
            _request(tenant_id=_CUSTOMER),
            tenant_overlay=_overlay(secret_ref="llm.glm.api_key"),
            surface=EnumDelegationSurface.CLOUD,
        )
    refusal = excinfo.value.refusal
    assert (
        refusal.reason is EnumCustomerKeyRefusalReason.HOUSE_CREDENTIAL_ON_CUSTOMER_PATH
    )
    assert refusal.attempted_api_key_ref == "llm.glm.api_key"


@pytest.mark.usefixtures("local_and_house_backends", "house_keys_planted")
def test_a_customer_declared_auth_free_backend_is_routable_on_the_cloud() -> None:
    """ "No credential" is three different facts; only one of them is a refusal.

    An overlay row with no ``secret_ref`` means the customer pointed us at an
    endpoint of THEIRS that needs no auth — their infrastructure, their cost.
    Refusing it would be the guard mistaking "OmniNode pays" for "nobody
    declared a key", which are not the same claim.
    """
    decision = delta(
        _request(tenant_id=_CUSTOMER),
        tenant_overlay=_overlay(secret_ref=None),
        surface=EnumDelegationSurface.CLOUD,
    )
    assert decision.api_key_ref is None
    assert decision.endpoint_url == "https://openrouter.ai/api/v1/chat/completions"


# --- The customer-local CLI terminus is kept ----------------------------------


@pytest.mark.usefixtures("local_and_house_backends", "house_keys_planted")
def test_customer_local_surface_keeps_its_typed_local_terminus() -> None:
    """Customer-local CLI + no key -> the uncredentialed local backend.

    Nothing is pooled here: the call executes on the customer's own machine
    against a backend that carries no credential at all.
    """
    decision = delta(
        _request(tenant_id=_CUSTOMER),
        surface=EnumDelegationSurface.CUSTOMER_LOCAL,
    )
    assert decision.api_key_ref is None
    assert decision.tier_name == "local"
    assert decision.endpoint_url == "http://local.test:8000/v1/chat/completions"


@pytest.mark.usefixtures("local_and_house_backends", "house_keys_planted")
def test_customer_local_surface_still_refuses_a_house_credential() -> None:
    """The local surface is not a bypass.

    Escalating past the free local rung on the customer's own machine would
    still authenticate with OmniNode's key. ``min_tier_name`` pins the ladder
    to the house-credentialed tier, which must refuse on BOTH surfaces.
    """
    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        delta(
            _request(tenant_id=_CUSTOMER),
            min_tier_name="cheap_cloud",
            surface=EnumDelegationSurface.CUSTOMER_LOCAL,
        )
    assert (
        excinfo.value.refusal.reason
        is EnumCustomerKeyRefusalReason.HOUSE_CREDENTIAL_ON_CUSTOMER_PATH
    )


# --- (b) Mechanical absence of any house-key fallthrough ----------------------


@pytest.mark.usefixtures("house_keys_planted")
def test_no_house_credential_is_reachable_on_the_customer_path() -> None:
    """THE proof: walk the SHIPPED ladder, assert no customer decision exists.

    This runs against the real ``bifrost_delegation.yaml`` /
    ``routing_tiers.yaml`` the product ships — not a fixture — with every
    house credential planted so every house-credentialed backend is genuinely
    routable. For every task class the routing authority declares, a customer
    request on the cloud surface must terminate in a refusal.

    It fails if a house credential would execute customer work: any task class
    that yields a decision fails the assertion and names the credential it
    would have executed on.
    """
    routing._load_bifrost_endpoints.cache_clear()
    house_refs = house_credential_refs(routing._load_bifrost_endpoints())
    # The fixture is only meaningful if the shipped contract actually declares
    # house credentials to leak.
    #
    # OMN-17372: the companion assertion `"LLM_GLM_API_KEY" in house_refs` was
    # REMOVED, not relaxed. It asserted the shipped config still declared a
    # house credential by ENV-VAR NAME, via `api_key_env` — and that field has
    # since been deleted from every backend in `bifrost_delegation.yaml`,
    # because it let a keyless customer's delegation authenticate on OmniNode's
    # own provider account. The precondition it guarded is unweakened: the
    # shipped ladder still declares house credentials by `secret_ref`, which
    # the surviving assertion checks, and that is what makes the planted-key
    # fixture below genuinely able to execute customer work if the terminus
    # leaked. `house_credential_refs` still scans `api_key_env` as
    # defence-in-depth, so a re-added field would be caught here too.
    assert "llm.glm.api_key" in house_refs

    # Every ``use_for`` task class declared anywhere in the shipped ladder.
    declared_task_types: set[str] = {"code_generation"}
    for tier in routing._get_config().tiers:
        for model in tier.models:
            declared_task_types.update(getattr(model, "use_for", ()) or ())
    assert len(declared_task_types) > 1, "ladder declared no task classes to walk"

    leaked: list[str] = []
    for task_type in sorted(declared_task_types):
        try:
            decision = delta(
                _request(task_type=task_type, tenant_id=_CUSTOMER),
                surface=EnumDelegationSurface.CLOUD,
            )
        except CustomerKeyRefusedError:
            continue
        except Exception:
            # An unroutable task class is not a leak; it never reaches a
            # provider at all. Only a returned DECISION is a leak.
            continue
        leaked.append(
            f"task_type={task_type!r} would execute on "
            f"api_key_ref={decision.api_key_ref!r} "
            f"backend={decision.selected_backend_ref!r} "
            f"tier={decision.tier_name!r}"
        )

    assert not leaked, (
        "house-key fallthrough on the customer path — a customer with no "
        "registered provider key reached a routable backend:\n" + "\n".join(leaked)
    )


def test_house_credential_refs_is_derived_from_the_contract_not_hardcoded() -> None:
    """The house-credential set is read off the shipped contract.

    A new house-credentialed backend added to ``bifrost_delegation.yaml`` is
    covered the day it lands, with no allowlist to update. Proven by feeding
    the deriver a backend the code has never seen.
    """
    routing._load_bifrost_endpoints.cache_clear()
    invented = {
        "brand-new-backend": routing.BifrostBackendRef(
            endpoint_url="https://example.invalid/v1/chat/completions",
            model_name="m",
            timeout_ms=1000,
            max_tokens=128,
            api_key_ref="llm.brand_new.api_key",
            api_key_env="BRAND_NEW_API_KEY",
        )
    }
    derived = house_credential_refs(invented)
    assert derived == frozenset({"llm.brand_new.api_key", "BRAND_NEW_API_KEY"})


# --- Platform (house-tenant) work is untouched --------------------------------


@pytest.mark.usefixtures("local_and_house_backends", "house_keys_planted")
def test_platform_own_work_is_unchanged_on_both_tenant_forms() -> None:
    """OmniNode's own work runs on OmniNode's own key. That is not pooling.

    Both the untenanted form and the explicit house tenant must produce the
    identical decision they produced before this guard existed.
    """
    untenanted = delta(_request(tenant_id=None), surface=EnumDelegationSurface.CLOUD)
    house = delta(
        _request(tenant_id=HOUSE_TENANT_SLUG), surface=EnumDelegationSurface.CLOUD
    )
    assert untenanted.selected_backend_ref == house.selected_backend_ref
    assert untenanted.api_key_ref == house.api_key_ref
    assert not is_customer_attributed(None)
    assert not is_customer_attributed(HOUSE_TENANT_SLUG)
    assert not is_customer_attributed("   ")
    assert is_customer_attributed(_CUSTOMER)


@pytest.mark.usefixtures("local_and_house_backends", "house_keys_planted")
def test_platform_work_may_escalate_to_the_house_credentialed_tier() -> None:
    """Falsifier for a guard that is really just 'never route to cheap_cloud'."""
    decision = delta(
        _request(tenant_id=HOUSE_TENANT_SLUG),
        min_tier_name="cheap_cloud",
        surface=EnumDelegationSurface.CLOUD,
    )
    assert decision.api_key_ref == "llm.glm.api_key"
    assert decision.tier_name == "cheap_cloud"


# --- The deployed cloud consumer declares the cloud surface -------------------


def test_the_bus_routing_consumer_declares_the_cloud_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HandlerRoutingIntent`` is the deployed multi-tenant consumer.

    The surface is not read from an env var — it is a property of WHICH entry
    point ran. This asserts the bus consumer names it explicitly, so a future
    edit that drops the kwarg (silently reverting to the default) is still
    safe, and one that passes CUSTOMER_LOCAL fails here.
    """
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_routing_intent,
    )

    seen: dict[str, object] = {}

    def _capture(request: object, **kwargs: object) -> object:
        seen.update(kwargs)
        raise CustomerKeyRefusedError.for_missing_key(
            tenant_id=_CUSTOMER,
            task_type="code_generation",
            correlation_id=uuid4(),
            surface=EnumDelegationSurface.CLOUD,
        )

    monkeypatch.setattr(handler_routing_intent, "routing_delta", _capture)
    handler = handler_routing_intent.HandlerRoutingIntent(tenant_overlay_db=None)

    from omnibase_core.models.delegation.wire import ModelRoutingIntent

    intent = ModelRoutingIntent(payload=_request(tenant_id=_CUSTOMER))
    with pytest.raises(CustomerKeyRefusedError):
        handler.handle(intent)
    assert seen["surface"] is EnumDelegationSurface.CLOUD


@pytest.mark.usefixtures("local_and_house_backends", "house_keys_planted")
def test_the_default_surface_is_the_strict_one() -> None:
    """An unmigrated call site inherits the strict posture, not the lax one.

    Fail-closed: forgetting to thread the surface must refuse a customer, not
    silently hand them the house key.
    """
    with pytest.raises(CustomerKeyRefusedError):
        delta(_request(tenant_id=_CUSTOMER))
