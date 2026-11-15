# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The second-run guard: who takes it, the age it reports, and the release that ends a run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.scheduled_run_guard import ScheduledRunGuard

pytestmark = pytest.mark.unit
T0 = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)


def test_acquire_on_an_idle_guard_returns_none() -> None:
    guard = ScheduledRunGuard()
    assert guard.try_acquire(T0) is None


def test_second_acquire_returns_the_age_and_leaves_the_start_where_it_was() -> None:
    guard = ScheduledRunGuard()
    guard.try_acquire(T0)
    assert guard.try_acquire(T0 + timedelta(seconds=2.5)) == 2.5
    assert guard.try_acquire(T0 + timedelta(seconds=3)) == 3.0


def test_release_then_acquire_returns_none_again() -> None:
    guard = ScheduledRunGuard()
    guard.try_acquire(T0)
    guard.try_acquire(T0 + timedelta(seconds=2))
    guard.release()
    assert guard.try_acquire(T0 + timedelta(seconds=3)) is None


def test_the_age_is_never_negative_when_now_precedes_the_start() -> None:
    guard = ScheduledRunGuard()
    guard.try_acquire(T0)
    assert guard.try_acquire(T0 - timedelta(seconds=5)) == 0.0
