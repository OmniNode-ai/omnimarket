# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC1, AC2: the local savings view serves one definition of a saving.

The savings topic served the legacy recorded saving (cost_savings_usd) with no
baseline model, so it read $28.554502 beside the metering headline's
$31.853384 on a copy of an operator store, and it credited runs whose answer was
refused. The view now serves each run's modelled saving against the named
baseline and pricing version, and none for a run that ended with a failure
cause, so it agrees with the metering fold.

Failure modes, each with a test below:
  V1  a refused run's stored modelled saving is counted in the headline
  V2  the headline sums the legacy recorded saving instead of the modelled one
  V3  the view serves no baseline model, or the manifest number, not the
      pricing version the saving was priced against
  V4  a store on the previous view step never gains this view
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _call,
    _delegation,
    _row,
    _store,
)

pytestmark = pytest.mark.unit

_PRICED = {
    "modelled_savings_usd": 0.0004,
    "savings_baseline_model": "claude-sonnet-5-5",
    "savings_pricing_manifest_version": "pm-7",
}


def _two_runs(tmp_path: Path) -> Path:
    return _store(
        tmp_path,
        (
            _delegation("succeeded", **_PRICED),
            _delegation(
                "refused", terminal_failure_cause="quality_gate_refused", **_PRICED
            ),
        ),
        (_call("succeeded", "measured"), _call("refused", "measured")),
    )


def _sessions(path: Path) -> dict[str, dict[str, Any]]:
    return {str(s["session_id"]): s for s in _row(path)["sessions"]}


def test_savings_view_one_definition_counts_only_the_run_that_succeeded(
    tmp_path: Path,
) -> None:
    """V1, V2."""
    path = _two_runs(tmp_path)
    assert _row(path)["cumulative_savings_usd"] == pytest.approx(0.0004)
    sessions = _sessions(path)
    assert sessions["refused"]["savings_usd"] == 0.0
    assert sessions["succeeded"]["savings_usd"] == pytest.approx(0.0004)


def test_savings_view_one_definition_names_its_baseline_and_pricing_version(
    tmp_path: Path,
) -> None:
    """V3."""
    row = _row(_two_runs(tmp_path))
    assert row["baseline_model"] == "claude-sonnet-5-5"
    assert row["pricing_manifest_version"] == "pm-7"


def test_savings_view_one_definition_a_store_on_the_previous_step_gains_the_view(
    tmp_path: Path,
) -> None:
    """V4."""
    path = _two_runs(tmp_path)
    conn = sqlite3.connect(path)
    previous = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_projection_savings/sqlite"
        / "delegation_savings_view_null_cost.sql"
    ).read_text(encoding="utf-8")
    conn.execute("DROP VIEW projection_delegation_savings")
    conn.execute(previous)
    conn.execute(
        "DELETE FROM omnimarket_sqlite_store_steps "
        "WHERE step = 'omn24615_delegation_savings_one_definition_view'"
    )
    conn.commit()
    conn.close()
    assert _row(path)["baseline_model"] is None

    SqliteDatabaseAdapter(path)._connect().close()

    assert _row(path)["baseline_model"] == "claude-sonnet-5-5"
