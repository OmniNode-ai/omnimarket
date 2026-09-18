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

Nothing here reads a secret VALUE. The catalogue supplies the endpoint, model
and budgets; the local credential adapter supplies the REFERENCE; the value is
resolved at the effect boundary and nowhere else.

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
    resolve_local_byok_credential_ref,
)
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend
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

    byok = resolve_byok_provider_backend(slug)
    if byok is None:
        # The provider is house-keyed but the catalogue does not offer it to
        # customers (``not_offered``, e.g. vertex). Nothing to substitute; the
        # house ref fails closed at the secret boundary on a customer machine.
        return backend

    customer_ref = resolve_local_byok_credential_ref(slug, db_path=db_path)
    if customer_ref is None:
        return backend

    logger.info(
        "LocalByokRoute: substituting house rung %s (provider=%s) with the "
        "locally registered BYOK route %s",
        backend.backend_id,
        slug,
        byok.backend_id,
    )
    return ModelResolvedDelegationBackend(
        backend_id=byok.backend_id,
        model_id=byok.model_name,
        endpoint_ref=byok.endpoint_url,
        tier=backend.tier,
        # The catalogue's budgets are the customer's, not the house rung's.
        # Both are declared on every catalogue row; the ``or`` arms are the
        # documented "fall back to the platform default" semantics the overlay
        # row already has for its own nullable columns.
        max_tokens=byok.max_tokens or backend.max_tokens,
        timeout_ms=byok.timeout_ms or backend.timeout_ms,
        extra_headers=dict(backend.extra_headers),
        # The whole point: the customer's minted reference, never the house one.
        secret_ref=customer_ref,
        # OMN-16944 belt and braces. ``resolve_api_key_async`` drops this
        # unconditionally for a tenant-shaped ref, so the guarantee does not
        # rest on this line -- but declaring no env fallback means there is
        # nothing for a future call site to thread through either.
        api_key_env=None,
        model_id_source=(
            "byok_provider_backends.v1.yaml "
            f"(local BYOK credential registered for provider {slug!r})"
        ),
    )


__all__: list[str] = [
    "HOUSE_SECRET_REF_PATTERN",
    "house_provider_slug",
    "substitute_local_byok_route",
]
