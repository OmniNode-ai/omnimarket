# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One verdict per Claude token-pool label from its measured usage (OMN-23607).

Pure: each label's two usage windows, the overlay's thresholds and a clock in, a
verdict (ok, drain, limited, unknown) per label out. A reading that is missing or older than
the policy's bound is unknown and never ok; a window that reset since it was read is not counted
and never drawn as zero use. The drain list is empty unless one label is ok, so the pool is
never drained empty.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_pool_usage_verdict_compute.models import (
    ModelPoolLabelReading,
    ModelPoolLabelUsage,
    ModelPoolUsagePolicy,
    ModelPoolUsageRequest,
    ModelPoolUsageResult,
    ModelPoolUsageWindowPolicy,
    ModelPoolWindowUsage,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.models.model_pool_usage_result import (
    Reason,
    Verdict,
)

_VERDICTS = ("ok", "drain", "limited", "unknown")
_RANK: dict[str, int] = {"ok": 0, "drain": 1, "limited": 2}
_REASON: dict[tuple[str, str], Reason] = {
    ("five_hour", "limited"): "five_hour_limited",
    ("weekly", "limited"): "weekly_limited",
    ("five_hour", "drain"): "five_hour_drain",
    ("weekly", "drain"): "weekly_drain",
}


def _seconds(until: datetime, now: datetime) -> int:
    return max(0, int((until - now).total_seconds()))


def _window(
    used: float | None,
    resets_at: datetime | None,
    bounds: ModelPoolUsageWindowPolicy,
    now: datetime,
) -> ModelPoolWindowUsage:
    to_reset = _seconds(resets_at, now) if resets_at is not None else None
    if resets_at is not None and resets_at <= now:
        return ModelPoolWindowUsage(
            fraction_used=None, seconds_to_reset=0, state="reset_passed"
        )
    if used is None:
        return ModelPoolWindowUsage(
            fraction_used=None, seconds_to_reset=to_reset, state="unread"
        )
    if used >= bounds.limited_at:
        state = "limited"
    elif used >= bounds.drain_at:
        state = "drain"
    else:
        state = "ok"
    return ModelPoolWindowUsage(
        fraction_used=used, seconds_to_reset=to_reset, state=state
    )


def _label_usage(
    reading: ModelPoolLabelReading, policy: ModelPoolUsagePolicy, now: datetime
) -> ModelPoolLabelUsage:
    five = _window(
        reading.five_hour_utilization,
        reading.five_hour_resets_at,
        policy.five_hour,
        now,
    )
    week = _window(
        reading.seven_day_utilization,
        reading.seven_day_resets_at,
        policy.weekly,
        now,
    )

    def verdict(
        value: Verdict, reason: Reason, until: int | None = None
    ) -> ModelPoolLabelUsage:
        return ModelPoolLabelUsage(
            label=reading.label,
            five_hour=five,
            weekly=week,
            verdict=value,
            reason=reason,
            seconds_until_usable=until,
        )

    if reading.revoked:
        return verdict("limited", "revoked")
    if reading.limited_until is not None and reading.limited_until > now:
        return verdict(
            "limited", "pool_limited_mark", _seconds(reading.limited_until, now)
        )
    counted = [
        (name, w)
        for name, w in (("five_hour", five), ("weekly", week))
        if w.state in _RANK
    ]
    if not counted:
        return verdict("unknown", "no_reading")
    age_limit = policy.max_reading_age_seconds
    if reading.read_at is None:
        return verdict("unknown", "no_reading")
    if _seconds(now, reading.read_at) > age_limit:
        return verdict("unknown", "stale_reading")
    worst = max((w.state for _, w in counted), key=_RANK.__getitem__)
    if worst == "ok":
        return verdict("ok", "within_thresholds")
    name = next(n for n, w in counted if w.state == worst)
    until = None
    if worst == "limited":
        resets = [
            w.seconds_to_reset
            for _, w in counted
            if w.state == "limited" and w.seconds_to_reset is not None
        ]
        until = max(resets) if resets else None
    return verdict(
        "limited" if worst == "limited" else "drain",
        _REASON[(name, worst)],
        until,
    )


class HandlerPoolUsageVerdict:
    """Judge every label of the pool by the overlay's thresholds."""

    def handle(self, request: ModelPoolUsageRequest) -> ModelPoolUsageResult:
        labels = tuple(
            sorted(
                (
                    _label_usage(r, request.policy, request.now)
                    for r in request.readings
                ),
                key=lambda u: u.label,
            )
        )
        counts = {v: sum(1 for u in labels if u.verdict == v) for v in _VERDICTS}
        drain = (
            tuple(u.label for u in labels if u.verdict == "drain")
            if counts["ok"]
            else ()
        )
        return ModelPoolUsageResult(
            labels=labels,
            drain_labels=drain,
            ok=counts["ok"],
            drain=counts["drain"],
            limited=counts["limited"],
            unknown=counts["unknown"],
            correlation_id=request.correlation_id,
        )


__all__ = ["HandlerPoolUsageVerdict"]
