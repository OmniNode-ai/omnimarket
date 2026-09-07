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
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        """Query rows from a table with optional filters, ordering and bound.

        OMN-17888. ``order_by`` / ``descending`` / ``limit`` are the minimal
        typed capability a projection handler needs to ask a BOUNDED question --
        "the latest row of this session" -- instead of reading every row it
        might be and sorting them in Python. Without it,
        ``HandlerProjectionSessionReplay.project`` re-read the WHOLE session on
        EVERY event, which is O(n^2) in session length and made the busiest
        live session (100,441 rows at 2026-09-07T15:53Z, growing ~3,029/hour)
        both the slowest and, once the runtime seam grew a 125,000-row budget,
        about eight hours from being refused outright.

        All three are keyword-only with defaults that reproduce the previous
        behaviour exactly, so every existing caller and every existing double is
        unchanged. ``descending`` without ``order_by`` is a REFUSAL, not a
        silent no-op: silently ignoring it hands a caller that asked for the
        newest row the oldest one, which is a wrong answer wearing the shape of
        a right one.
        """
        ...


# Backward-compat alias — existing code imports DatabaseAdapter
DatabaseAdapter = ProtocolProjectionDatabaseSync


def _order_key(value: object) -> tuple[int, float, str]:
    """Total order over heterogeneous stored values for the in-memory double.

    Real columns are typed, so a real ORDER BY never has to compare an int with
    a string. The in-memory fixture has no schema, so it needs a total order to
    avoid a ``TypeError`` that would only ever be an artefact of the double.
    Numbers sort before strings; anything else sorts first.
    """
    if isinstance(value, bool):
        return (1, float(value), "")
    if isinstance(value, (int, float)):
        return (1, float(value), "")
    if isinstance(value, str):
        return (2, 0.0, value)
    return (0, 0.0, "")


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
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        """In-memory equivalent of the real adapters' ORDER BY / LIMIT.

        OMN-17888. The refusals are reproduced here deliberately, not just the
        happy path: a double that quietly accepted ``descending=True`` with no
        ``order_by``, or a zero ``limit``, would let a caller ship a query the
        real adapters reject, which is exactly the class of vacuous test the
        OMN-15598 upsert-parity fix was written about.
        """
        if order_by is None and descending:
            raise ValueError("descending requires an order_by column")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise ValueError(f"limit must be a positive int, got {limit!r}")

        rows = self.tables.get(table, [])
        result = [
            row
            for row in rows
            if not filters or all(row.get(k) == v for k, v in filters.items())
        ]
        if order_by is not None:
            # A row missing the ordering column sorts as if it held the column's
            # zero, matching how the real relations declare `sequence NOT NULL
            # DEFAULT 0` rather than inventing an ordering the store has not got.
            result.sort(
                key=lambda row: _order_key(row.get(order_by)), reverse=descending
            )
        if limit is not None:
            result = result[:limit]
        return result

    def has_table(self, table: str) -> bool:
        """Return True when the in-memory fixture has an explicit table."""
        return table in self.tables


__all__: list[str] = [
    "DatabaseAdapter",
    "InmemoryDatabaseAdapter",
    "ProtocolProjectionDatabaseSync",
]
