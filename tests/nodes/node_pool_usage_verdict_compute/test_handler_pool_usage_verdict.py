# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23607: label readings + overlay policy -> HandlerPoolUsageVerdict."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from omnimarket.nodes.node_pool_usage_verdict_compute.handlers.handler_pool_usage_verdict import (
    HandlerPoolUsageVerdict,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.models import (
    ModelPoolLabelReading,
    ModelPoolUsagePolicy,
    ModelPoolUsageRequest,
    ModelPoolUsageWindowPolicy,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 12, 1, 12, 0, 0, tzinfo=UTC)
CID = UUID("00000000-0000-4000-8000-000000000236")
FIVE_RESET = NOW + timedelta(hours=5)
WEEK_RESET = NOW + timedelta(days=3)


def _policy(
    five: tuple[float, float] = (0.5, 0.9),
    weekly: tuple[float, float] = (0.7, 0.95),
    max_age: int = 300,
) -> ModelPoolUsagePolicy:
    return ModelPoolUsagePolicy(
        five_hour=ModelPoolUsageWindowPolicy(drain_at=five[0], limited_at=five[1]),
        weekly=ModelPoolUsageWindowPolicy(drain_at=weekly[0], limited_at=weekly[1]),
        max_reading_age_seconds=max_age,
    )


def _reading(
    label: str,
    five: float | None = 0.1,
    week: float | None = 0.1,
    **overrides: object,
) -> ModelPoolLabelReading:
    fields: dict[str, object] = {
        "label": label,
        "read_at": NOW,
        "five_hour_utilization": five,
        "five_hour_resets_at": FIVE_RESET,
        "seven_day_utilization": week,
        "seven_day_resets_at": WEEK_RESET,
    }
    fields.update(overrides)
    return ModelPoolLabelReading(**fields)


def _handle(
    readings: tuple[ModelPoolLabelReading, ...],
    policy: ModelPoolUsagePolicy | None = None,
):
    return HandlerPoolUsageVerdict().handle(
        ModelPoolUsageRequest(
            now=NOW,
            policy=policy if policy is not None else _policy(),
            readings=readings,
            correlation_id=CID,
        )
    )


def _by_label(result):
    return {usage.label: usage for usage in result.labels}


def test_rule_1_revoked_label_is_limited_before_any_window_is_consulted() -> None:
    result = _handle((_reading("alpha", five=0.0, week=0.0, revoked=True),))
    usage = _by_label(result)["alpha"]
    assert usage.verdict == "limited"
    assert usage.reason == "revoked"
    assert usage.seconds_until_usable is None
    assert result.limited == 1
    assert result.ok == 0


def test_rule_2_pool_limited_mark_carries_seconds_until_usable() -> None:
    limited_until = NOW + timedelta(seconds=120)
    result = _handle((_reading("alpha", limited_until=limited_until),))
    usage = _by_label(result)["alpha"]
    assert usage.verdict == "limited"
    assert usage.reason == "pool_limited_mark"
    assert usage.seconds_until_usable == 120
    # An expired mark does not limit.
    expired = _handle((_reading("bravo", limited_until=NOW - timedelta(seconds=1)),))
    assert _by_label(expired)["bravo"].verdict == "ok"


def test_rule_3_window_states_at_the_exact_thresholds_of_the_policy() -> None:
    result = _handle(
        (
            _reading("below", five=0.49),
            _reading("at-drain", five=0.5),
            _reading("at-limited", five=0.9),
        )
    )
    labels = _by_label(result)
    assert labels["below"].five_hour.state == "ok"
    assert labels["at-drain"].five_hour.state == "drain"
    assert labels["at-limited"].five_hour.state == "limited"
    assert labels["below"].verdict == "ok"
    assert labels["at-drain"].verdict == "drain"
    assert labels["at-limited"].verdict == "limited"
    assert labels["below"].five_hour.fraction_used == 0.49
    assert labels["below"].five_hour.seconds_to_reset == 5 * 3600


def test_rule_4_unread_window_is_reported_and_never_drawn_as_zero_use() -> None:
    result = _handle((_reading("alpha", five=None, five_hour_resets_at=None),))
    usage = _by_label(result)["alpha"]
    assert usage.five_hour.state == "unread"
    assert usage.five_hour.fraction_used is None
    assert usage.five_hour.seconds_to_reset is None
    assert usage.verdict == "ok"
    assert usage.reason == "within_thresholds"


def test_rule_5_reset_passed_window_is_not_counted_for_the_verdict() -> None:
    result = _handle(
        (
            _reading(
                "alpha",
                five=0.9,
                five_hour_resets_at=NOW - timedelta(seconds=1),
            ),
        )
    )
    usage = _by_label(result)["alpha"]
    assert usage.five_hour.state == "reset_passed"
    assert usage.five_hour.fraction_used is None
    assert usage.five_hour.seconds_to_reset == 0
    assert usage.verdict == "ok"
    assert usage.reason == "within_thresholds"


def test_rule_6_missing_or_stale_reading_is_unknown_and_never_ok() -> None:
    no_reading = _handle(
        (
            _reading(
                "alpha",
                five=None,
                week=None,
                five_hour_resets_at=None,
                seven_day_resets_at=None,
            ),
        )
    )
    assert _by_label(no_reading)["alpha"].verdict == "unknown"
    assert _by_label(no_reading)["alpha"].reason == "no_reading"

    no_read_at = _handle((_reading("bravo", read_at=None),))
    assert _by_label(no_read_at)["bravo"].reason == "no_reading"

    stale = _handle((_reading("charlie", read_at=NOW - timedelta(seconds=301)),))
    usage = _by_label(stale)["charlie"]
    assert usage.verdict == "unknown"
    assert usage.reason == "stale_reading"

    fresh_at_the_bound = _handle(
        (_reading("delta", read_at=NOW - timedelta(seconds=300)),)
    )
    assert _by_label(fresh_at_the_bound)["delta"].verdict == "ok"


def test_rule_worst_window_decides_the_verdict_and_names_its_reason() -> None:
    result = _handle((_reading("alpha", five=0.92, week=0.1),))
    usage = _by_label(result)["alpha"]
    assert usage.verdict == "limited"
    assert usage.reason == "five_hour_limited"
    assert usage.seconds_until_usable == 5 * 3600

    drained = _handle((_reading("bravo", five=0.1, week=0.95),))
    usage = _by_label(drained)["bravo"]
    assert usage.verdict == "limited"
    assert usage.reason == "weekly_limited"
    assert usage.seconds_until_usable == 3 * 24 * 3600

    worst_drain = _handle((_reading("charlie", five=0.6, week=0.8),))
    assert _by_label(worst_drain)["charlie"].verdict == "drain"
    assert _by_label(worst_drain)["charlie"].reason == "five_hour_drain"


def test_drain_labels_only_while_another_label_is_still_ok() -> None:
    with_ok = _handle(
        (
            _reading("alpha"),
            _reading("bravo", five=0.6),
            _reading("charlie", five=0.92),
            _reading(
                "delta",
                five=None,
                week=None,
                five_hour_resets_at=None,
                seven_day_resets_at=None,
            ),
        )
    )
    assert with_ok.drain_labels == ("bravo",)
    assert (with_ok.ok, with_ok.drain, with_ok.limited, with_ok.unknown) == (
        1,
        1,
        1,
        1,
    )

    without_ok = _handle(
        (
            _reading("bravo", five=0.6),
            _reading("charlie", five=0.92),
            _reading(
                "delta",
                five=None,
                week=None,
                five_hour_resets_at=None,
                seven_day_resets_at=None,
            ),
        )
    )
    assert without_ok.ok == 0
    assert without_ok.drain == 1
    assert without_ok.drain_labels == ()


def test_readings_order_does_not_change_the_result() -> None:
    readings = (
        _reading("alpha"),
        _reading("bravo", five=0.6),
        _reading("charlie", five=0.92),
        _reading(
            "delta",
            five=None,
            week=None,
            five_hour_resets_at=None,
            seven_day_resets_at=None,
        ),
    )
    forward = _handle(readings)
    reverse = _handle(tuple(reversed(readings)))
    assert forward == reverse
    assert tuple(usage.label for usage in forward.labels) == (
        "alpha",
        "bravo",
        "charlie",
        "delta",
    )


def test_control_the_verdict_follows_the_policy_not_the_code() -> None:
    reading = (_reading("alpha", five=0.6, week=0.1),)
    under_strict = _handle(reading, policy=_policy(five=(0.5, 0.9)))
    assert _by_label(under_strict)["alpha"].verdict == "drain"
    under_lenient = _handle(reading, policy=_policy(five=(0.95, 1.0)))
    assert _by_label(under_lenient)["alpha"].verdict == "ok"
