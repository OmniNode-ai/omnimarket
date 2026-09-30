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
      declares a plan and a limit model, ``vertex`` stays not-offered, and the
      ``glm`` default is the general API (the Coding Plan is detection-only)

Plans (OMN-20157)
-----------------
One provider can be reachable through more than one product with one request
shape: z.ai serves a Coding Plan (flat-rate quota) and a general pay-as-you-go
API on two endpoints. A row is therefore keyed ``(provider, plan)``.
:func:`resolve_byok_provider_backend` with no plan returns the provider's
``default_plan`` row. A named plan that the provider does not declare resolves
to ``None``, never to the default: silently answering a general-API key on the
Coding-Plan endpoint is a wrong-product refusal at best.

Detection-only plans (OMN-20157)
--------------------------------
z.ai's subscription terms (section 4) bar Coding Plan quota from "directly
invoking model APIs from your own applications, bots, websites, SaaS products or
other systems" and from being used by "customers or any organization" (quoted in
knowledge-base-internal ``reference/zai-glm-coding-plan-terms.md``). A customer's
Coding Plan key sent to our route is that, and the exposure (rate limiting, a
freeze, a ban after three violations) lands on the customer's account. So the
customer default for ``glm`` is ``general_api``, and the ``coding_plan`` row is
declared ``customer_routable: false``: it stays in the catalogue so plan
detection can recognise a Coding Plan key and so the quota model can describe
it, and :func:`resolve_byok_provider_backend` never returns it. A key or a named
plan that lands on such a row is refused with the row's typed ``refusal_code``
(:class:`ByokPlanNotPermittedError`), which tells the customer what to register
instead. Nothing ever routes a customer to that endpoint.

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
from decimal import Decimal
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


class ModelByokPricing(BaseModel):
    """The per-1M-token price of a row's model, when it differs from its tier's.

    A call is booked at its tier's rate unless its model declares a price here
    (OMN-20154): z.ai's general API serves ``glm-4.5-flash`` free inside a
    catalogue whose other entries are metered, and a zero-price model booked at a
    tier rate is a phantom paid delegation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_1m_usd: Decimal = Field(ge=0)
    output_per_1m_usd: Decimal = Field(ge=0)
    #: Where the price was read. Required: an unsourced zero reads like a fact.
    source: str = Field(min_length=1)


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
    #: OMN-20157. ``false`` declares a DETECTION-ONLY plan: the catalogue knows
    #: its endpoint so a key that belongs to it can be recognised, but no route,
    #: overlay row or registration may address it. The provider's terms bar it
    #: from third-party systems; the row's ``refusal_code`` and
    #: ``refusal_message`` say so to the customer.
    customer_routable: bool = True
    #: Required on a detection-only row, forbidden on a routable one: the typed
    #: code a caller refuses a key or a named plan with.
    refusal_code: str | None = Field(default=None, pattern=r"^BYOK_[A-Z][A-Z0-9_]*$")
    #: What the customer is told, including what to register instead.
    refusal_message: str | None = Field(default=None, min_length=1)
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
    #: OMN-20154. Declared only when the model's price is not its tier's.
    pricing: ModelByokPricing | None = None

    @model_validator(mode="after")
    def _a_detection_only_row_declares_its_refusal(self) -> ModelByokProviderBackend:
        if self.customer_routable:
            if self.refusal_code is not None or self.refusal_message is not None:
                raise ValueError(
                    "a customer_routable row may not declare a refusal_code or "
                    "refusal_message; only a detection-only row is refused"
                )
            return self
        if self.default_plan:
            raise ValueError(
                "a row with customer_routable false cannot be the default_plan: "
                "a caller that names no plan would be routed to it"
            )
        if self.refusal_code is None or self.refusal_message is None:
            raise ValueError(
                "a row with customer_routable false must declare its refusal_code "
                "and refusal_message, so a key that lands on it is refused with a "
                "typed reason and never silently"
            )
        return self


class ByokPlanNotPermittedError(ValueError):
    """A key or named plan belongs to a plan the provider's terms bar us from routing.

    OMN-20157. Raised at registration, before anything is stored or published,
    for a plan the catalogue declares ``customer_routable: false`` (z.ai's Coding
    Plan). A ``ValueError`` so the intake route treats it as the customer's input
    and not as a server fault. Carries the typed ``code`` and the customer-facing
    message; never the key.
    """

    def __init__(self, backend: ModelByokProviderBackend) -> None:
        self.code = backend.refusal_code or "BYOK_PLAN_NOT_PERMITTED"
        self.provider = backend.provider
        self.plan = backend.plan
        self.message = backend.refusal_message or ""
        super().__init__(f"{self.code}: {self.message}")


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
        if not any(row.customer_routable for row in rows):
            raise ByokCatalogError(
                f"BYOK provider catalog at {path} declares provider {provider!r} "
                "with no customer_routable plan; a provider a customer may "
                "register a key for needs at least one plan a route can address."
            )
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


def byok_routable_plans(provider: str) -> tuple[str, ...]:
    """The plans of ``provider`` a customer's key may be registered and routed under.

    OMN-20157. :func:`byok_provider_plans` lists every DECLARED plan, including a
    detection-only one; this lists the subset with ``customer_routable`` true,
    sorted. It is the list to offer a customer back ("register a key for one of
    these").
    """
    normalized = provider.strip().lower()
    return tuple(
        sorted(
            plan
            for (name, plan), row in load_byok_plan_catalog().items()
            if name == normalized and row.customer_routable
        )
    )


def resolve_byok_declared_plan(
    provider: str, plan: str
) -> ModelByokProviderBackend | None:
    """The declared row for ``(provider, plan)``, routable or not, or ``None``.

    OMN-20157. For plan detection, quota modelling and refusal, which all need to
    describe a detection-only plan. It is NOT a routing lookup: a route is
    resolved only by :func:`resolve_byok_provider_backend`, which never returns a
    detection-only row.
    """
    return load_byok_plan_catalog().get(
        (provider.strip().lower(), plan.strip().lower())
    )


def require_byok_plan_permitted(provider: str, plan: str) -> None:
    """Raise :class:`ByokPlanNotPermittedError` when ``plan`` is detection-only.

    A plan the catalogue does not declare at all returns quietly: that is the
    catalogue's other refusal (an unknown plan), made by the caller that lists
    the plans on offer. Call this at every point a plan is about to be filed or
    routed: intake, the local CLI and the credential adapter.
    """
    row = resolve_byok_declared_plan(provider, plan)
    if row is not None and not row.customer_routable:
        raise ByokPlanNotPermittedError(row)


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

    With ``plan=None`` the provider's ``default_plan`` row is returned. A plan
    declared ``customer_routable: false`` (OMN-20157, z.ai's Coding Plan) also
    resolves to ``None``: nothing ever routes a customer to it.

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
    row = load_byok_plan_catalog().get((normalized, plan.strip().lower()))
    # OMN-20157: a detection-only plan is never a route (the provider's terms
    # bar it from third-party systems). It resolves to nothing, like an
    # undeclared plan, and never widens to the default.
    return row if row is not None and row.customer_routable else None


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


def byok_declared_price_per_1m(
    endpoint_url: str | None, model_name: str
) -> tuple[Decimal, Decimal] | None:
    """Return the declared ``(input, output)`` USD per 1M tokens, or ``None``.

    Keyed on the endpoint and the model together, because a model id alone does
    not say which product answered (z.ai serves ``glm-4.5-flash`` free on the
    general API only). ``None`` means no price is declared and the caller books
    at the tier rate; it never means free.
    """
    if not endpoint_url:
        return None
    for backend in load_byok_plan_catalog().values():
        if (
            backend.pricing is not None
            and backend.endpoint_url == endpoint_url
            and backend.model_name == model_name
        ):
            return (
                backend.pricing.input_per_1m_usd,
                backend.pricing.output_per_1m_usd,
            )
    return None


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
    "ByokPlanNotPermittedError",
    "ModelByokLimitModel",
    "ModelByokLimitWindow",
    "ModelByokNotOfferedProvider",
    "ModelByokPricing",
    "ModelByokProviderBackend",
    "ModelCatalogueParityGap",
    "byok_backend_max_retries",
    "byok_declared_price_per_1m",
    "byok_limit_counter_key",
    "byok_provider_plans",
    "byok_routable_plans",
    "catalogue_parity_gap",
    "customer_provider_catalogue",
    "house_keyed_provider_slugs",
    "load_byok_not_offered_providers",
    "load_byok_plan_catalog",
    "load_byok_provider_catalog",
    "require_byok_plan_permitted",
    "resolve_byok_backend_by_id",
    "resolve_byok_declared_plan",
    "resolve_byok_provider_backend",
]
