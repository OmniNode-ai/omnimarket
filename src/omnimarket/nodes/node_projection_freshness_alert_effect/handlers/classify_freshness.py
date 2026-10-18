# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure decision: no I/O and no clock read (OMN-19513)."""

from datetime import datetime

from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    EnumFreshnessState,
    ModelFreshnessObservation,
    ModelFreshnessVerdict,
)


def classify_freshness(
    *,
    check_id: str,
    now: datetime,
    supply_newest_at: datetime | None,
    demand_distinct_active: int | None,
    max_age_seconds: int,
    min_distinct: int,
    error: str | None = None,
) -> ModelFreshnessVerdict:
    """Equality is fresh, future supply has age zero, and idle never alerts."""
    age: float | None = None
    if error is None:
        try:
            observation = ModelFreshnessObservation.model_validate(
                {
                    "supply_newest_at": supply_newest_at,
                    "demand_distinct_active": demand_distinct_active,
                }
            )
            if now.utcoffset() is None:
                raise ValueError("now must be timezone-aware")
            if observation.supply_newest_at is not None:
                age = max(0.0, (now - observation.supply_newest_at).total_seconds())
        except (ValueError, TypeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
    if error is not None:
        return ModelFreshnessVerdict(
            check_id=check_id,
            state=EnumFreshnessState.UNKNOWN,
            supply_newest_at=None,
            age_seconds=None,
            demand_distinct_active=None,
            error=error,
        )
    assert demand_distinct_active is not None
    if demand_distinct_active < min_distinct:
        state = EnumFreshnessState.IDLE
    elif age is not None and age <= max_age_seconds:
        state = EnumFreshnessState.FRESH
    else:
        state = EnumFreshnessState.STALE
    return ModelFreshnessVerdict(
        check_id=check_id,
        state=state,
        supply_newest_at=supply_newest_at,
        age_seconds=age,
        demand_distinct_active=demand_distinct_active,
    )
