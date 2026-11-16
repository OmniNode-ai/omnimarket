# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC1: the metering headline saves nothing on a run whose usage is not measured.

The audit found the all-time headline credited 93 runs whose provider usage was
unknown ($2.372418, 83 of them Beam), while the savings view left them out. A
run counts toward the saving only when every call it made was measured by the
provider; any other run is counted as unknown tokens, by reason, as the savings
view already does.

Failure modes, each with a test below:
  U1  a run whose provider usage is unknown or estimated is credited a saving
  U2  a run with no call record is credited a saving
  U3  a provider-measured run loses its saving (over-exclusion)
  U4  a store that has recorded no call reads every run as unmeasured
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_projection_metering_summary.models import (
    ModelMeteringSummaryRow,
)
from omnimarket.projection.sqlite_metering_summary import (
    refresh_metering_summary,
    resolve_metering_baseline_model,
)
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _TENANT,
    _call,
    _delegation,
    _store,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 16, tzinfo=UTC)


def _all_row(path: Path) -> ModelMeteringSummaryRow:
    rows = refresh_metering_summary(
        path, _TENANT, resolve_metering_baseline_model().model, _NOW
    )
    [row] = [r for r in rows if r.window_kind == "all"]
    return row


def _five_runs(tmp_path: Path) -> Path:
    runs: tuple[dict[str, Any], ...] = (
        _delegation("measured"),
        _delegation("refused", terminal_failure_cause="quality_gate_refused"),
        _delegation("unknown"),
        _delegation("estimated"),
        _delegation("no-call"),
    )
    calls = (
        _call("measured", "measured"),
        _call("refused", "measured"),
        _call("unknown", "unknown"),
        _call("estimated", "estimated"),
    )
    return _store(tmp_path, runs, calls)


def _measured_alone(tmp_path: Path) -> ModelMeteringSummaryRow:
    return _all_row(
        _store(tmp_path, (_delegation("measured"),), (_call("measured", "measured"),))
    )


def test_metering_unknown_usage_saves_only_the_measured_run(tmp_path: Path) -> None:
    """U1, U2, U3: the AC1 falsifier."""
    row = _all_row(_five_runs(tmp_path / "five"))
    alone = _measured_alone(tmp_path / "alone")
    assert alone.savings_usd is not None
    assert Decimal(alone.savings_usd) > 0
    assert row.savings_usd == alone.savings_usd
    assert row.runs_total == 5
    assert row.runs_failed == 1
    assert row.runs_unknown_tokens == 3


def test_metering_unknown_usage_a_store_with_no_call_recorded_keeps_its_runs(
    tmp_path: Path,
) -> None:
    """U4: the store has the call table, as every store does, and no row in it."""
    path = _store(tmp_path, (_delegation("measured"),))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_call_metrics").fetchone() == (0,)
    row = _all_row(path)
    assert row.runs_unknown_tokens == 0
    assert row.savings_usd == _measured_alone(tmp_path / "alone").savings_usd
