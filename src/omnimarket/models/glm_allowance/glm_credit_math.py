# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Credit and window arithmetic of the GLM Coding Plan allowance (OMN-20287).

Pure functions over the policy the deployment supplied. Nothing here reads a
clock, a file or the environment: ``now`` is an argument.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from omnimarket.models.glm_allowance.model_glm_allowance_policy import (
    ModelGlmAllowancePolicy,
    ModelGlmCreditRate,
)
from omnimarket.models.glm_allowance.model_glm_usage_record import ModelGlmUsageRecord

_WEEK = timedelta(days=7)


def rate_for_model(
    policy: ModelGlmAllowancePolicy, model: str
) -> tuple[ModelGlmCreditRate, bool]:
    """The rate of a model, and whether the policy rates it.

    A model the policy does not rate is charged at the dearest declared rate,
    so an unrated model can only make the allowance look smaller.
    """
    for rate in policy.credit_rates:
        if rate.model == model:
            return rate, True
    dearest = max(
        policy.credit_rates,
        key=lambda r: (r.output_rate, r.input_rate, r.cached_input_rate),
    )
    return dearest, False


def peak_factor(policy: ModelGlmAllowancePolicy, at: datetime) -> float:
    """1.0 inside the peak window, the off-peak factor outside it."""
    peak = policy.peak
    if peak is None:
        return 1.0
    local = at.astimezone(timezone(timedelta(hours=peak.utc_offset_hours)))
    if (
        local.weekday() in peak.weekdays
        and peak.start_hour <= local.hour < peak.end_hour
    ):
        return 1.0
    return peak.off_peak_factor


def credits_for_usage(
    policy: ModelGlmAllowancePolicy, record: ModelGlmUsageRecord
) -> float:
    """Credits one call spent: the recorded figure, else tokens x rate x peak factor."""
    if record.credits is not None:
        return record.credits
    rate, _ = rate_for_model(policy, record.model)
    raw = (
        record.input_tokens * rate.input_rate
        + record.cached_input_tokens * rate.cached_input_rate
        + record.output_tokens * rate.output_rate
    ) / policy.credit_divisor
    return raw * peak_factor(policy, record.started_at)


def unrated_models(
    policy: ModelGlmAllowancePolicy, usage: Iterable[ModelGlmUsageRecord]
) -> tuple[str, ...]:
    """Models in the usage the policy has no rate for, sorted."""
    return tuple(
        sorted(
            {
                u.model
                for u in usage
                if u.credits is None and not rate_for_model(policy, u.model)[1]
            }
        )
    )


def window_start(policy: ModelGlmAllowancePolicy, now: datetime) -> datetime:
    """Start of the rolling window that ends at ``now``."""
    return now - timedelta(hours=policy.window_hours)


def week_start(policy: ModelGlmAllowancePolicy, now: datetime) -> datetime:
    """Start of the fixed weekly period containing ``now``."""
    weeks = (now - policy.week_anchor) // _WEEK
    return policy.week_anchor + weeks * _WEEK


def week_reset(policy: ModelGlmAllowancePolicy, now: datetime) -> datetime:
    """When the weekly counter next resets."""
    return week_start(policy, now) + _WEEK
