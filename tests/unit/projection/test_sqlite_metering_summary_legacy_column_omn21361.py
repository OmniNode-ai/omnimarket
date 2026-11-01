# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21361: a local store whose metering_summary predates
``savings_per_measured_run_usd`` is migrated on open, so the end-of-delegate
refresh reads its stored row instead of failing validation on every run.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.pricing import DEFAULT_BASELINE_MODEL
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.sqlite_metering_summary import (
    read_summary_row,
    refresh_metering_after_terminal,
)

pytestmark = pytest.mark.unit

_TENANT = "00000000-0000-4000-8000-000000000001"
_STEP = "omn21361_metering_summary_savings_per_measured_run_column"
_TERMINAL = datetime(2026, 10, 24, 6, 20, tzinfo=UTC)


def _columns(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(metering_summary)")}
    finally:
        conn.close()


def _make_legacy(path: Path) -> None:
    """Strip the column and its step record, as a store written before them."""
    conn = sqlite3.connect(path)
    conn.execute(
        "ALTER TABLE metering_summary DROP COLUMN savings_per_measured_run_usd"
    )
    conn.execute("DELETE FROM omnimarket_sqlite_store_steps WHERE step = ?", (_STEP,))
    conn.commit()
    conn.close()
    assert "savings_per_measured_run_usd" not in _columns(path)


def _refreshed_store(tmp_path: Path) -> Path:
    path = tmp_path / "delegation.sqlite"
    SqliteDatabaseAdapter(path)._connect().close()
    refresh_metering_after_terminal(
        path,
        tenant_id=_TENANT,
        terminal_at=_TERMINAL,
        baseline_model=DEFAULT_BASELINE_MODEL,
    )
    return path


def test_metering_summary_legacy_column_is_added_on_open(tmp_path: Path) -> None:
    path = _refreshed_store(tmp_path)
    _make_legacy(path)

    row = read_summary_row(path, _TENANT, "all", "", DEFAULT_BASELINE_MODEL)

    assert row is not None
    assert row.savings_per_measured_run_usd is None
    assert "savings_per_measured_run_usd" in _columns(path)


def test_metering_refresh_self_heals(tmp_path: Path) -> None:
    path = _refreshed_store(tmp_path)
    _make_legacy(path)

    first = refresh_metering_after_terminal(
        path,
        tenant_id=_TENANT,
        terminal_at=_TERMINAL,
        baseline_model=DEFAULT_BASELINE_MODEL,
    )
    second = refresh_metering_after_terminal(
        path,
        tenant_id=_TENANT,
        terminal_at=_TERMINAL,
        baseline_model=DEFAULT_BASELINE_MODEL,
    )

    assert first.tenant_id == second.tenant_id == _TENANT
    assert "savings_per_measured_run_usd" in _columns(path)
    assert read_summary_row(path, _TENANT, "all", "", DEFAULT_BASELINE_MODEL)
