# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23870: a synthetic code edit turn is counted nowhere in real traffic.

The code edit loop sends each model turn as a delegate run stamped with the
request's traffic class and source surface. A trial's turns come back as
terminals whose provenance is synthetic; the delegation events keep them typed
``experiment`` and the real-traffic readings (run and token counts, spend,
savings, the metering records) must read the same with them as without.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.projection import sqlite_metering_summary
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.sqlite_metering_reader import read_metering_records
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_UUID,
    seed_tenant_registry,
)
from tests.projection.test_synthetic_exclusion import (
    _BASELINE,
    _REAL_SAVINGS,
    _SYNTHETIC_SAVINGS,
    _terminal,
    _write,
)

pytestmark = pytest.mark.unit

_SURFACE = "code-eval-trial"


def _code_edit_turn(
    traffic_class: str | None, savings: float, lane: str
) -> dict[str, Any]:
    """One loop turn's terminal: a code_generation run stamped by the loop."""
    payload = _terminal(traffic_class, savings)
    payload["task_type"] = "code_generation"
    payload["caller_lane"] = lane
    if "provenance" in payload:
        payload["provenance"] = {**payload["provenance"], "source_surface": _SURFACE}
    return payload


def test_a_synthetic_code_edit_turn_is_kept_typed_and_left_out_of_real_traffic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(path)
    seed_tenant_registry(db)
    real = _code_edit_turn("organic", _REAL_SAVINGS, "real-lane")
    _write(db, real)
    monkeypatch.setattr(
        sqlite_metering_summary, "resolve_baseline", lambda _: _BASELINE
    )
    tenant = str(PROJECTION_TENANT_UUID)
    now = datetime.now(UTC) + timedelta(minutes=1)

    def reading() -> dict[str, Any]:
        sqlite_metering_summary.refresh_metering_summary(
            path, tenant, _BASELINE.model, now
        )
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            return dict(
                conn.execute(
                    "SELECT runs_total, runs_measured, tokens_in, tokens_out, "
                    "spend_usd, savings_usd FROM metering_summary "
                    "WHERE window_kind = 'all'"
                ).fetchone()
            )
        finally:
            conn.close()

    before = reading()
    assert before["runs_total"] == 1

    trial_turns = [
        _code_edit_turn("synthetic", _SYNTHETIC_SAVINGS, "trial-lane-1")
        for _ in range(3)
    ]
    for turn in trial_turns:
        _write(db, turn)

    # Positive control: all three trial turns are stored, typed experiment.
    stored = {
        r["correlation_id"]: (r["data_source"], r["caller_lane"])
        for r in db.query("delegation_events")
    }
    assert stored[real["correlation_id"]] == ("real", "real-lane")
    for turn in trial_turns:
        assert stored[turn["correlation_id"]] == ("experiment", "trial-lane-1")
    assert len(stored) == 4

    # The real-traffic reading is the same with the trial turns planted.
    assert reading() == before
    assert [r.correlation_id for r in read_metering_records(db_path=path)] == [
        real["correlation_id"]
    ]
