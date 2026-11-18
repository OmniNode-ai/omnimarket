# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run whose cost was never measured serves a null session cost locally.

The local ``delegation.savings.v1`` view served each session's
``local_cost_usd`` as ``COALESCE(cost_usd, 0)``, so a run the writer stored
with no cost read as a measured free run, and the dashboard's Cost column
showed $0 for it. Each test names the failure it exists to catch:

* an unmeasured session cost is served as 0;
* a measured cost of exactly 0 is served as null (over-correction);
* the measured cumulative totals move when an unmeasured run is present;
* a store that already recorded the previous savings-view step never
  receives the corrected view.

Every store is a SQLite file under pytest's ``tmp_path``.
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


def _sessions(path: Path) -> dict[str, dict[str, Any]]:
    return {str(s["session_id"]): s for s in _row(path)["sessions"]}


def _three_runs(tmp_path: Path) -> Path:
    return _store(
        tmp_path,
        (
            _delegation("unmeasured", cost_usd=None, cost_savings_usd=None),
            _delegation("free", cost_usd=0.0, cost_savings_usd=0.000486),
            _delegation("priced", cost_usd=0.25, cost_savings_usd=0.75),
        ),
        (
            _call("unmeasured", "measured"),
            _call("free", "measured"),
            _call("priced", "measured"),
        ),
    )


def test_savings_view_null_cost_an_unmeasured_run_serves_a_null_cost(
    tmp_path: Path,
) -> None:
    sessions = _sessions(_three_runs(tmp_path))
    assert sessions["unmeasured"]["local_cost_usd"] is None


def test_savings_view_null_cost_a_measured_zero_and_a_priced_run_keep_their_cost(
    tmp_path: Path,
) -> None:
    sessions = _sessions(_three_runs(tmp_path))
    assert sessions["free"]["local_cost_usd"] == 0.0
    assert sessions["free"]["local_cost_usd"] is not None
    assert sessions["priced"]["local_cost_usd"] == pytest.approx(0.25)


def test_savings_view_null_cost_measured_totals_are_unchanged(
    tmp_path: Path,
) -> None:
    with_unmeasured = _row(_three_runs(tmp_path / "a"))
    without = _row(
        _store(
            tmp_path / "b",
            (
                _delegation("free", cost_usd=0.0, cost_savings_usd=0.000486),
                _delegation("priced", cost_usd=0.25, cost_savings_usd=0.75),
            ),
            (_call("free", "measured"), _call("priced", "measured")),
        )
    )
    for total in (
        "cumulative_local_cost_usd",
        "cumulative_savings_usd",
        "cumulative_cloud_cost_usd",
        "cumulative_counterfactual_baseline_usd",
    ):
        assert with_unmeasured[total] == pytest.approx(without[total]), total
    assert with_unmeasured["cumulative_local_cost_usd"] == pytest.approx(0.25)
    assert with_unmeasured["unpriced_call_count"] == 1


def test_savings_view_null_cost_a_store_on_the_previous_step_gains_the_view(
    tmp_path: Path,
) -> None:
    path = _three_runs(tmp_path)
    conn = sqlite3.connect(path)
    # Put the store back on the view the previous step installed, with only
    # that step's marker recorded, as a store opened before this change is.
    measured = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_projection_savings/sqlite"
        / "delegation_savings_view_measured.sql"
    ).read_text(encoding="utf-8")
    conn.execute("DROP VIEW projection_delegation_savings")
    conn.execute(measured)
    conn.execute(
        "DELETE FROM omnimarket_sqlite_store_steps "
        "WHERE step = 'omn24224_delegation_savings_null_cost_view'"
    )
    conn.commit()
    conn.close()
    assert _sessions(path)["unmeasured"]["local_cost_usd"] == 0.0

    SqliteDatabaseAdapter(path)._connect().close()

    assert _sessions(path)["unmeasured"]["local_cost_usd"] is None
