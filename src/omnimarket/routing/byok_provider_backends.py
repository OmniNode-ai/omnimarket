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
    - OMN-17932: ``gemini``/``glm``/``vertex`` were declared not-offered; ``glm``
      was lifted 2026-09-06
    - OMN-20157: ``gemini`` and a second ``glm`` plan are offered, every row
      declares a plan and a limit model, ``vertex`` stays not-offered

Plans (OMN-20157)
-----------------
One provider can be reachable through more than one product with one request
shape: z.ai serves a Coding Plan (flat-rate quota) and a general pay-as-you-go
API on two endpoints. A row is therefore keyed ``(provider, plan)``.
:func:`resolve_byok_provider_backend` with no plan returns the provider's
``default_plan`` row, so every caller written before plans existed resolves
exactly what it resolved before. A named plan that the provider does not
declare resolves to ``None``, never to the default: silently answering a
general-API key on the Coding-Plan endpoint is a wrong-product refusal at best.

Limit model (OMN-20157)
-----------------------
Every row declares how its provider meters a credential, so quota tracking
counts the same way for every tenant. The lab is one tenant among the rest;
there is no separate lab path. :func:`byok_limit_counter_key` is the counter
identity: tenant, credential reference, provider, plan and, when the provider
meters per model, the model.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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


class ModelByokLimitWindow(BaseModel):
    """One metering window a provider applies to a credential.

    ``limit`` is nullable on purpose: ``None`` means the provider publishes no
    fixed cap, or the cap depends on an account tier this catalogue cannot
    know. ``limit_by_tier`` lists the published caps per tier, so the tier the
    customer actually holds (a fact about THEIR account) selects one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_hours: int = Field(gt=0)
    unit: Literal["requests", "prompts", "credits", "tokens"]
    limit: int | None = Field(default=None, gt=0)
    limit_by_tier: dict[str, int] = Field(default_factory=dict)
    #: Where the number (or its absence) comes from. Required: an unsourced
    #: cap is a guess that reads like a fact.
    source: str = Field(min_length=1)


class ModelByokLimitModel(BaseModel):
    """How a provider meters one credential on one plan (OMN-20157)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    billing: Literal["flat_rate_quota", "free_tier", "pay_as_you_go"]
    #: ``model`` counts per model id; ``plan`` pools every model on the plan
    #: into one counter (the GLM Coding Plan pools credits across models).
    counter_scope: Literal["model", "plan"]
    windows: tuple[ModelByokLimitWindow, ...] = ()

    @model_validator(mode="after")
    def _a_metered_plan_declares_a_window(self) -> ModelByokLimitModel:
        if self.billing != "pay_as_you_go" and not self.windows:
            raise ValueError(
                f"a {self.billing} limit model must declare at least one window; "
                "an unmetered flat-rate or free plan is not a thing the provider "
                "offers"
            )
        return self


class ModelByokProviderBackend(BaseModel):
    """One declared BYOK backend binding, keyed by ``(provider, plan)``.

    A 1:1 source for the writable columns of a
    ``delegation_routing_tenant_overlay`` row EXCEPT ``tenant_id``,
    ``task_type`` and ``secret_ref``, which are per-registration facts the
    caller supplies. There is deliberately no ``secret_ref`` here — see the
    module docstring.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    #: OMN-20157. The product the key belongs to on that provider. Lower-case
    #: token; a provider with one product declares one plan.
    plan: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    #: Exactly one row per multi-plan provider carries ``true``. A provider
    #: with a single row is its own default.
    default_plan: bool = False
    #: ``true`` when ``endpoint_url`` and ``model_name`` duplicate a live rung
    #: of ``bifrost_delegation.yaml`` (held honest by the parity gate). ``false``
    #: declares a customer-only surface the platform holds no key for and
    #: therefore has no rung on (the z.ai general API, OMN-6790).
    mirrors_house_rung: bool = True
    backend_id: str = Field(min_length=1)
    endpoint_url: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    #: OMN-18265. How many times a TRANSIENT provider failure on this
    #: customer-credentialed route may be re-issued to the same backend before
    #: the delegation terminalises. Required, not defaulted: a customer's chain
    #: has exactly one responder (no house credential may answer customer work,
    #: OMN-17082), so this number IS the whole recovery budget for them and a
    #: silent default would hide it. ``0`` is a legitimate declaration meaning
    #: "do not retry"; an absent field is a mis-shaped row and is refused.
    max_retries: int = Field(ge=0)
    timeout_ms: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    limit_model: ModelByokLimitModel


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


def _read_plan_catalog(
    path: Path,
) -> dict[tuple[str, str], ModelByokProviderBackend]:
    raw = _load_document(path)
    entries = raw.get("providers")
    if not isinstance(entries, list):
        raise ByokCatalogError(
            f"BYOK provider catalog at {path} has no 'providers' list."
        )

    catalog: dict[tuple[str, str], ModelByokProviderBackend] = {}
    backend_ids: set[str] = set()
    for entry in entries:
        # OMN-18265: the forbidden-provider refusal runs on the RAW row, before
        # shape validation. A Claude row must be refused for being Claude, not
        # incidentally for whichever field it also happens to be missing — and
        # adding a required field to the model above would otherwise silently
        # move which refusal a reader sees.
        if isinstance(entry, Mapping):
            raw_provider = entry.get("provider")
            if isinstance(raw_provider, str):
                _refuse_forbidden_provider(raw_provider, path, section="providers")
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
        key = (backend.provider, backend.plan)
        if key in catalog:
            raise ByokCatalogError(
                f"BYOK provider catalog at {path} declares provider "
                f"{backend.provider!r} plan {backend.plan!r} more than once; one "
                "provider and plan must resolve to exactly one backend."
            )
        if backend.backend_id in backend_ids:
            raise ByokCatalogError(
                f"BYOK provider catalog at {path} reuses backend_id "
                f"{backend.backend_id!r}; cost and tier accounting attribute a "
                "customer-paid call by backend_id, so two plans may not share one."
            )
        backend_ids.add(backend.backend_id)
        catalog[key] = backend

    by_provider: dict[str, list[ModelByokProviderBackend]] = {}
    for backend in catalog.values():
        by_provider.setdefault(backend.provider, []).append(backend)
    for provider, rows in by_provider.items():
        defaults = [row for row in rows if row.default_plan]
        if len(rows) > 1 and len(defaults) != 1:
            raise ByokCatalogError(
                f"BYOK provider catalog at {path} declares {len(rows)} plans for "
                f"provider {provider!r} with {len(defaults)} marked default_plan; "
                "exactly one must be, so a caller that names no plan resolves "
                "one route and never guesses."
            )
    return catalog


def _default_plan_rows(
    plans: Mapping[tuple[str, str], ModelByokProviderBackend],
) -> dict[str, ModelByokProviderBackend]:
    """The one row a caller that names no plan resolves, per provider."""
    by_provider: dict[str, list[ModelByokProviderBackend]] = {}
    for backend in plans.values():
        by_provider.setdefault(backend.provider, []).append(backend)
    return {
        provider: next((row for row in rows if row.default_plan), rows[0])
        for provider, rows in by_provider.items()
    }


def _read_catalog(path: Path) -> dict[str, ModelByokProviderBackend]:
    return _default_plan_rows(_read_plan_catalog(path))


@lru_cache(maxsize=1)
def load_byok_plan_catalog() -> dict[tuple[str, str], ModelByokProviderBackend]:
    """Load and cache every declared ``(provider, plan)`` row (OMN-20157).

    Same caching contract as :func:`load_byok_provider_catalog`; call
    ``load_byok_plan_catalog.cache_clear()`` in tests that rewrite the file.
    """
    return _read_plan_catalog(CATALOG_PATH)


@lru_cache(maxsize=1)
def load_byok_provider_catalog() -> dict[str, ModelByokProviderBackend]:
    """Load and cache the declared BYOK provider→default-plan-backend catalog.

    Cached for the process lifetime: the catalog ships inside the wheel
    (``[tool.hatch.build] artifacts`` packages ``src/omnimarket/**/*.yaml``)
    and cannot change under a running consumer. Call
    ``load_byok_provider_catalog.cache_clear()`` in tests that rewrite it.

    A provider with several plans maps to its ``default_plan`` row here; use
    :func:`load_byok_plan_catalog` for every plan.

    Raises:
        ByokCatalogError: the file is absent, is not a mapping, declares the
            wrong ``schema_version``, has no ``providers`` list, declares one
            provider and plan twice, or declares a multi-plan provider without
            exactly one default plan.
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


def byok_provider_plans(provider: str) -> tuple[str, ...]:
    """Every plan the catalogue declares for ``provider``, sorted (OMN-20157).

    Empty for a provider the catalogue does not offer. A single-plan provider
    returns its one plan.
    """
    normalized = provider.strip().lower()
    return tuple(
        sorted(plan for (name, plan) in load_byok_plan_catalog() if name == normalized)
    )


def resolve_byok_provider_backend(
    provider: str, plan: str | None = None
) -> ModelByokProviderBackend | None:
    """Resolve the declared BYOK backend for ``provider`` and ``plan``, or ``None``.

    ``None`` is the fail-CLOSED answer for an undeclared provider: the caller
    mints no routing overlay row, so a delegation for that tenant selects
    nothing rather than inheriting a platform backend and its house
    credential. It is equally the answer for a plan the provider does not
    declare: a named plan is never widened to the default, because a key for
    one product presented to another product's endpoint is refused there and
    reads as a billing failure (OMN-6790).

    With ``plan=None`` the provider's ``default_plan`` row is returned, which is
    what every caller written before plans existed resolved.

    Matching is exact on the provider string the customer submitted, lowercased
    and stripped. ``ModelInferenceCredentialCreateRequest.provider`` already
    constrains that string to ``^[A-Za-z0-9_-]+$``, so case is the only
    normalisation a legitimate submission can need. A plan is normalised the
    same way.
    """
    normalized = provider.strip().lower()
    if not normalized:
        return None
    if plan is None:
        return load_byok_provider_catalog().get(normalized)
    return load_byok_plan_catalog().get((normalized, plan.strip().lower()))


def resolve_byok_backend_by_id(
    backend_ref: str | None,
) -> ModelByokProviderBackend | None:
    """Return the catalogue row whose ``backend_id`` is ``backend_ref``, or ``None``.

    A tenant-overlay routing decision carries the catalogue ``backend_id`` as
    its ``selected_backend_ref``, so this is how the plan a credential was
    registered under is read back off an overlay row: the ``backend_id`` names
    the ``(provider, plan)`` pair (OMN-20157).
    """
    if not backend_ref:
        return None
    normalized = backend_ref.strip()
    if not normalized:
        return None
    for backend in load_byok_plan_catalog().values():
        if backend.backend_id == normalized:
            return backend
    return None


def byok_backend_max_retries(backend_ref: str | None) -> int | None:
    """Return the declared same-route retry budget for ``backend_ref``, or ``None``.

    Keyed on the catalogue's ``backend_id`` because that is what a tenant-overlay
    routing decision carries as ``selected_backend_ref``
    (``_decision_from_tenant_overlay``) — the provider string is not on the
    decision, and parsing it back out of the minted credential reference would
    be a guess where a declared key exists.

    ``None`` is the fail-CLOSED answer for a ref the catalogue does not declare:
    no budget is granted by default, so a route this file does not describe
    behaves exactly as it did before OMN-18265.
    """
    backend = resolve_byok_backend_by_id(backend_ref)
    return backend.max_retries if backend is not None else None


def byok_limit_counter_key(
    tenant_id: str, api_key_ref: str, backend: ModelByokProviderBackend
) -> tuple[str, str, str, str, str | None]:
    """The identity of the quota counter one credential's calls count against.

    ``(tenant_id, api_key_ref, provider, plan, model_name)``. ``model_name`` is
    ``None`` when the row meters per plan (``limit_model.counter_scope`` is
    ``plan``): every model on that plan draws on one pool, so keying on the
    model would split one quota into counters that each look healthy.

    The lab is a tenant like any other. Nothing here special-cases a house
    tenant, so our own keys are counted by the mechanism a customer's are.
    """
    model = backend.model_name if backend.limit_model.counter_scope == "model" else None
    return (tenant_id, api_key_ref, backend.provider, backend.plan, model)


__all__: list[str] = [
    "BYOK_CATALOG_SCHEMA_VERSION",
    "CATALOG_PATH",
    "FORBIDDEN_PROVIDER_PATTERN",
    "ByokCatalogError",
    "ModelByokLimitModel",
    "ModelByokLimitWindow",
    "ModelByokNotOfferedProvider",
    "ModelByokProviderBackend",
    "ModelCatalogueParityGap",
    "byok_backend_max_retries",
    "byok_limit_counter_key",
    "byok_provider_plans",
    "catalogue_parity_gap",
    "customer_provider_catalogue",
    "house_keyed_provider_slugs",
    "load_byok_not_offered_providers",
    "load_byok_plan_catalog",
    "load_byok_provider_catalog",
    "resolve_byok_backend_by_id",
    "resolve_byok_provider_backend",
]
