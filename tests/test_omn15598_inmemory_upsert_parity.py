# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15598: InmemoryDatabaseAdapter.upsert diverged from Sqlite/Postgres.

``InmemoryDatabaseAdapter.upsert`` (``src/omnimarket/projection/protocol_database.py``)
did a full-row REPLACE (``rows[i] = row``) on conflict-key match: a column
present on the stored row but absent from the incoming ``row`` dict was
silently dropped. ``SqliteDatabaseAdapter.upsert``
(``sqlite_database.py:126-128``) and ``PostgresSyncProjectionAdapter.upsert``
(``postgres_sync_database.py``) instead build a targeted
``ON CONFLICT ... DO UPDATE SET`` naming only the incoming columns, so an
unnamed column keeps its stored value. The double and the real stores had
OPPOSITE merge semantics for the same call, and every no-clobber unit test
written against the double (which every fast test uses) was vacuous.

Both real adapters agree with each other (verified by reading
``sqlite_database.py`` and ``postgres_sync_database.py``: identical
``ON CONFLICT (...) DO UPDATE SET <update_cols>`` construction), so this test
picks no side -- it makes the double match what BOTH real stores already do.

AC1 (verbatim, ticket OMN-15598): a test parameterized over
``[InmemoryDatabaseAdapter, SqliteDatabaseAdapter]`` seeds a row with columns
``{a, b, c}``, upserts a row naming only ``{conflict_key, b}``, and asserts
column ``c`` survives with its seeded value. It must FAIL for the in-memory
parameter on unmodified ``dev`` and PASS for sqlite. Falsifier: the test
passes on unmodified ``dev`` (then it is not testing this) -- confirmed RED
for ``inmemory`` / GREEN for ``sqlite`` before the ``protocol_database.py``
fix landed; GREEN for both after.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

import pytest

from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_TABLE = "omn15598_parity_probe"


class _AdapterFactory(Protocol):
    def __call__(
        self, tmp_path: Path
    ) -> InmemoryDatabaseAdapter | SqliteDatabaseAdapter: ...


def _make_inmemory(tmp_path: Path) -> InmemoryDatabaseAdapter:
    return InmemoryDatabaseAdapter()


def _make_sqlite(tmp_path: Path) -> SqliteDatabaseAdapter:
    """Pre-create the probe table, matching the ``_drifted_registry_db``
    pattern in ``test_omn15472_registration_service_url_create_path.py`` --
    ``SqliteDatabaseAdapter`` additively ALTERs an EXISTING table's columns
    (``_ensure_columns``) but never CREATEs an arbitrary one, so the harness
    owns table creation the same way the real migrations do.
    """
    db_path = tmp_path / "omn15598_parity.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            f"CREATE TABLE {_TABLE} (id TEXT NOT NULL UNIQUE, a TEXT, b TEXT, c TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    return SqliteDatabaseAdapter(db_path)


@pytest.mark.parametrize(
    "make_adapter",
    [_make_inmemory, _make_sqlite],
    ids=["inmemory", "sqlite"],
)
def test_upsert_partial_update_does_not_clobber_unnamed_column(
    make_adapter: _AdapterFactory, tmp_path: Path
) -> None:
    """AC1: seed {a,b,c}; upsert naming only {conflict_key,b}; c must survive.

    Same upsert-partial-update sequence driven through BOTH adapters -- the
    cross-adapter parity the ticket calls for, not two independent unit
    suites (per feedback_define_and_match_seams: match the seam, don't test
    each side in isolation).
    """
    db = make_adapter(tmp_path)

    ok_seed = db.upsert(
        _TABLE, "id", {"id": "row-1", "a": "seed-a", "b": "seed-b", "c": "seed-c"}
    )
    ok_update = db.upsert(_TABLE, "id", {"id": "row-1", "b": "updated-b"})

    assert ok_seed is True
    assert ok_update is True
    rows = db.query(_TABLE)
    assert len(rows) == 1, (
        "the partial upsert must UPDATE the existing row, not add a second one"
    )
    row = rows[0]
    assert row.get("b") == "updated-b", "the NAMED column must take the new value"
    assert row.get("c") == "seed-c", (
        "an UNNAMED column must survive the UPSERT untouched (targeted-column "
        "update, matching SqliteDatabaseAdapter/PostgresSyncProjectionAdapter "
        "semantics) -- it must not be dropped by a full-row REPLACE"
    )
    assert row.get("a") == "seed-a", "a second unnamed column must also survive"


def test_upsert_insert_path_still_records_only_named_columns() -> None:
    """Negative/fail-closed case: the merge must not fabricate columns on
    INSERT (no pre-existing row to merge with) -- the row written is exactly
    what the caller named, same as before this fix."""
    db = InmemoryDatabaseAdapter()

    db.upsert(_TABLE, "id", {"id": "row-2", "a": "only-a"})

    row = db.query(_TABLE)[0]
    assert row == {"id": "row-2", "a": "only-a"}
