# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_database_sweep (OMN-13676).

COMPUTE node with self-collected I/O. PRE-WORK (this PR): the psql boundary is
now a constructor-injected probe seam (``NodeDatabaseSweep(psql_runner=...)`` →
``PsqlRunner = (query, database) -> (rc, stdout, stderr)``). Tests inject a fake
runner returning synthetic DB rows; production leaves it ``None`` and the live
``_psql`` subprocess is used. We NEVER monkeypatch subprocess/asyncpg — the seam
is a typed collaborator. Filesystem inputs (Drizzle schema, vendored node
migrations) are real files under ``tmp_path``.

Each case asserts typed result fields (table status, row counts, migration
state); the negative-control cases force STALE / EMPTY / MISSING tables and a
PENDING (never-applied) vendored migration.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep import (
    DatabaseSweepRequest,
    NodeDatabaseSweep,
)

PsqlResponse = tuple[int, str, str]


def _table_psql(
    ts_col: str | None, health_row: PsqlResponse
) -> Callable[[str, str], PsqlResponse]:
    """Fake psql for the table-health path.

    Answers the information_schema column-existence probe (``ts_col`` is the one
    that 'exists') and the subsequent health query (``health_row``)."""

    def _runner(query: str, database: str) -> PsqlResponse:
        if "information_schema.columns" in query:
            if ts_col is not None and f"column_name='{ts_col}'" in query:
                return (0, "1", "")
            return (0, "", "")
        if "CASE" in query:  # the count/max/CASE health query
            return health_row
        return (1, "", f"unexpected query: {query[:60]}")

    return _runner


def _drizzle_schema(home: Path, table: str) -> None:
    schema = home / "omnidash" / "shared" / f"{table}-schema.ts"
    schema.parent.mkdir(parents=True, exist_ok=True)
    schema.write_text(f'pgTable("{table}", {{}})', encoding="utf-8")


_TABLE = "agg_table"

# (id, ts_col, health_row, expected_status, expected_row_count)
TABLE_CASES = [
    pytest.param(
        "created_at",
        (0, "50|2026-06-27 11:00:00|HEALTHY", ""),
        "HEALTHY",
        50,
        id="healthy-fresh-table",
    ),
    pytest.param(
        "created_at",
        (0, "5|2026-06-01 00:00:00|STALE", ""),
        "STALE",
        5,
        id="stale-table",
    ),
    pytest.param(
        "created_at",
        (0, "0||EMPTY", ""),
        "EMPTY",
        0,
        id="empty-table",
    ),
    pytest.param(
        "created_at",
        (1, "", "relation does not exist"),
        "MISSING",
        0,
        id="missing-table-query-error",
    ),
    pytest.param(
        None,  # no timestamp column found anywhere
        (1, "", "n/a"),
        "UNKNOWN",
        0,
        id="no-timestamp-column-unknown",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("ts_col", "health_row", "expected_status", "expected_row_count"),
    [(c.values[0], c.values[1], c.values[2], c.values[3]) for c in TABLE_CASES],
    ids=[c.id for c in TABLE_CASES],
)
def test_database_sweep_table_health_multiparam(
    tmp_path: Path,
    ts_col: str | None,
    health_row: PsqlResponse,
    expected_status: str,
    expected_row_count: int,
) -> None:
    home = tmp_path / "omni_home"
    home.mkdir()
    _drizzle_schema(home, _TABLE)  # makes the table drizzle_defined

    handler = NodeDatabaseSweep(psql_runner=_table_psql(ts_col, health_row))
    result = handler.handle(DatabaseSweepRequest(omni_home=str(home), table=_TABLE))

    by_name = {r.table_name: r for r in result.table_results}
    assert _TABLE in by_name, f"missing table result: {by_name.keys()}"
    table = by_name[_TABLE]
    assert table.status == expected_status
    assert table.drizzle_defined is True
    if expected_status in ("HEALTHY", "STALE", "EMPTY"):
        assert table.row_count == expected_row_count


@pytest.mark.integration
def test_database_sweep_vendored_migration_pending(tmp_path: Path) -> None:
    """Negative control: a vendored node migration with no schema_migrations row
    is classified PENDING and counted in migrations_pending."""
    home = tmp_path / "omni_home"
    mig = home / "omnimarket/src/omnimarket/nodes/node_foo/migrations/001_init.sql"
    mig.parent.mkdir(parents=True, exist_ok=True)
    mig.write_text("-- create table\n", encoding="utf-8")

    # schema_migrations returns no applied node ids → the disk file is unapplied.
    def _runner(query: str, database: str) -> PsqlResponse:
        if "schema_migrations" in query:
            return (0, "", "")
        return (1, "", "unexpected")

    handler = NodeDatabaseSweep(psql_runner=_runner)
    result = handler.handle(
        DatabaseSweepRequest(omni_home=str(home), table="not_a_real_table")
    )

    node_mig = next(
        m for m in result.migration_results if m.migration_tool == "node-vendored"
    )
    assert node_mig.status == "PENDING"
    assert node_mig.disk_migrations == 1
    assert node_mig.applied_migrations == 0
    assert "node:node_foo:001_init.sql" in node_mig.message
    assert result.migrations_pending >= 1
    assert result.status == "issues_found"


@pytest.mark.integration
def test_database_sweep_vendored_migration_current(tmp_path: Path) -> None:
    """Positive control: a vendored node migration that IS recorded in
    schema_migrations is classified CURRENT."""
    home = tmp_path / "omni_home"
    mig = home / "omnimarket/src/omnimarket/nodes/node_foo/migrations/001_init.sql"
    mig.parent.mkdir(parents=True, exist_ok=True)
    mig.write_text("-- create table\n", encoding="utf-8")

    def _runner(query: str, database: str) -> PsqlResponse:
        if "schema_migrations" in query:
            return (0, "node:node_foo:001_init.sql", "")
        return (1, "", "unexpected")

    handler = NodeDatabaseSweep(psql_runner=_runner)
    result = handler.handle(
        DatabaseSweepRequest(omni_home=str(home), table="not_a_real_table")
    )

    node_mig = next(
        m for m in result.migration_results if m.migration_tool == "node-vendored"
    )
    assert node_mig.status == "CURRENT"
    assert node_mig.applied_migrations == 1
