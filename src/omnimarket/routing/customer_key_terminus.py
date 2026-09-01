# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The customer-path delegation terminus — a typed refusal, never a house key.

OMN-17082, reshaped by the operator ruling of 2026-08-31: **there are no
keyless customers, and no house credential ever executes customer work.**

Two surfaces, two honest termini
--------------------------------

``EnumDelegationSurface`` names WHICH entry point is running, because the
correct terminus differs:

* ``CLOUD`` — the deployed multi-tenant runtime. A customer with no
  registered provider key gets a typed refusal
  (:class:`CustomerKeyRefusedError`) naming what to do about it. Not the
  house key, and not OmniNode's own GPUs either: we do not offer inference,
  so "free local" is not a customer-facing fallback here.
* ``CUSTOMER_LOCAL`` — the customer's own machine, via the CLI. An
  uncredentialed local backend IS the honest terminus there: no OmniNode
  credential and no OmniNode compute is involved, so there is nothing to
  pool. A house-credentialed backend is refused on this surface too — running
  on the customer's laptop does not make OmniNode's provider account theirs.

The surface is a property of the code path, not of configuration. It is
threaded in as a pure input (mirroring ``roi_overlay`` / ``tenant_overlay``)
by the entry point that knows the answer, and it defaults to ``CLOUD``
everywhere so an unmigrated call site inherits the STRICT posture. Fail-closed
is the whole point: the failure mode this module exists to prevent is a path
that quietly treats a customer as house.

Why this is not already covered by OMN-16944
--------------------------------------------

OMN-16944 made a *tenant-minted* ``secret_ref``
(``cred_<tenant>_<provider>_<uuid4hex>``) fail closed at the resolver choke
point, dropping ``env_var_fallback`` so a missing tenant value can never be
satisfied by ``LLM_GLM_API_KEY``. That guard is keyed on the ref's SHAPE.

A customer with **no key at all** never produces a ref of that shape, so the
guard is structurally unreachable on precisely the path that matters. The
routing authority resolved the platform ladder for them and the effect
boundary authenticated with the house key — silently, successfully, on
OmniNode's provider account. OMN-16944 closed the narrow half; this module
closes the terminus.

What counts as a house credential
---------------------------------

Not a guessed prefix and not an allowlist: the set is DERIVED from the
shipped bifrost contract via :func:`house_credential_refs` — every
``api_key_ref`` and every ``api_key_env`` any platform backend declares. A
new house-credentialed backend is covered the day it lands, with nothing to
remember to update. Both fields matter: ``api_key_env`` is a genuine
additional fallback the effect boundary resolves (OMN-13943), so a decision
carrying only ``api_key_env`` still executes on a house key.

A customer-owned credential is anything else that is not in that set — the
minted ``cred_…`` shape (:mod:`omnimarket.tenant_credential_ref`) and the
per-tenant dotted refs an overlay row may carry. The dangerous direction is
under-claiming the house set, which is why it is derived rather than typed
out.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG, HOUSE_TENANT_UUID

# The customer-facing error code. A stable, machine-readable contract: gateway
# and CLI surfaces route on this string, so changing it is a breaking API
# change and ``tests/test_omn17082_customer_key_terminus.py`` pins it.
CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE: Final[
    Literal["delegation.customer_provider_key.absent"]
] = "delegation.customer_provider_key.absent"

# Actionable remediation carried on every refusal. A terminus that refuses
# without saying what would fix it is the dishonest terminus in a different
# costume — the customer cannot tell "you have no key" from "we are broken".
CUSTOMER_KEY_REMEDIATION: Final[str] = (
    "Register a provider key for this tenant (POST "
    "/v1/tenants/me/inference-credentials) and retry. OmniNode does not offer "
    "inference and never runs customer work on a platform credential."
)


class EnumDelegationSurface(StrEnum):
    """Which entry point is resolving this delegation.

    ``CLOUD`` is the default everywhere it is threaded, so an unmigrated or
    future call site gets the strict posture rather than the lax one.
    """

    CLOUD = "cloud"
    CUSTOMER_LOCAL = "customer_local"


class EnumCustomerKeyRefusalReason(StrEnum):
    """Why a customer's delegation terminated in a refusal."""

    # The customer has registered no provider key and the surface offers no
    # credential-free terminus. Actionable: register a key.
    NO_PROVIDER_KEY_REGISTERED = "no_provider_key_registered"
    # A route was resolved, but executing it would authenticate the customer's
    # work with an OmniNode platform credential. Not actionable by the
    # customer — this is a routing/overlay misconfiguration on our side.
    HOUSE_CREDENTIAL_ON_CUSTOMER_PATH = "house_credential_on_customer_path"


class ModelCustomerKeyRefusal(BaseModel):
    """The typed, customer-facing refusal payload.

    Carries only reference NAMES, never secret values — ``api_key_ref`` /
    ``api_key_env`` are safe to log, publish and display by construction (see
    :mod:`omnimarket.tenant_credential_ref`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_code: Literal["delegation.customer_provider_key.absent"] = Field(
        default=CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE,
        description="Stable machine-readable code customer surfaces route on.",
    )
    reason: EnumCustomerKeyRefusalReason = Field(
        description="Which of the two refusal conditions fired."
    )
    tenant_id: str = Field(
        description="The customer this delegation was attributed to."
    )
    task_type: str = Field(description="Task class that was being routed.")
    surface: EnumDelegationSurface = Field(
        description="Entry point whose terminus rules were applied."
    )
    correlation_id: UUID = Field(description="Correlation id of the refused request.")
    attempted_api_key_ref: str | None = Field(
        default=None,
        description=(
            "The credential REFERENCE the refused route would have used, when "
            "a route was resolved at all. Never a secret value."
        ),
    )
    attempted_api_key_env: str | None = Field(
        default=None,
        description="The env-var NAME the refused route would have fallen back to.",
    )
    attempted_backend_ref: str | None = Field(
        default=None, description="Backend the refused route had selected."
    )
    remediation: str = Field(
        default=CUSTOMER_KEY_REMEDIATION,
        description="What the customer must do to make this delegation routable.",
    )

    @property
    def message(self) -> str:
        """A single human-readable line, safe to surface verbatim."""
        return (
            f"Delegation refused for tenant {self.tenant_id!r} "
            f"(task_type={self.task_type!r}, surface={self.surface.value}): "
            f"{self.reason.value}. {self.remediation}"
        )


class CustomerKeyRefusedError(Exception):
    """Raised instead of returning a route a customer must not execute.

    Raising rather than returning a sentinel is deliberate: every existing
    caller of the routing authority treats a returned decision as executable,
    so a sentinel would need every one of them to remember to check it. An
    exception cannot be ignored into a house-key call.
    """

    def __init__(self, refusal: ModelCustomerKeyRefusal) -> None:
        self.refusal = refusal
        super().__init__(refusal.message)

    @classmethod
    def for_missing_key(
        cls,
        *,
        tenant_id: str,
        task_type: str,
        correlation_id: UUID,
        surface: EnumDelegationSurface,
    ) -> CustomerKeyRefusedError:
        """Build the no-registered-key refusal."""
        return cls(
            ModelCustomerKeyRefusal(
                reason=EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED,
                tenant_id=tenant_id,
                task_type=task_type,
                surface=surface,
                correlation_id=correlation_id,
            )
        )


def is_customer_attributed(tenant_id: str | None) -> bool:
    """Return whether this delegation belongs to a CUSTOMER rather than to us.

    Reuses the existing house-tenant authority
    (:mod:`omnimarket.projection.tenant_isolation`) rather than minting a
    second notion of "is this ours" — the same predicate
    ``resolve_tenant_overlay`` already short-circuits on. Both representations
    of the one house identity are recognised (ADR-0027: slug on the wire, UUID
    canonically), because a request that arrives carrying the UUID form is
    still OmniNode's own work.

    Blank/whitespace is NOT customer-attributed: it is the untenanted platform
    path, which legitimately runs on the house key.
    """
    if tenant_id is None:
        return False
    normalized = tenant_id.strip()
    if not normalized:
        return False
    return normalized not in {HOUSE_TENANT_SLUG, str(HOUSE_TENANT_UUID)}


def house_credential_refs(backends: Mapping[str, object]) -> frozenset[str]:
    """Derive the set of platform credential names from resolved backends.

    Every ``api_key_ref`` and every ``api_key_env`` a platform backend
    declares. Both, because ``api_key_env`` is a genuine ADDITIONAL fallback
    the effect boundary resolves (OMN-13943) — a decision carrying only
    ``api_key_env`` still executes on a house key, so treating it as
    uncredentialed would reopen exactly this defect.

    Derived, never hardcoded: a house-credentialed backend added to the
    bifrost contract is covered the day it lands.
    """
    names: set[str] = set()
    for backend in backends.values():
        for attribute in ("api_key_ref", "api_key_env"):
            value = getattr(backend, attribute, None)
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
    return frozenset(names)


def refuse_keyless_customer_on_cloud(
    *,
    tenant_id: str | None,
    task_type: str,
    correlation_id: UUID,
    surface: EnumDelegationSurface,
    has_customer_credential: bool,
) -> None:
    """Refuse BEFORE the platform ladder runs, when there is nothing to route.

    A customer on the cloud with no registered credential has no routable
    backend at all — the platform ladder's rungs are OmniNode's own keys and
    OmniNode's own GPUs. Refusing here rather than after tier iteration is
    what makes the terminus honest: the customer is told "register a provider
    key", not "no tier has a configured endpoint for this task type", which
    is true of our config and false of their problem.
    """
    if surface is not EnumDelegationSurface.CLOUD:
        return
    if has_customer_credential:
        return
    if not is_customer_attributed(tenant_id):
        return
    assert tenant_id is not None  # narrowed by is_customer_attributed
    raise CustomerKeyRefusedError.for_missing_key(
        tenant_id=tenant_id.strip(),
        task_type=task_type,
        correlation_id=correlation_id,
        surface=surface,
    )


def enforce_customer_key_terminus(
    *,
    tenant_id: str | None,
    task_type: str,
    correlation_id: UUID,
    surface: EnumDelegationSurface,
    api_key_ref: str | None,
    api_key_env: str | None,
    backend_ref: str | None,
    house_refs: Iterable[str],
    customer_declared_backend: bool = False,
) -> None:
    """Refuse a resolved route that a customer must not execute.

    Applied to every route the routing authority is about to hand back, on
    both the platform-ladder and tenant-overlay paths. The overlay path needs
    it just as much: the overlay table is writable DATA, and a row whose
    ``secret_ref`` names a platform credential is house pooling by
    configuration — and it slips past the OMN-16944 minted-ref guard precisely
    because that ref is not tenant-shaped.

    Non-customer (house/untenanted) work is returned untouched: OmniNode's own
    workloads running on OmniNode's own key is not pooling.

    ``customer_declared_backend`` says the route came from the customer's OWN
    overlay row rather than from the platform ladder. It changes exactly one
    thing: what an ABSENT credential means. On the platform ladder, no
    credential means an OmniNode GPU, which we do not sell — refused on the
    cloud. On a customer-declared backend it means the customer pointed us at
    an endpoint of theirs that needs no auth, which is their infrastructure
    and their cost, so it is routable on every surface. It never softens the
    house-credential check: an overlay row naming a platform credential is
    refused either way.

    Raises:
        CustomerKeyRefusedError: When the route would authenticate customer
            work with a platform credential, or when the surface offers no
            credential-free terminus and the customer registered no key.
    """
    if not is_customer_attributed(tenant_id):
        return
    assert tenant_id is not None  # narrowed by is_customer_attributed
    normalized_tenant = tenant_id.strip()
    house = frozenset(house_refs)

    ref = api_key_ref.strip() if isinstance(api_key_ref, str) and api_key_ref else None
    env = api_key_env.strip() if isinstance(api_key_env, str) and api_key_env else None

    if (ref is not None and ref in house) or (env is not None and env in house):
        raise CustomerKeyRefusedError(
            ModelCustomerKeyRefusal(
                reason=(EnumCustomerKeyRefusalReason.HOUSE_CREDENTIAL_ON_CUSTOMER_PATH),
                tenant_id=normalized_tenant,
                task_type=task_type,
                surface=surface,
                correlation_id=correlation_id,
                attempted_api_key_ref=ref,
                attempted_api_key_env=env,
                attempted_backend_ref=backend_ref,
            )
        )

    if ref is None and env is None:
        # No credential at all. Three different facts wear this same shape:
        #   * the customer's own auth-free endpoint (overlay row, no
        #     secret_ref) — their infrastructure, their cost, routable
        #     anywhere;
        #   * a free local backend on the customer's own machine — nothing of
        #     ours is involved, so this is the honest CLI terminus;
        #   * a free local backend in OUR cloud — that is an OmniNode GPU, and
        #     we do not sell inference, so it is a refusal.
        if customer_declared_backend:
            return
        if surface is EnumDelegationSurface.CUSTOMER_LOCAL:
            return
        raise CustomerKeyRefusedError(
            ModelCustomerKeyRefusal(
                reason=EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED,
                tenant_id=normalized_tenant,
                task_type=task_type,
                surface=surface,
                correlation_id=correlation_id,
                attempted_backend_ref=backend_ref,
            )
        )

    # A credential that is neither a platform credential nor absent is the
    # customer's own — the minted ``cred_…`` shape or a per-tenant overlay
    # ref. Routable on every surface.
    return


__all__: list[str] = [
    "CUSTOMER_KEY_REMEDIATION",
    "CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE",
    "CustomerKeyRefusedError",
    "EnumCustomerKeyRefusalReason",
    "EnumDelegationSurface",
    "ModelCustomerKeyRefusal",
    "enforce_customer_key_terminus",
    "house_credential_refs",
    "is_customer_attributed",
    "refuse_keyless_customer_on_cloud",
]
