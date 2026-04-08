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
        conflict_val = row.get(conflict_key)

        # Find existing row with same conflict key value
        for i, existing in enumerate(rows):
            if existing.get(conflict_key) == conflict_val:
                rows[i] = row
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


__all__: list[str] = [
    "DatabaseAdapter",
    "InmemoryDatabaseAdapter",
    "ProtocolProjectionDatabaseSync",
]
