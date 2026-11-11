# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Paid hosted rungs on the delegation ladder: placement, spending cap, burn (OMN-23756).

The routing overlay's ``paid_rungs`` section declares hosted models ordinary
routing may select, behind one switch. This module is where the routing
authority reads it:

* :func:`apply_paid_rungs` places the paid tiers into the loaded ladder (and
  appends rungs that join an existing metered tier), refusing a rung whose
  tier's cost model is ``free_local`` so a paid call is never booked at zero;
* :func:`with_paid_tiers` inserts the paid tiers into a class's closed tier
  order after ``after_tier``, the same insertion the registered customer-key
  tier uses;
* :func:`paid_rung_gate_reason` is checked before a paid tier is offered: it
  reads the provider's own usage accounting and closes the paid tiers, with a
  typed reason, once the spend since switch-on reaches the cap or the reading
  cannot be taken (fail closed);
* :func:`build_paid_rung_burn_reading` answers "how fast is the credit going".

It lives outside the reducer node because it reads the network and a secret
reference, which the node-purity check refuses inside a reducer (the same
reason :mod:`omnimarket.handlers.routing.paid_gate` lives here).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from omnibase_core.models.delegation.wire import EnumTierCostType
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.enums.enum_routing_exclusion import EnumRoutingExclusionReason
from omnimarket.handlers.routing.routing_tiers_path import (
    load_delegation_routing_overlay,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.models.delegation.model_paid_rung_burn import (
    ModelPaidRungBurnReading,
    ModelPaidRungCallRecord,
    ModelPaidRungModelBurn,
    ModelProviderCreditsReading,
)
from omnimarket.models.delegation.model_paid_rungs_overlay import (
    ModelPaidRungsOverlay,
    ModelPaidRungSpendSource,
)
from omnimarket.models.delegation.wire import (
    ModelDelegationConfig,
    ModelRoutingTier,
)

logger = logging.getLogger(__name__)

#: How long one credits reading answers the cap check before it is read again.
CREDITS_READING_TTL_SECONDS = 60.0
_CREDITS_TIMEOUT_SECONDS = 10.0

CreditsReader = Callable[[ModelPaidRungSpendSource], ModelProviderCreditsReading]


def read_provider_credits(
    source: ModelPaidRungSpendSource,
) -> ModelProviderCreditsReading:
    """Read total credits and total usage from the provider's credits endpoint.

    The key is resolved by reference inside this process through the canonical
    store reader and sent only in the request header; it is never logged.
    Raises on any transport, credential or shape failure.
    """
    key = resolve_api_key_loop_safe(source.secret_ref)
    if key is None:
        msg = f"secret_ref {source.secret_ref!r} resolved to nothing"
        raise ValueError(msg)
    with httpx.Client(timeout=_CREDITS_TIMEOUT_SECONDS) as client:
        response = client.get(
            source.credits_url,
            headers={"Authorization": f"Bearer {key.get_secret_value()}"},
        )
    response.raise_for_status()
    data = response.json().get("data") or {}
    return ModelProviderCreditsReading(
        total_credits_usd=Decimal(str(data["total_credits"])),
        total_usage_usd=Decimal(str(data["total_usage"])),
        read_at=datetime.now(UTC),
    )


_credits_reader: CreditsReader = read_provider_credits
_cached: (
    tuple[ModelPaidRungSpendSource, float, ModelProviderCreditsReading | None] | None
) = None


def set_credits_reader(reader: CreditsReader | None) -> None:
    """Replace the credits reader (tests, a dry run); ``None`` restores the HTTP reader."""
    global _credits_reader, _cached
    _credits_reader = reader if reader is not None else read_provider_credits
    _cached = None


def current_credits(
    source: ModelPaidRungSpendSource,
) -> ModelProviderCreditsReading | None:
    """The credits reading, at most :data:`CREDITS_READING_TTL_SECONDS` old, or ``None``.

    A failed read is remembered for the same window, so a provider outage closes
    the paid tiers without one request per routing decision.
    """
    global _cached
    now = time.monotonic()
    if (
        _cached is not None
        and _cached[0] == source
        and now - _cached[1] < CREDITS_READING_TTL_SECONDS
    ):
        return _cached[2]
    try:
        reading: ModelProviderCreditsReading | None = _credits_reader(source)
    except Exception as exc:  # fail closed on any failure to read the spend
        logger.warning(
            "paid rungs: provider credits unreadable at %s (%s); paid tiers closed",
            source.credits_url,
            type(exc).__name__,
        )
        reading = None
    _cached = (source, now, reading)
    return reading


def spent_since_switch_on(
    paid: ModelPaidRungsOverlay, reading: ModelProviderCreditsReading
) -> Decimal | None:
    """Provider usage now minus the baseline read at switch-on, or ``None``."""
    if paid.usage_baseline_usd is None:
        return None
    return max(reading.total_usage_usd - paid.usage_baseline_usd, Decimal(0))


def paid_rung_gate_reason(
    tier: ModelRoutingTier,
    overlay_paid: ModelPaidRungsOverlay | None = None,
) -> EnumRoutingExclusionReason | None:
    """Why a paid tier may not be offered right now, or ``None`` when it may.

    Only the overlay's own paid tiers are gated; every other tier returns
    ``None``. A spend that cannot be read closes the paid tiers (fail closed),
    as does a spend since switch-on at or above ``spend_cap_usd``.
    """
    paid = (
        overlay_paid
        if overlay_paid is not None
        else load_delegation_routing_overlay().paid_rungs
    )
    if paid is None or not paid.enabled or tier.name not in paid.tier_names():
        return None
    reading = current_credits(paid.spend_source)
    if reading is None:
        return EnumRoutingExclusionReason.TIER_PAID_RUNG_SPEND_UNREADABLE
    spent = spent_since_switch_on(paid, reading)
    if spent is None:
        return EnumRoutingExclusionReason.TIER_PAID_RUNG_SPEND_UNREADABLE
    if spent >= paid.spend_cap_usd:
        return EnumRoutingExclusionReason.TIER_PAID_RUNG_SPEND_CAP_REACHED
    return None


def _refuse(reason: str) -> ProtocolConfigurationError:
    return ProtocolConfigurationError(
        f"DELEGATION_ROUTING_OVERLAY_PATH paid_rungs: {reason} (OMN-23756)."
    )


def _is_free(tier: ModelRoutingTier) -> bool:
    if tier.cost is not None:
        return tier.cost.cost_type is EnumTierCostType.FREE_LOCAL
    return tier.cost_per_1k_tokens == 0.0


def apply_paid_rungs(
    config: ModelDelegationConfig,
    paid: ModelPaidRungsOverlay | None,
) -> ModelDelegationConfig:
    """Return ``config`` with the paid tiers placed after ``after_tier``.

    Returns ``config`` itself when there are no paid rungs or the switch is off.
    A rung whose tier is neither a paid tier nor a ladder tier, a rung whose
    ladder tier's cost model is ``free_local``, a paid tier name that collides
    with a ladder tier, and an ``after_tier`` missing from the ladder are each
    refused with :class:`ProtocolConfigurationError`.
    """
    if paid is None or not paid.enabled:
        return config
    ladder = {tier.name: tier for tier in config.tiers}
    paid_names = paid.tier_names()
    collisions = sorted(paid_names & ladder.keys())
    if collisions:
        raise _refuse(f"paid tier name(s) {collisions} collide with ladder tiers")
    if paid.after_tier not in ladder:
        raise _refuse(
            f"after_tier {paid.after_tier!r} is not in the routing ladder "
            f"(tiers: {sorted(ladder)})"
        )
    joined: dict[str, ModelRoutingTier] = {}
    for rung in paid.rungs:
        if rung.tier in paid_names:
            continue
        target = ladder.get(rung.tier)
        if target is None:
            raise _refuse(
                f"rung {rung.backend_id!r} names tier {rung.tier!r}, which is "
                f"neither a paid tier ({sorted(paid_names)}) nor a ladder tier"
            )
        if _is_free(target):
            raise _refuse(
                f"rung {rung.backend_id!r} names tier {rung.tier!r} whose cost "
                "model is free_local; a paid rung there would be booked at zero"
            )
        if rung.tier not in joined:
            joined[rung.tier] = target.model_copy(
                update={"models": (*target.models, *paid.ladder_rungs_for(rung.tier))}
            )
    tiers: list[ModelRoutingTier] = []
    for tier in config.tiers:
        tiers.append(joined.get(tier.name, tier))
        if tier.name == paid.after_tier:
            tiers.extend(paid.routing_tiers())
    return config.model_copy(update={"tiers": tuple(tiers)})


def with_paid_tiers(
    tiers: tuple[ModelRoutingTier, ...],
    config: ModelDelegationConfig,
    paid: ModelPaidRungsOverlay | None,
) -> tuple[ModelRoutingTier, ...]:
    """Insert the placed paid tiers into a class order after ``after_tier``.

    The paid tiers come from ``config`` (where :func:`apply_paid_rungs` placed
    them), so a class order and the ladder carry the same tier objects. A class
    order without ``after_tier`` is left as it is, and an order that already
    carries the paid tiers is returned unchanged.
    """
    if paid is None or not paid.enabled:
        return tiers
    names = tuple(tier.name for tier in tiers)
    paid_names = paid.tier_names()
    if paid.after_tier not in names or paid_names & set(names):
        return tiers
    placed = tuple(tier for tier in config.tiers if tier.name in paid_names)
    position = names.index(paid.after_tier) + 1
    return (*tiers[:position], *placed, *tiers[position:])


def without_paid_tiers(
    tiers: tuple[ModelRoutingTier, ...],
    paid: ModelPaidRungsOverlay | None,
) -> tuple[ModelRoutingTier, ...]:
    """``tiers`` with the overlay's paid tiers removed (the packaged order)."""
    if paid is None:
        return tiers
    names = paid.tier_names()
    return tuple(tier for tier in tiers if tier.name not in names)


def build_paid_rung_burn_reading(
    paid: ModelPaidRungsOverlay,
    credits: ModelProviderCreditsReading,
    calls: Sequence[ModelPaidRungCallRecord],
    *,
    now: datetime | None = None,
) -> ModelPaidRungBurnReading:
    """How fast the credit is going, from one credits reading and the call records.

    Spend since switch-on is the provider's own figure (usage now minus the
    baseline read at switch-on), so it counts every call on the key, including
    eval-arm runs. Dollars per hour divide that by the hours since
    ``enabled_at``; the projections divide what is left (of the balance, and of
    the cap) by that rate. Per-model calls and tokens count the call records at
    or after ``enabled_at``.
    """
    read_at = now or credits.read_at
    spent = spent_since_switch_on(paid, credits)
    rate: Decimal | None = None
    if spent is not None and paid.enabled_at is not None:
        hours = Decimal((read_at - paid.enabled_at).total_seconds()) / Decimal(3600)
        if hours > 0:
            rate = spent / hours
    balance = credits.balance_usd

    def _hours_left(left: Decimal) -> Decimal | None:
        if rate is None or rate <= 0:
            return None
        return max(left, Decimal(0)) / rate

    window = [
        c for c in calls if paid.enabled_at is None or c.completed_at >= paid.enabled_at
    ]
    per_model: dict[str, list[ModelPaidRungCallRecord]] = {}
    for call in window:
        per_model.setdefault(call.model_id, []).append(call)
    rows = []
    for model_id in sorted(per_model):
        group = per_model[model_id]
        reported = [c.provider_reported_cost_usd for c in group]
        rows.append(
            ModelPaidRungModelBurn(
                model_id=model_id,
                calls=len(group),
                tokens_in=sum(c.tokens_in for c in group),
                tokens_out=sum(c.tokens_out for c in group),
                booked_cost_usd=sum((c.booked_cost_usd for c in group), Decimal(0)),
                provider_reported_cost_usd=(
                    sum((r for r in reported if r is not None), Decimal(0))
                    if all(r is not None for r in reported)
                    else None
                ),
            )
        )
    return ModelPaidRungBurnReading(
        read_at=read_at,
        enabled=paid.enabled,
        enabled_at=paid.enabled_at,
        balance_usd=balance,
        total_credits_usd=credits.total_credits_usd,
        total_usage_usd=credits.total_usage_usd,
        spend_cap_usd=paid.spend_cap_usd,
        spent_since_switch_on_usd=spent,
        usd_per_hour=rate,
        hours_until_balance_gone=_hours_left(balance),
        hours_until_cap_reached=(
            _hours_left(paid.spend_cap_usd - spent) if spent is not None else None
        ),
        per_model=tuple(rows),
    )


__all__: list[str] = [
    "CREDITS_READING_TTL_SECONDS",
    "apply_paid_rungs",
    "build_paid_rung_burn_reading",
    "current_credits",
    "paid_rung_gate_reason",
    "read_provider_credits",
    "set_credits_reader",
    "spent_since_switch_on",
    "with_paid_tiers",
    "without_paid_tiers",
]
