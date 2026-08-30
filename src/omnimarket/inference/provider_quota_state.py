# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enforcement half of the contract-declared provider quota policy (OMN-16932).

OMN-16891 built the classification: it reads a 429 against
``bifrost_delegation.yaml``'s ``provider_quota_policy`` and decides whether the
provider is throttling transiently, has spent a periodic cap that lifts at a
stated instant, or has a billing gap that no retry can clear. It computes a
``disabled_until``.

Nothing read it back. The only consumer of the verdict was a ``logger.warning``,
so ``disable_until_reset`` disabled nothing and the ladder kept escalating into
an exhausted provider — detection without enforcement (Operating Rule 5). On the
dev lane that produced the OMN-16932 loop: the quality gate's judge leg spent a
metered Gemini call per delegation until the free-tier cap (20 requests) was
gone, and every subsequent delegation then ALSO escalated into the same dead
provider, spending a second call to relearn the same 429.

This module is the missing read-back. It is deliberately small:

* **Keyed by quota DOMAIN, not by backend id.** ``cloud-glm-judge``,
  ``cloud-gemini-pro`` and ``cloud-gemini-flash`` are three backend ids that
  spend ONE Gemini free-tier counter under one key. A backend-id-keyed ledger
  would rediscover the same exhausted quota once per id. This is OMN-15503's
  principle ("tier names are policy slots, not failure domains") applied across
  workflows instead of within one. The domain is the policy's own
  ``provider_id`` when the endpoint matches a declared provider, and the
  endpoint host otherwise, so an undeclared host can still be recorded without
  colliding with a declared one.

* **Process-local and in-memory.** The judge effect and the routing authority
  both run in ``omninode-runtime`` (verified on the dev lane 2026-08-29: 24
  ``HandlerRoutingIntent resolved`` and 12 ``judge-adequacy LLM call failed``
  lines in the same container), so a process-local ledger is enough to close
  the loop between them. Nothing here is durable truth — it is a cache of an
  observation the provider itself will re-assert on the next call, so losing it
  on restart costs at most one extra 429, never a wrong routing decision.

* **Self-lifting.** A cap is a cooldown, not a ban. Entries expire at the
  provider's stated reset (or the contract's fallback cooldown), and the rung
  returns to the ladder on its own with no operator action. Only
  ``disable_until_billing`` — which by definition has no reset — persists until
  a human funds the account.

Fail direction is inherited from the classifier: an unrecognised code stays
``retryable`` and is never recorded here, so this can only ever remove a
provider that DECLARED itself unusable, never one that merely looked slow.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.inference.provider_quota_policy import (
    ModelQuotaVerdict,
    load_provider_quota_policy,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumQuotaDisposition,
)


class ModelQuotaDisabledState(BaseModel):
    """A provider quota domain currently barred from routing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quota_domain: str = Field(
        ..., description="Declared provider_id, or the endpoint host when undeclared."
    )
    disposition: EnumQuotaDisposition = Field(...)
    disabled_until: datetime | None = Field(
        default=None,
        description="Instant the domain returns to the ladder; None never lifts.",
    )
    provider_code: str | None = Field(default=None)
    reason: str = Field(default="")
    recorded_at: datetime = Field(...)


_LOCK = threading.Lock()
_DISABLED: dict[str, ModelQuotaDisabledState] = {}


def quota_domain_for_endpoint(endpoint_url: str) -> str | None:
    """Return the quota failure domain an endpoint belongs to.

    Prefers the contract-declared ``provider_id`` so every backend sharing one
    provider's counter shares one ledger key. Falls back to the endpoint host
    (prefixed, so it can never collide with a declared provider_id) when the
    policy does not describe the host. Returns ``None`` for a URL with no host.
    """
    host = (urlparse(endpoint_url).hostname or "").lower()
    if not host:
        return None
    try:
        policy = load_provider_quota_policy()
    except (OSError, ValueError):
        # The policy is a hard requirement of the CLASSIFIER, but this resolver
        # is also called on the read path for every routing eligibility check.
        # Degrading to host-keying here keeps routing working; a genuinely
        # missing policy still fails loud where it is loaded for classification.
        return f"host:{host}"
    for provider in policy.providers:
        match = provider.match_endpoint_host.lower()
        if host == match or host.endswith(f".{match}"):
            return provider.provider_id
    return f"host:{host}"


def record_quota_verdict(*, endpoint_url: str, verdict: ModelQuotaVerdict) -> None:
    """Fold a classified quota verdict into the routing-visible ledger.

    A ``retryable`` verdict records nothing: an ordinary throttle clears by
    itself and stranding a live tier on one is the failure this module's
    asymmetry exists to avoid. A non-retryable verdict bars the whole quota
    domain until its stated reset.
    """
    if verdict.retryable:
        return
    domain = verdict.provider_id or quota_domain_for_endpoint(endpoint_url)
    if domain is None:
        return
    state = ModelQuotaDisabledState(
        quota_domain=domain,
        disposition=verdict.disposition,
        disabled_until=verdict.disabled_until,
        provider_code=verdict.provider_code,
        reason=verdict.reason,
        recorded_at=datetime.now(UTC),
    )
    with _LOCK:
        existing = _DISABLED.get(domain)
        # Keep the LATER reset when two calls race: the provider has told us the
        # domain is unusable for at least that long, and shortening the window
        # would send the next delegation straight back into the same 429.
        if (
            existing is not None
            and existing.disabled_until is None
            and state.disabled_until is not None
        ):
            return
        if (
            existing is not None
            and existing.disabled_until is not None
            and state.disabled_until is not None
            and existing.disabled_until > state.disabled_until
        ):
            return
        _DISABLED[domain] = state


def quota_domain_disabled(
    endpoint_url: str, *, now: datetime | None = None
) -> ModelQuotaDisabledState | None:
    """Return the active disable for an endpoint's quota domain, or ``None``.

    Expired entries are dropped as they are read, so a lifted cap needs no
    sweeper and the rung returns to the ladder on the next routing decision.
    """
    domain = quota_domain_for_endpoint(endpoint_url)
    if domain is None:
        return None
    reference = now or datetime.now(UTC)
    with _LOCK:
        state = _DISABLED.get(domain)
        if state is None:
            return None
        if state.disabled_until is not None and reference >= state.disabled_until:
            del _DISABLED[domain]
            return None
        return state


def clear_provider_quota_state() -> None:
    """Drop every recorded disable. For tests and lane-refresh boundaries."""
    with _LOCK:
        _DISABLED.clear()


__all__ = [
    "ModelQuotaDisabledState",
    "clear_provider_quota_state",
    "quota_domain_disabled",
    "quota_domain_for_endpoint",
    "record_quota_verdict",
]
