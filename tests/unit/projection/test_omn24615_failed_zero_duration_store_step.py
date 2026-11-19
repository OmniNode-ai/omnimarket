# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC8, follow-up: a stored failed run at 0 ms is not a measured duration.

The audit found 58 failed runs stored with latency 0 counted as measured durations. The writer now stores no
duration for a run that ended on a failure cause and timed nothing, but rows an earlier build wrote keep their
0 ms. A one-time store step clears them, for both execution_ms (the served run duration) and latency_ms (the model
call time), and leaves every measured duration alone.

Failure modes, each with a test below:
  Z1  a stored failed run at 0 ms is still served as a measured duration
  Z2  a failed run that timed something, or a run without a failure cause, loses its duration
  Z3  the step runs again on every open, clearing a 0 a later write stored on purpose
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _delegation,
    _store,
)

pytestmark = pytest.mark.unit

_STEP = "omn24615_delegation_events_failed_zero_duration"


def _earlier_store(tmp_path: Path) -> Path:
    runs: tuple[dict[str, Any], ...] = (
        _delegation("failed-zero", terminal_failure_cause="provider_error"),
        _delegation("failed-timed", terminal_failure_cause="provider_error"),
        _delegation("completed-zero"),
    )
    path = _store(tmp_path, runs)
    with sqlite3.connect(path) as conn:
        # What an earlier build stored: 0 ms on the failed run that timed nothing.
        for cid, execution, latency in (
            ("failed-zero", 0, 0),
            ("failed-timed", 900, 450),
            ("completed-zero", 0, 0),
        ):
            conn.execute(
                "UPDATE delegation_events SET execution_ms = ?, latency_ms = ? WHERE correlation_id = ?",
                (execution, latency, cid),
            )
        conn.execute(
            "DELETE FROM omnimarket_sqlite_store_steps WHERE step = ?", (_STEP,)
        )
    return path


def _durations(path: Path) -> dict[str, tuple[Any, Any]]:
    with sqlite3.connect(path) as conn:
        return {
            str(cid): (execution, latency)
            for cid, execution, latency in conn.execute(
                "SELECT correlation_id, execution_ms, latency_ms FROM delegation_events"
            )
        }


def test_failed_zero_duration_a_stored_failed_run_at_zero_is_not_measured(
    tmp_path: Path,
) -> None:
    """Z1: the AC8 falsifier over a store an earlier build wrote."""
    path = _earlier_store(tmp_path)
    SqliteDatabaseAdapter(path)._connect().close()
    assert _durations(path)["failed-zero"] == (None, None)


def test_failed_zero_duration_measured_durations_are_kept(tmp_path: Path) -> None:
    """Z2."""
    path = _earlier_store(tmp_path)
    SqliteDatabaseAdapter(path)._connect().close()
    durations = _durations(path)
    assert durations["failed-timed"] == (900, 450)
    assert durations["completed-zero"] == (0, 0)


def test_failed_zero_duration_step_runs_once(tmp_path: Path) -> None:
    """Z3."""
    path = _earlier_store(tmp_path)
    SqliteDatabaseAdapter(path)._connect().close()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE delegation_events SET execution_ms = 0 WHERE correlation_id = 'failed-zero'"
        )
    SqliteDatabaseAdapter(path)._connect().close()
    assert _durations(path)["failed-zero"][0] == 0
