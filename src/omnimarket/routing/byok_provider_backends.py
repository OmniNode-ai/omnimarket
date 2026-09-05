# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provider→backend catalog for customer-supplied (BYOK) inference keys.

Loads ``configs/byok_provider_backends.v1.yaml`` — the declared answer to the
one question the BYOK routing bridge has to ask: *a customer registered a key
for provider ``X``; which backend does a delegation on that key address?*

Why this is a catalog lookup and not a derivation
-------------------------------------------------
The platform's own OpenRouter rungs in ``bifrost_delegation.yaml`` each carry
``secret_ref: llm.openrouter.api_key`` — a HOUSE credential. Deriving a
customer's route by "reuse the platform backend whose endpoint host matches
the provider" would inherit that ref by construction, and answering a customer
on a house credential is precisely what OMN-17372 ruling 3 forbids. So the
BYOK binding is declared separately and the ``secret_ref`` is supplied by the
caller from the tenant's OWN minted ref, never read from this file. This
module has no ``secret_ref`` field at all — the omission is the mechanism.

Fail-closed
-----------
:func:`resolve_byok_provider_backend` returns ``None`` for any provider the
catalog does not declare. ``None`` means *no route is minted* — the credential
is still catalogued (the customer can see they registered it), but no
``delegation_routing_tenant_overlay`` row exists, so nothing selects it. It
must never fall back to a platform backend.

The catalogue is exactly the handler-backed set (OMN-17353)
-----------------------------------------------------------
:func:`customer_provider_catalogue` is the single customer-facing authority for
*which* providers a customer may bring a key for. It is pinned in BOTH
directions against the platform contract by
``tests/test_omn17353_provider_catalogue.py``: every house-keyed rung in
``bifrost_delegation.yaml`` must be offered here or declared ``not_offered``
here (with a reason and a ticket), and every row here must be backed by a rung.
Nothing is inferred from a backend's endpoint host — the binding is declared.
Claude/Anthropic is refused as a provider id anywhere in the file
(:data:`FORBIDDEN_PROVIDER_PATTERN`): Claude is never a delegation target.

Related:
    - OMN-17372: cloud delegation on a customer's OpenRouter key (blocker b3)
    - OMN-17353: the catalogue equals the handler-backed set, both directions
    - OMN-15631: the ``delegation_routing_tenant_overlay`` table + resolver
    - OMN-17373: ``openai`` is deliberately absent — it has no backend yet
    - OMN-17932: ``gemini``/``glm``/``vertex`` are declared not-offered
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

#: Schema tag the catalog file must declare. A file that does not carry it is
#: refused rather than partially read — a silently-mis-shaped routing catalog
#: is worse than an absent one.
BYOK_CATALOG_SCHEMA_VERSION = "byok_provider_backends.v1"

CATALOG_PATH: Path = (
    Path(__file__).parent.parent / "configs" / "byok_provider_backends.v1.yaml"
)

#: Provider ids that may never appear in the catalogue, offered or not. The
#: launch rule (beta requirements r4 §2.4, axiom 2) is that Claude is never a
#: delegation target — not a default, not a fallback, not an accepted
#: credential type, not a catalogue row. Matched case-insensitively as a
#: substring so ``us.anthropic.opus`` and ``Claude-3`` are both refused.
FORBIDDEN_PROVIDER_PATTERN: re.Pattern[str] = re.compile(
    r"anthropic|claude", re.IGNORECASE
)

#: The shape of a HOUSE credential reference on a platform rung. The middle
#: segment is the provider slug the customer submits (``llm.openrouter.api_key``
#: -> ``openrouter``). :func:`house_keyed_provider_slugs` derives the
#: handler-backed set from it and fails closed on any other shape.
_HOUSE_SECRET_REF_PATTERN: re.Pattern[str] = re.compile(
    r"^llm\.(?P<slug>[a-z0-9_-]+)\.[a-z0-9_]+$"
)


class ByokCatalogError(ValueError):
    """The BYOK provider catalog is absent or mis-shaped.

    Raised rather than degraded-to-empty: an empty catalog and a broken
    catalog are indistinguishable to the caller, and the second one silently
    stops minting routes for every customer at once.
    """


class ModelByokProviderBackend(BaseModel):
    """One declared BYOK backend binding, keyed by the customer's provider id.

    A 1:1 source for the writable columns of a
    ``delegation_routing_tenant_overlay`` row EXCEPT ``tenant_id``,
    ``task_type`` and ``secret_ref``, which are per-registration facts the
    caller supplies. There is deliberately no ``secret_ref`` here — see the
    module docstring.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    endpoint_url: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    timeout_ms: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)


class ModelByokNotOfferedProvider(BaseModel):
    """A house-keyed platform provider the catalogue deliberately does NOT offer.

    The reverse half of the OMN-17353 parity gate: a rung the platform ships a
    house key for must be offered to customers or declared not-offered here,
    with the reason and the ticket that owns lifting it. Silence is a failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    ticket: str = Field(pattern=r"^OMN-[0-9]+$")


class ModelCatalogueParityGap(BaseModel):
    """What the customer catalogue and the platform contract disagree on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: House-keyed rung slugs that are neither offered nor declared not-offered.
    missing_from_catalogue: tuple[str, ...]
    #: Catalogue rows (offered or not-offered) that no house-keyed rung backs.
    unbacked_in_catalogue: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not self.missing_from_catalogue and not self.unbacked_in_catalogue


def _refuse_forbidden_provider(provider: str, path: Path, *, section: str) -> None:
    if FORBIDDEN_PROVIDER_PATTERN.search(provider):
        raise ByokCatalogError(
            f"BYOK provider catalog at {path} names provider {provider!r} in "
            f"'{section}'. Claude/Anthropic is never a delegation target and may "
            "not appear in the customer catalogue in any role."
        )


def _load_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ByokCatalogError(
            f"BYOK provider catalog not found at {path}. Every customer-supplied "
            "inference key resolves its route through this file; without it no "
            "BYOK delegation can be routed at all."
        )
    with open(path) as handle:
        raw: Any = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ByokCatalogError(f"BYOK provider catalog at {path} is not a mapping.")

    declared_version = raw.get("schema_version")
    if declared_version != BYOK_CATALOG_SCHEMA_VERSION:
        raise ByokCatalogError(
            f"BYOK provider catalog at {path} declares schema_version "
            f"{declared_version!r}; expected {BYOK_CATALOG_SCHEMA_VERSION!r}."
        )
    return raw


def _read_catalog(path: Path) -> dict[str, ModelByokProviderBackend]:
    raw = _load_document(path)
    entries = raw.get("providers")
    if not isinstance(entries, list):
        raise ByokCatalogError(
            f"BYOK provider catalog at {path} has no 'providers' list."
        )

    catalog: dict[str, ModelByokProviderBackend] = {}
    for entry in entries:
        try:
            backend = ModelByokProviderBackend.model_validate(entry)
        except ValidationError as exc:
            # ``extra="forbid"`` is what makes a house ``secret_ref`` on a
            # customer row impossible; surface it as a catalogue error so the
            # refusal is one exception class regardless of which field broke.
            raise ByokCatalogError(
                f"BYOK provider catalog at {path} has a mis-shaped 'providers' "
                f"row: {exc}"
            ) from exc
        _refuse_forbidden_provider(backend.provider, path, section="providers")
        if backend.provider in catalog:
            raise ByokCatalogError(
                f"BYOK provider catalog at {path} declares provider "
                f"{backend.provider!r} more than once; one provider must resolve "
                "to exactly one backend."
            )
        catalog[backend.provider] = backend
    return catalog


@lru_cache(maxsize=1)
def load_byok_provider_catalog() -> dict[str, ModelByokProviderBackend]:
    """Load and cache the declared BYOK provider→backend catalog.

    Cached for the process lifetime: the catalog ships inside the wheel
    (``[tool.hatch.build] artifacts`` packages ``src/omnimarket/**/*.yaml``)
    and cannot change under a running consumer. Call
    ``load_byok_provider_catalog.cache_clear()`` in tests that rewrite it.

    Raises:
        ByokCatalogError: the file is absent, is not a mapping, declares the
            wrong ``schema_version``, has no ``providers`` list, or declares
            one provider twice.
    """
    return _read_catalog(CATALOG_PATH)


def _read_not_offered(path: Path) -> dict[str, ModelByokNotOfferedProvider]:
    raw = _load_document(path)
    entries = raw.get("not_offered", [])
    if not isinstance(entries, list):
        raise ByokCatalogError(
            f"BYOK provider catalog at {path} has a 'not_offered' key that is "
            "not a list."
        )
    offered = _read_catalog(path)
    declined: dict[str, ModelByokNotOfferedProvider] = {}
    for entry in entries:
        try:
            row = ModelByokNotOfferedProvider.model_validate(entry)
        except ValidationError as exc:
            raise ByokCatalogError(
                f"BYOK provider catalog at {path} has a mis-shaped 'not_offered' "
                f"row: {exc}"
            ) from exc
        _refuse_forbidden_provider(row.provider, path, section="not_offered")
        normalized = row.provider.strip().lower()
        if normalized in offered:
            raise ByokCatalogError(
                f"BYOK provider catalog at {path} declares provider "
                f"{row.provider!r} as both offered and not_offered."
            )
        if normalized in declined:
            raise ByokCatalogError(
                f"BYOK provider catalog at {path} declares not_offered provider "
                f"{row.provider!r} more than once."
            )
        declined[normalized] = row
    return declined


@lru_cache(maxsize=1)
def load_byok_not_offered_providers() -> dict[str, ModelByokNotOfferedProvider]:
    """Load and cache the declared not-offered house-keyed providers.

    Same caching contract as :func:`load_byok_provider_catalog`; call
    ``load_byok_not_offered_providers.cache_clear()`` in tests that rewrite it.
    """
    return _read_not_offered(CATALOG_PATH)


def customer_provider_catalogue() -> tuple[str, ...]:
    """The customer-facing provider catalogue: every provider id a customer
    may register a key for, sorted.

    This is the ONLY authority for that set. Intake surfaces (the onex-api
    ``POST /v1/tenants/me/inference-credentials`` route) validate the
    submitted ``provider`` against it and refuse anything else with a typed
    error naming the allowed set; the projection writer mints a route only
    for a provider in it.
    """
    return tuple(sorted(load_byok_provider_catalog()))


def house_keyed_provider_slugs(backends: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Derive the handler-backed provider set from platform rung mappings.

    A rung that carries a house ``secret_ref`` is a provider the platform
    ships a handler and a credential path for. Its slug is the middle segment
    of ``llm.<provider>.<field>``. Keyless local rungs are not BYOK-shaped and
    contribute nothing. Any other ``secret_ref`` shape raises: the slug
    convention is the mechanism, so an unrecognised shape must fail closed
    rather than silently drop a provider from the parity check.
    """
    slugs: set[str] = set()
    for backend in backends:
        secret_ref = backend.get("secret_ref")
        if secret_ref is None:
            continue
        match = _HOUSE_SECRET_REF_PATTERN.fullmatch(str(secret_ref))
        if match is None:
            raise ByokCatalogError(
                f"platform rung {backend.get('backend_id')!r} carries secret_ref "
                f"{secret_ref!r}, which is not of the llm.<provider>.<field> "
                "shape the BYOK catalogue parity gate derives provider slugs from."
            )
        slugs.add(match.group("slug"))
    return frozenset(slugs)


def catalogue_parity_gap(
    platform_providers: Iterable[str],
    *,
    offered: Iterable[str],
    not_offered: Iterable[str],
) -> ModelCatalogueParityGap:
    """Compare the handler-backed set with the catalogue, both directions.

    Pure: takes already-derived provider sets so the runtime module never
    reads ``bifrost_delegation.yaml`` itself (the projection writer must not
    import the bifrost loader — see the catalogue file header).
    """
    platform = frozenset(platform_providers)
    declared = frozenset(offered) | frozenset(not_offered)
    return ModelCatalogueParityGap(
        missing_from_catalogue=tuple(sorted(platform - declared)),
        unbacked_in_catalogue=tuple(sorted(declared - platform)),
    )


def resolve_byok_provider_backend(provider: str) -> ModelByokProviderBackend | None:
    """Resolve the declared BYOK backend for ``provider``, or ``None``.

    ``None`` is the fail-CLOSED answer for an undeclared provider: the caller
    mints no routing overlay row, so a delegation for that tenant selects
    nothing rather than inheriting a platform backend and its house
    credential.

    Matching is exact on the provider string the customer submitted, lowercased
    and stripped. ``ModelInferenceCredentialCreateRequest.provider`` already
    constrains that string to ``^[A-Za-z0-9_-]+$``, so case is the only
    normalisation a legitimate submission can need.
    """
    normalized = provider.strip().lower()
    if not normalized:
        return None
    return load_byok_provider_catalog().get(normalized)


__all__: list[str] = [
    "BYOK_CATALOG_SCHEMA_VERSION",
    "CATALOG_PATH",
    "FORBIDDEN_PROVIDER_PATTERN",
    "ByokCatalogError",
    "ModelByokNotOfferedProvider",
    "ModelByokProviderBackend",
    "ModelCatalogueParityGap",
    "catalogue_parity_gap",
    "customer_provider_catalogue",
    "house_keyed_provider_slugs",
    "load_byok_not_offered_providers",
    "load_byok_provider_catalog",
    "resolve_byok_provider_backend",
]
