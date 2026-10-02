# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local path's BYOK route: a customer's own key replaces the house rung (OMN-18694).

On the hosted path a customer's registered key becomes a
``delegation_routing_tenant_overlay`` row, and
``_decision_from_tenant_overlay`` selects it. The local path
(``LocalDelegationDispatchPort``) never reaches that reducer: it has no broker
and no Postgres, and resolves its backend straight out of
``bifrost_delegation.yaml`` through ``resolve_delegation_backend``. Every cloud
rung in that file carries a HOUSE ``secret_ref`` (``llm.<provider>.<field>``).

So on a customer machine, before this module, the only cloud route that existed
was one authenticated with OUR credential. That is what OMN-17082 forbids and
what OMN-18694 AC3 falsifies.

What this module does
---------------------
:func:`substitute_local_byok_route` is a single transform applied to an
already-resolved backend:

* the backend carries no ``secret_ref`` -- a free local rung on owned hardware
  -- and is returned UNCHANGED. Cheapest-first is unaffected: a customer with a
  local model still uses it, and pays nobody;
* the backend carries a HOUSE ``secret_ref`` and the customer has registered a
  key for that provider -- it is REPLACED by the declared BYOK backend, carrying
  the customer's own minted reference;
* the backend carries a HOUSE ``secret_ref`` and the customer has registered
  nothing -- it is returned unchanged, and fails closed one frame later when the
  house reference does not resolve on their machine. This module does not
  manufacture a refusal the secret boundary already owns.

Why substitution rather than a new tier
---------------------------------------
A customer's chain of responders has exactly ONE member by construction
(OMN-17082, and the ``max_retries`` reasoning in
``byok_provider_backends.v1.yaml``): no house credential may answer their work,
so the house ladder is not a lawful successor for them. Inserting a tier would
imply a successor relationship that must never be taken. Substituting in place
says the true thing -- *where the platform would have spent its own money, the
customer spends theirs* -- and leaves the ladder's shape, the tier order, and
every escalation decision exactly as they were.

Nothing here reads a secret VALUE. The catalogue supplies the endpoint and
budgets; the local credential adapter supplies the REFERENCE and the model the
key resolved at registration (OMN-20157); the value is resolved at the effect
boundary and nowhere else.

Related:
    - OMN-18694: gap A, a customer's OpenRouter key routes a local delegation
    - OMN-17372 / OMN-17353: the declared BYOK provider catalogue
    - OMN-17082: no house credential may answer customer work
    - OMN-16944: a tenant-shaped ref never sees an env fallback
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from omnimarket.inference.local_byok_credential_adapter import (
    registered_local_byok_providers,
    resolve_local_byok_credential_model,
    resolve_local_byok_credential_plan,
    resolve_local_byok_credential_ref,
)
from omnimarket.routing.byok_provider_backends import (
    BYOK_MODEL_UNRESOLVED,
    ModelByokProviderBackend,
    customer_provider_catalogue,
    resolve_byok_backend_by_id,
    resolve_byok_provider_backend,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

logger = logging.getLogger(__name__)

#: The shape of a HOUSE credential reference on a platform rung. Identical to
#: ``byok_provider_backends._HOUSE_SECRET_REF_PATTERN`` by intent: the middle
#: segment of ``llm.<provider>.<field>`` is the provider slug a customer
#: registers a key under, which is what makes the substitution addressable.
HOUSE_SECRET_REF_PATTERN: re.Pattern[str] = re.compile(
    r"^llm\.(?P<slug>[a-z0-9_-]+)\.[a-z0-9_]+$"
)


def house_provider_slug(secret_ref: str | None) -> str | None:
    """Return the provider slug a house ``secret_ref`` names, or ``None``.

    ``None`` for an absent ref (a keyless local rung) and for any ref that is
    not house-shaped -- notably a tenant-minted ``cred_...`` ref, which is
    already a customer's and must never be substituted a second time.
    """
    if not secret_ref:
        return None
    match = HOUSE_SECRET_REF_PATTERN.fullmatch(secret_ref)
    return match.group("slug") if match is not None else None


def substitute_local_byok_route(
    backend: ModelResolvedDelegationBackend,
    *,
    db_path: Path | None = None,
) -> ModelResolvedDelegationBackend:
    """Replace a house-keyed rung with the customer's own BYOK route, if any.

    Pure with respect to routing inputs: the only external read is the local
    credential adapter's REFERENCE lookup, which returns no secret material.

    Args:
        backend: the backend the tier ladder resolved.
        db_path: the local credential database; defaults to the existing
            ``~/.omninode/delegation/delegation.sqlite``.

    Returns:
        The BYOK-substituted backend, or ``backend`` unchanged when no
        substitution applies. Never returns ``None`` -- a caller always has a
        route to attempt or a refusal to surface.
    """
    slug = house_provider_slug(backend.secret_ref)
    if slug is None:
        return backend

    # OMN-20157: the plan the customer's key was registered under selects the
    # product endpoint. A registration that recorded none resolves the
    # provider's default plan, which is what every earlier route meant. A
    # recorded plan the catalogue no longer declares resolves to nothing and
    # fails closed rather than falling back to another product's endpoint.
    plan = resolve_local_byok_credential_plan(slug, db_path=db_path)
    byok = resolve_byok_provider_backend(slug, plan=plan)
    if byok is None:
        # The provider is house-keyed but the catalogue does not offer it to
        # customers (``not_offered``, e.g. vertex), or the recorded plan is not
        # declared. Nothing to substitute; the house ref fails closed at the
        # secret boundary on a customer machine.
        return backend

    return (
        _registered_byok_route(
            byok,
            replaced=backend,
            tier=backend.tier,
            db_path=db_path,
        )
        or backend
    )


def _registered_byok_route(
    byok: ModelByokProviderBackend,
    *,
    replaced: ModelResolvedDelegationBackend | None,
    tier: str,
    db_path: Path | None,
) -> ModelResolvedDelegationBackend | None:
    """Build the route for ``byok`` on the customer's registered key, if there is one.

    ``None`` when the customer has registered no key for the provider: no route
    is minted, and no house credential answers in its place. ``replaced`` is the
    house rung this route stands in for, or ``None`` when the customer pinned the
    catalogue backend or the provider has no house rung at all.
    """
    slug = byok.provider
    customer_ref = resolve_local_byok_credential_ref(slug, db_path=db_path)
    if customer_ref is None:
        return None

    # OMN-20157: the model is the one the customer's KEY resolved from the
    # provider's own model list at registration, not an id pinned in the
    # catalogue and not the house rung's (which only says what OUR key can
    # use). A registration that could not resolve one carries the unresolved
    # marker, and the effect resolves it with the key before the call.
    model_id = (
        resolve_local_byok_credential_model(slug, db_path=db_path)
        or BYOK_MODEL_UNRESOLVED
    )

    # Logged values are taken from the CATALOGUE row, never from anything
    # derived from a ``secret_ref``. ``slug`` and ``customer_ref`` are both
    # safe to print by construction -- a provider slug is a public string and a
    # minted reference carries no secret material -- but both are taint-derived
    # from a field named ``secret_ref``, and a log line that a scanner cannot
    # prove clean is a log line that gets argued about on every future PR.
    # ``byok.provider`` and ``byok.backend_id`` are read out of
    # ``byok_provider_backends.v1.yaml`` and carry the same information.
    logger.info(
        "LocalByokRoute: routing provider=%s on the locally registered BYOK route %s",
        byok.provider,
        byok.backend_id,
    )
    max_tokens = byok.max_tokens or (replaced.max_tokens if replaced else None)
    timeout_ms = byok.timeout_ms or (replaced.timeout_ms if replaced else None)
    if max_tokens is None or timeout_ms is None:
        # A route with no house rung to inherit a budget from must declare its
        # own in the catalogue; there is no platform default to fall back to.
        raise RuntimeError(
            f"BYOK backend {byok.backend_id!r} declares no max_tokens/timeout_ms "
            "and no house rung supplies one; declare both in "
            "byok_provider_backends.v1.yaml."
        )
    return ModelResolvedDelegationBackend(
        backend_id=byok.backend_id,
        model_id=model_id,
        endpoint_ref=byok.endpoint_url,
        tier=tier,
        # The catalogue's budgets are the customer's, not the house rung's.
        max_tokens=max_tokens,
        timeout_ms=timeout_ms,
        extra_headers=dict(replaced.extra_headers) if replaced else {},
        # The whole point: the customer's minted reference, never the house one.
        secret_ref=customer_ref,
        # OMN-19765: name WHICH backend this substitution replaced, so a
        # caller who pinned that backend's id (e.g. ``cloud-glm``) can be told
        # their pin was honoured by this BYOK rung rather than escalated off.
        substituted_from_backend_id=replaced.backend_id if replaced else None,
        # OMN-16944 belt and braces. ``resolve_api_key_async`` drops this
        # unconditionally for a tenant-shaped ref, so the guarantee does not
        # rest on this line -- but declaring no env fallback means there is
        # nothing for a future call site to thread through either.
        api_key_env=None,
        model_id_source=(
            "provider model list via byok_provider_backends.v1.yaml "
            "model_preference (local BYOK credential registered for provider "
            f"{slug!r} plan {byok.plan!r})"
        ),
    )


class ByokKeyNotRegisteredError(RuntimeError):
    """The customer named a BYOK backend and has registered no key for it (OMN-17373).

    Names the provider and the one command that fixes it. Never a fallback: a
    customer's work is never answered on a house or lab model.
    """


def resolve_pinned_byok_route(
    backend_id: str,
    *,
    db_path: Path | None = None,
) -> ModelResolvedDelegationBackend | None:
    """Resolve a customer's ``--backend-id byok-<provider>`` pin from the catalogue.

    OMN-17373 defect 1. The bifrost config declares only platform rungs, so a
    catalogue backend id never resolved there. Returns ``None`` when
    ``backend_id`` is not a catalogue backend (the caller resolves it as a
    platform rung, exactly as before).

    Raises:
        ByokKeyNotRegisteredError: ``backend_id`` is a catalogue backend and the
            customer has registered no key, or one registered under a different
            plan of the provider, for it.
    """
    row = resolve_byok_backend_by_id(backend_id)
    if row is None or not row.customer_routable:
        return None
    plan = resolve_local_byok_credential_plan(row.provider, db_path=db_path)
    registered = resolve_byok_provider_backend(row.provider, plan=plan)
    route = (
        _registered_byok_route(row, replaced=None, tier=_BYOK_TIER, db_path=db_path)
        if registered is not None and registered.backend_id == row.backend_id
        else None
    )
    if route is None:
        raise ByokKeyNotRegisteredError(_missing_key_message(row.provider, backend_id))
    return route


def substitute_any_registered_byok_route(
    backend: ModelResolvedDelegationBackend,
    *,
    db_path: Path | None = None,
) -> ModelResolvedDelegationBackend:
    """Route a customer's work on a key for ANY catalogue provider (OMN-17373 defect 2).

    :func:`substitute_local_byok_route` only fires for a provider the platform
    holds a rung on. A provider declared ``mirrors_house_rung: false`` has none,
    so its customer's key could never route. This is the unpinned counterpart:
    when ``backend`` still carries a HOUSE ``secret_ref`` after that
    substitution (a platform credential the customer does not hold), the first
    provider in the catalogue the customer has a key for answers instead.

    A keyless local rung, and a rung already carrying a customer's own
    reference, are returned unchanged. With no registered key the rung is
    returned unchanged too and the caller's refusal names the missing key.
    """
    if house_provider_slug(backend.secret_ref) is None:
        return backend
    registered = set(registered_local_byok_providers(db_path=db_path))
    for provider in customer_provider_catalogue():
        if provider not in registered:
            continue
        plan = resolve_local_byok_credential_plan(provider, db_path=db_path)
        byok = resolve_byok_provider_backend(provider, plan=plan)
        if byok is None:
            continue
        route = _registered_byok_route(
            byok, replaced=backend, tier=backend.tier, db_path=db_path
        )
        if route is not None:
            return route
    return backend


#: The ladder tier a pinned BYOK route reports. A customer's provider model is
#: a metered cloud model, which is what the ``cheap_cloud`` tier prices.
_BYOK_TIER = "cheap_cloud"


def _missing_key_message(provider: str, backend_id: str) -> str:
    return (
        f"Backend {backend_id!r} runs on your own {provider} key and this machine "
        f"has no key registered for {provider}. Register one with "
        f"`onex secret set llm.{provider}.api_key`, then retry. Nothing else will "
        "answer for it: a customer's work never runs on a platform credential."
    )


__all__: list[str] = [
    "HOUSE_SECRET_REF_PATTERN",
    "ByokKeyNotRegisteredError",
    "house_provider_slug",
    "resolve_pinned_byok_route",
    "substitute_any_registered_byok_route",
    "substitute_local_byok_route",
]
