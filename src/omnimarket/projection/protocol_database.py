"""ProtocolProjectionDatabaseSync — sync projection database protocol.

Production: asyncpg UPSERT into Postgres on .201:5436.
Tests: InmemoryDatabaseAdapter that records rows for assertion.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from omnimarket.projection.upsert_statement import (
    SQL_EXPRESSION_SENTINEL_PREFIX,
    build_upsert_plan,
)


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


@runtime_checkable
class ProtocolProjectionAttestedWrite(Protocol):
    """A store that can perform an ATTESTED write and report what it stored.

    OMN-18159. Deliberately a SEPARATE protocol rather than two more methods on
    :class:`ProtocolProjectionDatabaseSync`, for two reasons that both matter.

    The first is honest interface segregation: this is a strictly narrower
    capability, and not every sync target has it. SQLite has no connected
    principal to attest to, so a local evidence side-target cannot answer the
    question this protocol asks even in principle -- it can only record that
    an expression was requested.

    The second is blast radius. ``ProtocolProjectionDatabaseSync`` is
    ``runtime_checkable``, and a ``runtime_checkable`` protocol matches on
    method PRESENCE, so adding a method to it silently reclassifies every
    existing structural implementation as a non-implementation. Around twenty
    test doubles across this repo implement the three-argument ``upsert`` and
    nothing else; every projection handler that guards its injected adapter
    with ``isinstance(db, DatabaseAdapter)`` would have begun raising
    ``TypeError`` against all of them at once. Widening a structural protocol
    is not a local change, and the compiler does not tell you.

    A caller that NEEDS an attested write probes for this protocol and refuses
    loudly when it is absent, naming the adapter. That refusal is the correct
    behaviour rather than a defensive one: writing the row anyway would store
    a NULL ``writer_identity``, and a readback ordering on a column that is
    silently never stamped reports "nobody wrote this" in exactly the same
    shape as "this was written by an unscoped principal".
    """

    def upsert_returning(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
        *,
        tenant: str | None = None,
        insert_only_columns: frozenset[str] = frozenset(),
        sql_expression_columns: Mapping[str, str] = MappingProxyType({}),
        returning: Sequence[str] = (),
    ) -> list[dict[str, object]]:
        """UPSERT a row and return the rows the database actually stored.

        OMN-18159. The sync twin of ``DelegationProjectionRunner
        ._dynamic_upsert``, carrying the same four capabilities under the same
        parameter names and the same refusals, so the two write paths of
        ``node_projection_delegation`` cannot disagree about what a write
        means. :func:`omnimarket.projection.upsert_statement.build_upsert_plan`
        owns every decision; an implementation here owns only its driver's
        parameter binding.

        This is NOT a richer alias of :meth:`upsert`. The two answer different
        questions -- ``upsert`` answers "did the write succeed", this answers
        "what is now stored" -- and only the second can serve a column the
        DATABASE stamped, which is the property the writer attestation on
        ``delegation_events`` depends on. ``upsert`` keeps its ``bool`` because
        it has many callers for which the stored row is not the question, and
        widening its return type would change what ``if db.upsert(...)`` means
        at every one of them.

        ``tenant`` states which tenant THIS statement runs as, for the case
        where the row deliberately does not name one. Omitted, the tenant is
        resolved from ``row["tenant_id"]`` exactly as ``upsert`` resolves it,
        so one resolver serves both halves of the RLS policy comparison.

        ``returning`` names the columns to read back. Empty (the default)
        leaves the statement exactly as ``upsert`` would have composed it and
        returns an empty list. A statement that took a ``DO NOTHING`` arm also
        returns an empty list -- there is no stored row this call can describe,
        and inventing one would be a confident-empty failure inverted.
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
        self.upsert_returning(table, conflict_key, row)
        return True

    def upsert_returning(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
        *,
        tenant: str | None = None,
        insert_only_columns: frozenset[str] = frozenset(),
        sql_expression_columns: Mapping[str, str] = MappingProxyType({}),
        returning: Sequence[str] = (),
    ) -> list[dict[str, object]]:
        """OMN-18159. The double reproduces the REFUSALS, not only the merge.

        A double that quietly accepted an expression outside the closed set,
        or a column bound on both sides, would let a caller ship a statement
        the real stores refuse -- the vacuous-test class the OMN-15598
        upsert-parity fix was written about.

        ``tenant`` is accepted and ignored: this fixture has no row-level
        security to bind it to. Refusing it instead would force every test
        that exercises a tenant-scoped call site to special-case the double,
        and silently rewriting the row's tenant would be worse still.
        """
        plan = build_upsert_plan(
            table=table,
            conflict_key=conflict_key,
            row=row,
            insert_only_columns=insert_only_columns,
            sql_expression_columns=sql_expression_columns,
            returning=returning,
        )
        rows = self.tables.setdefault(table, [])

        # OMN-18159. An IDENTITY this fixture cannot evaluate is recorded as
        # an unmistakable sentinel rather than a plausible value: a double
        # that invented "postgres" for CURRENT_USER would make every
        # attestation assertion written against it pass while the production
        # statement bound a literal string.
        #
        # A CLOCK is different, and the distinction is deliberate rather than
        # an inconsistency. Nothing asserts that the database's clock said any
        # particular thing; what callers need from NOW() is a value that
        # ORDERS, because the per-row snapshot republish derives its ordering
        # token from written_at and a mutable-key exposure whose deltas all
        # carried the same token would drop every write after the first as a
        # stale replay. So this fixture gives NOW() a real, monotonic UTC
        # timestamp and keeps the sentinel for CURRENT_USER. Faking an
        # identity destroys the property under test; faking a clock does not.
        stamped: dict[str, object] = {
            column: (
                datetime.now(tz=UTC).isoformat()
                if expression == "NOW()"
                else f"{SQL_EXPRESSION_SENTINEL_PREFIX}{expression}>"
            )
            for column, expression in plan.expression_columns.items()
        }

        stored: dict[str, object] | None = None
        for index, existing in enumerate(rows):
            if all(
                key in existing and existing[key] == row[key]
                for key in plan.conflict_keys
            ):
                # Targeted-column merge, NOT a full-row replace (OMN-15598): a
                # column present on `existing` but absent from the incoming
                # assignments keeps its stored value, matching the real
                # adapters' ON CONFLICT DO UPDATE SET semantics. An insert-only
                # column is absent from those assignments, so an existing row
                # keeps its first value -- which is the whole point of naming
                # it insert-only.
                updated = dict(existing)
                for column, expression in plan.update_assignments:
                    updated[column] = (
                        row[column] if expression is None else stamped[column]
                    )
                rows[index] = updated
                stored = updated
                break
        if stored is None:
            stored = {**row, **stamped}
            rows.append(stored)

        self.upsert_count += 1
        if not plan.returning:
            return []
        return [{column: stored.get(column) for column in plan.returning}]

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
    "ProtocolProjectionAttestedWrite",
    "ProtocolProjectionDatabaseSync",
]
