# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC8 (Task 5, finding 5 of the capped review): the stale falsifier is read only while both stages deliver heartbeats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.overlay_store.heartbeat_gate import (
    EnumStaleReading,
    read_stale_falsifier,
)

NOW = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
BOUND = 120.0


def _ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


ALIVE = _ago(10)


def _read(
    *,
    overlay_heartbeat_at: datetime | None = ALIVE,
    projection_heartbeat_at: datetime | None = ALIVE,
    writer_heartbeat_at: datetime | None = ALIVE,
) -> EnumStaleReading:
    return read_stale_falsifier(
        now=NOW,
        bound_s=BOUND,
        overlay_heartbeat_at=overlay_heartbeat_at,
        projection_heartbeat_at=projection_heartbeat_at,
        writer_heartbeat_at=writer_heartbeat_at,
    )


def test_stale_requires_both_heartbeats_positive_control_both_alive_reads_fresh() -> (
    None
):
    assert _read() == EnumStaleReading.FRESH


def test_stale_requires_both_heartbeats_both_alive_and_an_old_overlay_reads_stale() -> (
    None
):
    assert _read(overlay_heartbeat_at=_ago(500)) == EnumStaleReading.STALE


@pytest.mark.parametrize("stage", ["projection_heartbeat_at", "writer_heartbeat_at"])
def test_stale_requires_both_heartbeats_a_quiet_stage_is_unreadable_not_stale(
    stage: str,
) -> None:
    quiet = {stage: _ago(500), "overlay_heartbeat_at": _ago(500)}
    assert _read(**quiet) == EnumStaleReading.UNREADABLE


@pytest.mark.parametrize("stage", ["projection_heartbeat_at", "writer_heartbeat_at"])
def test_stale_requires_both_heartbeats_a_stage_that_never_beat_is_unreadable(
    stage: str,
) -> None:
    assert _read(**{stage: None}) == EnumStaleReading.UNREADABLE


def test_stale_requires_both_heartbeats_a_fresh_overlay_does_not_excuse_a_quiet_stage() -> (
    None
):
    assert _read(writer_heartbeat_at=_ago(500)) == EnumStaleReading.UNREADABLE
