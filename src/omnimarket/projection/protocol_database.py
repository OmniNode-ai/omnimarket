"""ProtocolProjectionDatabaseSync — sync projection database protocol.

Production: asyncpg UPSERT into Postgres on .201:5436.
Tests: InmemoryDatabaseAdapter that records rows for assertion.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from threading import RLock
from types import MappingProxyType
from typing import cast

from omnibase_core.models.projection.model_upsert_plan import (
    SQL_EXPRESSION_SENTINEL_PREFIX,
    build_upsert_plan,
)

from omnimarket.handlers.handler_projection_derived_write import (
    HandlerProjectionDerivedWrite,
)
from omnimarket.models.model_credential_route_write import ModelCredentialRouteWrite
from omnimarket.protocols.protocol_projection_database_sync import (
    DatabaseAdapter,
    ProtocolProjectionAttestedWrite,
    ProtocolProjectionDatabaseSync,
)


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
        self._credential_route_lock = RLock()

    def replace_credential_route(
        self,
        plan: ModelCredentialRouteWrite,
    ) -> tuple[bool, int | None]:
        row = dict(plan.row)
        expected_version = plan.expected_version
        with self._credential_route_lock:
            rows = self.query(
                "delegation_routing_tenant_overlay",
                {
                    "tenant_id": row["tenant_id"],
                    "task_type": row["task_type"],
                },
            )
            # Schema versions are integers; generic projection rows erase that type.
            current = int(cast(int, rows[0].get("version", 1))) if rows else None
            revoked = self.query(
                "tenant_inference_credentials", {"api_key_ref": row["secret_ref"]}
            )
            if (revoked and revoked[0].get("revoked_at") is not None) or (
                expected_version is not None and current != expected_version
            ):
                return False, current
            if row.get("secret_ref") is not None and not self.query(
                "tenant_inference_credentials",
                {"tenant_id": row["tenant_id"], "api_key_ref": row["secret_ref"]},
            ):
                raise RuntimeError("credential model target is unavailable")
            self.upsert_returning(
                "delegation_routing_tenant_overlay",
                "tenant_id,task_type",
                {
                    **row,
                    "version": 1 if current is None else current + 1,
                },
                insert_only_columns=frozenset({"created_at"}),
            )
            if row.get("secret_ref") is not None:
                self.upsert_returning(
                    "tenant_inference_credentials",
                    "api_key_ref",
                    {
                        "api_key_ref": row["secret_ref"],
                        # An empty choice is an unknown one, as on SQLite.
                        "models": list(cast(list[str], row["models"])) or None,
                    },
                )
            return True, 1 if current is None else current + 1

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
        derive: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        input_columns: Sequence[str] = (),
        derived_columns: Sequence[str] = (),
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
        derivation = HandlerProjectionDerivedWrite.prepare(
            plan,
            row=row,
            derive=derive,
            input_columns=input_columns,
            derived_columns=derived_columns,
            insert_only_columns=insert_only_columns,
        )
        rows = self.tables.setdefault(table, [])
        previous = [dict(record) for record in rows] if derivation is not None else []

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

        if derivation is not None:
            try:
                derived = HandlerProjectionDerivedWrite.derived_values(
                    derivation, stored
                )
                stored.update(derived)
            except BaseException:
                self.tables[table] = previous
                raise
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
