# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A local store serves delegation.savings.v1's unpriced_call_count (OMN-20717).

Migration 096 appends ``unpriced_call_count`` to the Postgres savings view and
the exposure declares it. The read node refuses an exposure whose relation
lacks a declared column (503 ``projection_column_missing``), so a local store
must serve it too, or the local savings page goes dark.

Each test names the failure it exists to catch:

* the local exposure answers 503 after the column is declared;
* an unpriced call is not counted, or a priced or measured-free call is;
* the totals move when an unpriced call is added (they must stay the sum of the
  priced calls, as numbers);
* a store that recorded the OMN-20008 step before this one never gains the
  column.

Every store is a SQLite file under pytest's ``tmp_path``; reads go through the
real read node over :class:`SqliteTableRowSource`, as ``onex dashboard`` does.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _call,
    _delegation,
    _read,
    _row,
    _store,
)

pytestmark = pytest.mark.unit


def test_an_unpriced_call_is_counted_beside_unchanged_totals(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        (
            _delegation("priced", cost_usd=0.25, cost_savings_usd=0.5),
            _delegation("unpriced", cost_usd=None, cost_savings_usd=0.0),
        ),
        (_call("priced", "measured"),),
    )
    row = _row(path)
    assert row["unpriced_call_count"] == 1
    assert row["session_count"] == 2
    assert row["cumulative_local_cost_usd"] == pytest.approx(0.25)
    assert row["cumulative_cloud_cost_usd"] == pytest.approx(0.75)
    assert row["cumulative_savings_usd"] == pytest.approx(0.5)


def test_priced_and_measured_free_calls_count_none(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        (
            _delegation("priced", cost_usd=0.1),
            _delegation("free", cost_usd=0.0),
        ),
        (_call("priced", "measured"), _call("free", "measured")),
    )
    row = _row(path)
    assert row["unpriced_call_count"] == 0
    assert row["cumulative_local_cost_usd"] == pytest.approx(0.1)


def test_the_sessions_array_keeps_its_keys(tmp_path: Path) -> None:
    path = _store(tmp_path, (_delegation("unpriced", cost_usd=None),))
    sessions: list[dict[str, Any]] = _row(path)["sessions"]
    assert len(sessions) == 1
    assert "unpriced" not in sessions[0]
    assert "unpriced_call_count" not in sessions[0]


def test_a_store_that_recorded_the_earlier_step_gains_the_column(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path, (_delegation("old", cost_usd=None),))
    conn = sqlite3.connect(path)
    conn.execute("DROP VIEW projection_delegation_savings")
    conn.execute(
        "DELETE FROM omnimarket_sqlite_store_steps "
        "WHERE step IN ('omn20717_delegation_savings_unpriced_view', "
        "'omn21206_delegation_savings_measured_view')"
    )
    conn.commit()
    conn.close()
    assert _read(path)[0] == 503, "positive control: the view is really gone"

    from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

    SqliteDatabaseAdapter(path)._connect().close()

    assert _row(path)["unpriced_call_count"] == 1
