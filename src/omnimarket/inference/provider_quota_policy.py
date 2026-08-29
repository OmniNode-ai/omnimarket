# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared classification of provider quota responses (OMN-16891).

A 429 is not one failure class, and treating it as one is what makes a
quota-capped tier expensive:

* a **periodic cap** (z.ai code ``1310``) lifts at a stated instant. Retrying
  before then is a guaranteed failure, so the tier should be disabled until the
  reset and then allowed back into the ladder on its own.
* a **billing/entitlement gap** (z.ai code ``1113``) has no reset at all. It
  clears only when a human funds the account, so retries are pure waste and the
  correct output is an operator alert.
* an **ordinary throttle** (the OpenRouter free tier) clears by itself and
  should stay retryable.

Which code means which is DECLARED in ``bifrost_delegation.yaml``'s
``provider_quota_policy`` block, not encoded here: adding a provider or
recoding a disposition is an overlay edit (OMN-13215), and this module only
knows how to *apply* a policy it is handed.

Failure direction is asymmetric on purpose. An UNKNOWN code stays retryable —
over-disabling on an unrecognised code would strand a live tier on a transient
throttle. But a KNOWN cap whose reset instant cannot be parsed still disables,
falling back to the contract's cooldown: a cap we cannot time is still a cap.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field

# OMN-16891: the config SHAPES live with the other bifrost wire DTOs — the
# bifrost loader validates the whole contract with extra="forbid", so a second
# private definition here would be a second source of truth that could drift
# out of the schema the loader enforces. This module owns the LOGIC only.
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumQuotaDisposition,
    ModelProviderQuotaPolicy,
    ModelQuotaCodeRule,
    ModelQuotaProviderRule,
    ModelSaturationPolicy,
    ModelTierSaturationRule,
)

_CONTRACT_FILENAME = "bifrost_delegation.yaml"
_POLICY_KEY = "provider_quota_policy"

# z.ai states the reset inline: "Your limit will reset at 2026-08-30 20:32:52".
_RESET_PATTERN = re.compile(
    r"reset\s+at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", re.IGNORECASE
)


class ModelQuotaVerdict(BaseModel):
    """The decision for one quota response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: EnumQuotaDisposition = Field(...)
    provider_id: str | None = Field(default=None)
    provider_code: str | None = Field(default=None)
    disabled_until: datetime | None = Field(
        default=None,
        description="Instant the tier may be retried; None when no reset exists.",
    )
    alert: bool = Field(default=False)
    reason: str = Field(default="")

    @property
    def retryable(self) -> bool:
        """Whether the tier may be retried now."""
        return self.disposition is EnumQuotaDisposition.RETRYABLE


def _provider_for(
    policy: ModelProviderQuotaPolicy, endpoint_url: str
) -> ModelQuotaProviderRule | None:
    """Match a provider rule by the endpoint's host.

    Subdomains match their declared parent so a regional or versioned host does
    not silently fall through to the default disposition.
    """
    host = (urlparse(endpoint_url).hostname or "").lower()
    if not host:
        return None
    for provider in policy.providers:
        match = provider.match_endpoint_host.lower()
        if host == match or host.endswith(f".{match}"):
            return provider
    return None


def _rule_for(
    provider: ModelQuotaProviderRule, code: str | None
) -> ModelQuotaCodeRule | None:
    if code is None:
        return None
    for rule in provider.codes:
        if rule.code == code:
            return rule
    return None


def max_wait_ms_for(policy: ModelSaturationPolicy, tier_name: str) -> int:
    """Milliseconds to wait for capacity in ``tier_name`` before escalating."""
    for rule in policy.tiers:
        if rule.tier == tier_name:
            return rule.max_wait_ms
    return policy.default_max_wait_ms


def saturation_rule_for(
    policy: ModelSaturationPolicy, tier_name: str
) -> ModelTierSaturationRule | None:
    """Return the declared bounded-wait rule for ``tier_name``, if any."""
    for rule in policy.tiers:
        if rule.tier == tier_name:
            return rule
    return None


def _contract_path() -> Path:
    return Path(__file__).resolve().parents[1] / "configs" / _CONTRACT_FILENAME


@lru_cache(maxsize=1)
def load_provider_quota_policy() -> ModelProviderQuotaPolicy:
    """Load the committed ``provider_quota_policy`` block.

    Fails loud (Rule 8): a missing or malformed block is a configuration error,
    not a reason to silently fall back to "everything is retryable" — that is
    precisely the behaviour this module exists to replace.
    """
    path = _contract_path()
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    block = raw.get(_POLICY_KEY)
    if not block:
        raise ValueError(
            f"{path} declares no {_POLICY_KEY!r} block; provider quota "
            "responses cannot be classified without one."
        )
    return ModelProviderQuotaPolicy.model_validate(block)


def clear_provider_quota_policy_cache() -> None:
    """Drop the cached policy after a contract change in tests."""
    load_provider_quota_policy.cache_clear()


def _extract_error_code(body: dict[str, Any] | None) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return None if code is None else str(code)


def _extract_error_message(body: dict[str, Any] | None) -> str:
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if not isinstance(error, dict):
        return ""
    return str(error.get("message") or "")


def _parse_reset_instant(message: str) -> datetime | None:
    """Parse the reset instant the provider states inline.

    The provider reports a naive wall-clock stamp with no offset. It is read as
    UTC so the value is comparable and unambiguous; the cost of being wrong is
    bounded either way (a slightly early retry re-trips the same 429, a
    slightly late one waits a little longer).
    """
    match = _RESET_PATTERN.search(message)
    if not match:
        return None
    stamp = match.group(1).replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def classify_quota_response(
    *,
    status_code: int,
    endpoint_url: str,
    body: dict[str, Any] | None,
    policy: ModelProviderQuotaPolicy | None = None,
    now: datetime | None = None,
) -> ModelQuotaVerdict | None:
    """Classify one provider response as a quota verdict.

    Returns ``None`` when the response carries no quota semantics (any status
    other than 429), so callers keep their existing handling for ordinary
    transport and server failures.
    """
    if status_code != 429:
        return None

    active = policy if policy is not None else load_provider_quota_policy()
    provider = _provider_for(active, endpoint_url)
    code = _extract_error_code(body)

    if provider is None:
        return ModelQuotaVerdict(
            disposition=active.default_disposition,
            provider_code=code,
            reason=(
                "429 from a provider the quota policy does not describe; "
                "treated as an ordinary throttle"
            ),
        )

    rule = _rule_for(provider, code)
    if rule is None:
        return ModelQuotaVerdict(
            disposition=active.default_disposition,
            provider_id=provider.provider_id,
            provider_code=code,
            reason=(
                f"429 from {provider.provider_id} with unmapped code {code!r}; "
                "treated as an ordinary throttle rather than disabling a "
                "possibly-healthy tier"
            ),
        )

    if rule.disposition is EnumQuotaDisposition.DISABLE_UNTIL_BILLING:
        return ModelQuotaVerdict(
            disposition=rule.disposition,
            provider_id=provider.provider_id,
            provider_code=rule.code,
            disabled_until=None,
            alert=rule.alert,
            reason=(
                f"{provider.provider_id} code {rule.code}: no balance or resource "
                "package. No reset will arrive on its own — retrying cannot "
                "succeed, so the tier is disabled and an operator is alerted."
            ),
        )

    if rule.disposition is EnumQuotaDisposition.DISABLE_UNTIL_RESET:
        reference = now or datetime.now(UTC)
        disabled_until = None
        if rule.reset_from == "message_reset_timestamp":
            disabled_until = _parse_reset_instant(_extract_error_message(body))
        if disabled_until is None:
            # Fail CLOSED: a cap we cannot time is still a cap.
            disabled_until = reference + timedelta(
                seconds=rule.fallback_cooldown_seconds
            )
            detail = (
                "reset instant not present in the provider message; applying the "
                f"contract cooldown of {rule.fallback_cooldown_seconds}s"
            )
        else:
            detail = f"provider-stated reset at {disabled_until.isoformat()}"
        return ModelQuotaVerdict(
            disposition=rule.disposition,
            provider_id=provider.provider_id,
            provider_code=rule.code,
            disabled_until=disabled_until,
            alert=rule.alert,
            reason=(
                f"{provider.provider_id} code {rule.code}: periodic limit "
                f"exhausted; {detail}"
            ),
        )

    return ModelQuotaVerdict(
        disposition=rule.disposition,
        provider_id=provider.provider_id,
        provider_code=rule.code,
        alert=rule.alert,
        reason=f"{provider.provider_id} code {rule.code}: declared retryable",
    )


__all__: list[str] = [
    "EnumQuotaDisposition",
    "ModelProviderQuotaPolicy",
    "ModelQuotaCodeRule",
    "ModelQuotaProviderRule",
    "ModelQuotaVerdict",
    "classify_quota_response",
    "clear_provider_quota_policy_cache",
    "load_provider_quota_policy",
]


# --------------------------------------------------------------------------
# Saturation policy (OMN-16891)
# --------------------------------------------------------------------------
#
# Two failure modes bound the correct behaviour when owned capacity is busy:
#   * never queue on local indefinitely — a saturated local rung must be able
#     to hand work to a free cloud rung rather than stall the ladder;
#   * never skip straight to cloud while local is idle — that spends money (or
#     free-tier goodwill) on capacity we already own.
#
# Bounded-wait-then-escalate resolves both. The bound is DECLARED per tier in
# ``bifrost_delegation.yaml``; a tier with no entry escalates immediately,
# because there is no owned capacity worth waiting for.
#
# There is deliberately no queue-depth metric behind this. `.201:8000` exposes
# no queue counters (probed 2026-08-28: no `/metrics` `queue`, `running_req`,
# or `num_queue`), so saturation is observed as ELAPSED WAIT at dispatch rather
# than read from a signal that does not exist — which is also what keeps this
# from becoming a new metrics subsystem.

_SATURATION_KEY = "saturation_policy"


@lru_cache(maxsize=1)
def load_saturation_policy() -> ModelSaturationPolicy:
    """Load the committed ``saturation_policy`` block. Fails loud (Rule 8)."""
    path = _contract_path()
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    block = raw.get(_SATURATION_KEY)
    if not block:
        raise ValueError(
            f"{path} declares no {_SATURATION_KEY!r} block; the bounded-wait "
            "budget cannot be resolved without one."
        )
    return ModelSaturationPolicy.model_validate(block)


def clear_saturation_policy_cache() -> None:
    """Drop the cached saturation policy after a contract change in tests."""
    load_saturation_policy.cache_clear()


__all__ += [
    "ModelSaturationPolicy",
    "ModelTierSaturationRule",
    "clear_saturation_policy_cache",
    "load_saturation_policy",
    "max_wait_ms_for",
    "saturation_rule_for",
]
