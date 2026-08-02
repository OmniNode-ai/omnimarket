"""ProtocolProjectionDatabaseSync — sync projection database protocol.

Production: asyncpg UPSERT into Postgres on .201:5436.
Tests: InmemoryDatabaseAdapter that records rows for assertion.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolProjectionDatabaseSync(Protocol):
    """Protocol for synchronous projection database operations.

    Disambiguated from the async ProtocolProjectionDatabase in
    omnibase_compat which serves projection runners.
    """

    def upsert(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
    ) -> bool:
        """UPSERT a row. Returns True on success."""
        ...

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        """Query rows from a table with optional filters."""
        ...


# Backward-compat alias — existing code imports DatabaseAdapter
DatabaseAdapter = ProtocolProjectionDatabaseSync


class InmemoryDatabaseAdapter:
    """In-memory database adapter for testing.

    Stores rows in a dict of lists keyed by table name.

    OMN-15598: ``upsert`` performs a TARGETED-COLUMN merge on conflict-key match
    (``{**existing, **row}``), matching :class:`SqliteDatabaseAdapter.upsert`
    (``sqlite_database.py:126-128``, ``ON CONFLICT ... DO UPDATE SET`` naming
    only the incoming columns) and ``PostgresSyncProjectionAdapter.upsert``
    (``postgres_sync_database.py``, same ``ON CONFLICT`` shape) byte-for-byte.
    A column present on the stored row but absent from the incoming ``row``
    dict is left untouched, exactly as it would be on the real stores. Before
    this fix the adapter did a full-row REPLACE (``rows[i] = row``), silently
    dropping any pre-existing column the caller didn't name -- the opposite of
    what every real store does for the same call, which made every no-clobber
    test written against this double vacuous (see
    ``tests/test_omn15598_inmemory_upsert_parity.py``).
    """

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, object]]] = {}
        self.upsert_count: int = 0

    def upsert(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
    ) -> bool:
        if table not in self.tables:
            self.tables[table] = []

        rows = self.tables[table]
        conflict_keys = [key.strip() for key in conflict_key.split(",") if key.strip()]
        if not conflict_keys:
            raise ValueError("conflict_key must contain at least one key")
        missing = [key for key in conflict_keys if key not in row]
        if missing:
            raise KeyError(f"row missing conflict key(s): {missing}")

        # Find existing row with same conflict key value(s).
        for i, existing in enumerate(rows):
            if all(
                key in existing and existing[key] == row[key] for key in conflict_keys
            ):
                # Targeted-column merge, NOT a full-row replace (OMN-15598): a
                # column present on `existing` but absent from `row` keeps its
                # stored value, matching the real adapters' ON CONFLICT DO
                # UPDATE SET semantics (which name only the incoming columns).
                rows[i] = {**existing, **row}
                self.upsert_count += 1
                return True

        rows.append(row)
        self.upsert_count += 1
        return True

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        rows = self.tables.get(table, [])
        if not filters:
            return list(rows)

        result = []
        for row in rows:
            if all(row.get(k) == v for k, v in filters.items()):
                result.append(row)
        return result

    def has_table(self, table: str) -> bool:
        """Return True when the in-memory fixture has an explicit table."""
        return table in self.tables


__all__: list[str] = [
    "DatabaseAdapter",
    "InmemoryDatabaseAdapter",
    "ProtocolProjectionDatabaseSync",
]
