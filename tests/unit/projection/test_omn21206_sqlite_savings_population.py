# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local-store population falsifiers using disposable SQLite files.

Catch unmeasured money counted as measured, missing excluded counts, measured
zero mistaken for unknown, missing token-bearing sessions, tenant leakage and
an existing store never applying the new view step. No server is started.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _OTHER,
    _call,
    _delegation,
    _row,
    _store,
)

pytestmark = pytest.mark.unit


def test_money_excludes_estimated_unknown_but_sessions_and_tokens_retain_them(
    tmp_path: Path,
) -> None:
    path = _store(
        tmp_path,
        (
            *(
                _delegation(
                    run,
                    cost_usd=cost,
                    cost_savings_usd=cost * 2,
                    tokens_input=tokens,
                    tokens_output=0,
                )
                for run, cost, tokens in (
                    ("measured", 1, 14),
                    ("estimated", 4, 22),
                    ("unknown", 7, 33),
                )
            ),
            _delegation("other", tenant_id=_OTHER, cost_usd=9, cost_savings_usd=18),
        ),
        (
            _call("measured", "measured"),
            _call("estimated", "estimated"),
            _call("other", "measured"),
        ),
    )
    result = _row(path)
    assert result["session_count"] == 3
    assert result["cumulative_local_cost_usd"] == 1
    assert result["cumulative_savings_usd"] == 2
    assert result["measured_run_count"] == 1
    assert result["estimated_run_count"] == 1
    assert result["unknown_run_count"] == 1
    assert (
        sum(s["prompt_tokens"] + s["completion_tokens"] for s in result["sessions"])
        == 69
    )
    assert _row(path, _OTHER)["cumulative_savings_usd"] == 18


def test_measured_zero_stays_zero_with_unmeasured_money_beside_it(
    tmp_path: Path,
) -> None:
    path = _store(
        tmp_path,
        (
            _delegation("free", cost_usd=0, cost_savings_usd=0),
            _delegation("unknown", cost_usd=7, cost_savings_usd=14),
        ),
        (_call("free", "measured"),),
    )
    result = _row(path)
    assert result["session_count"] == 2
    assert result["cumulative_local_cost_usd"] == 0
    assert result["cumulative_savings_usd"] == 0
    assert result["measured_run_count"] == result["unknown_run_count"] == 1


def test_existing_store_replaces_the_old_view_once(tmp_path: Path) -> None:
    path = _store(tmp_path, (_delegation("old"),), (_call("old", "measured"),))
    old_sql = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_projection_savings/sqlite/delegation_savings_view_unpriced.sql"
    ).read_text()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP VIEW projection_delegation_savings")
        conn.executescript(old_sql)
        conn.execute(
            "DELETE FROM omnimarket_sqlite_store_steps WHERE step='omn21206_delegation_savings_measured_view'"
        )
        columns = {
            r[1]
            for r in conn.execute("PRAGMA table_info(projection_delegation_savings)")
        }
        assert "unknown_run_count" not in columns, (
            "positive control: genuinely old view"
        )
    SqliteDatabaseAdapter(path)._connect().close()
    assert _row(path)["measured_run_count"] == 1
    SqliteDatabaseAdapter(path)._connect().close()
    assert _row(path)["measured_run_count"] == 1
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM omnimarket_sqlite_store_steps "
                "WHERE step='omn21206_delegation_savings_measured_view'"
            ).fetchone()[0]
            == 1
        )
