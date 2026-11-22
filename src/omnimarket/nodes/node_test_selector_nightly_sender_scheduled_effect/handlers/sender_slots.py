# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which night a tick belongs to, when it started, and when the night counts as missed."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    ModelNightlyRunView,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender_schedule_overlay import (
    ModelNightlySenderScheduleOverlay,
)


def slot_date_for(now: datetime, overlay: ModelNightlySenderScheduleOverlay) -> date:
    """The UTC date of the slot a tick at ``now`` looks for.

    Before the day's earliest check time the night still being followed is the previous day's slot.
    """
    utc = now.astimezone(UTC)
    if utc.timetz().replace(tzinfo=None) >= overlay.earliest_check_time_utc:
        return utc.date()
    return utc.date() - timedelta(days=1)


def slot_start(slot_date: date, overlay: ModelNightlySenderScheduleOverlay) -> datetime:
    return datetime.combine(slot_date, overlay.nightly_slot_time_utc, tzinfo=UTC)


def missed_after(
    slot_date: date, overlay: ModelNightlySenderScheduleOverlay
) -> datetime:
    """The moment a night with nothing sent counts as missed: the slot plus ``collector_max_lag_hours``."""
    return slot_start(slot_date, overlay) + timedelta(
        hours=overlay.collector_max_lag_hours
    )


def run_for_slot(
    runs: tuple[ModelNightlyRunView, ...],
    slot_date: date,
    overlay: ModelNightlySenderScheduleOverlay,
) -> ModelNightlyRunView | None:
    """The newest run created within the 24 hours from the slot, or ``None``."""
    start = slot_start(slot_date, overlay)
    inside = [r for r in runs if start <= r.created_at < start + timedelta(days=1)]
    return max(inside, key=lambda r: r.created_at, default=None)


__all__: list[str] = ["missed_after", "run_for_slot", "slot_date_for", "slot_start"]
