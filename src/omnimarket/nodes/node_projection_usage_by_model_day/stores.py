# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Migration 0004's columns on the store-neutral usage write (OMN-25861).

The node that owns 0004 owns how its two columns are written. The Postgres probe is a
mixin of ``PostgresSyncProjectionAdapter``, as ``node_projection_tenant_credentials``
gives that adapter its credential methods, so the shared adapter module does not grow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    # Annotations only: the adapter module imports this one for its mixin, so
    # nothing here may import, at load, a module that leads back to it.
    from omnimarket.nodes.node_projection_usage_by_model_day.models import (
        ModelUsageCallDelta,
    )
    from omnimarket.projection.protocol_database import DatabaseAdapter

    class _PgHost(Protocol):
        """What the Postgres mixin calls on ``PostgresSyncProjectionAdapter``."""

        def _connect(self) -> Any: ...

    _PgBase = _PgHost
else:
    _PgBase = object


class UsagePgStore(_PgBase):
    """The Postgres sync adapter's answer to which of 0004's columns exist."""

    def has_column(self, table: str, column: str) -> bool:
        """Whether ``table`` has ``column`` on this database.

        The migrations own this schema and a write naming a column they have not
        added fails, so a writer that must keep working while a migration is still
        on its way asks first. Read from the catalogue; the table resolves through
        the same search_path every write here uses.
        """
        # Imported here: that module imports this one for the mixin.
        from omnimarket.projection.postgres_sync_database import _validate_identifier

        table_ident = _validate_identifier(table, kind="table")
        column_ident = _validate_identifier(column, kind="column")
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_attribute "
                    "WHERE attrelid = to_regclass(%s) AND attname = %s "
                    "AND NOT attisdropped)",
                    (table_ident, column_ident),
                )
                row = cur.fetchone()
                return bool(row and row[0])
        finally:
            conn.close()


def _column_in_force(db: DatabaseAdapter, table: str, column: str) -> bool:
    """Whether ``db`` has migration 0004's ``column``.

    A store that answers ``has_column`` (the Postgres sync adapter, whose schema the
    migrations own) is asked, so the write follows the columns in force as the async
    writer does; a store that does not (the local SQLite file, which adds a column
    a row brings, and the in-memory double) takes every column.
    """
    has_column = getattr(db, "has_column", None)
    return True if has_column is None else bool(has_column(table, column))


def backends_of_calls(model_id: str, calls: list[dict[str, object]]) -> str | None:
    """A row's backends as stored: a JSON array, or None when any call stated none.

    The writer's rule (_BACKENDS_OF_CALLS) on the local store: the sorted distinct
    backends the calls were reached through, where the backend is set and differs
    from the model, so a direct call adds nothing. A call that stated no backend,
    including one stored before the column existed, is unknown, so the row's list is
    unknown too (JG-19), never a partial list or an empty one.
    """
    stated = [row.get("backend") for row in calls]
    if any(backend is None for backend in stated):
        return None
    return json.dumps(
        sorted({str(b) for b in stated if b and b != model_id}),
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class UsageBackendColumns:
    """Which of 0004's two columns a store has.

    0004's two ALTERs can commit separately, so a call's backend is stored once its
    column exists, and a row's backends are counted once the day table has its
    column too.
    """

    call: bool
    row: bool

    @classmethod
    def of(
        cls, db: DatabaseAdapter, calls_table: str, aggregate_table: str
    ) -> UsageBackendColumns:
        call = _column_in_force(db, calls_table, "backend")
        return cls(call, call and _column_in_force(db, aggregate_table, "backends"))

    def call_row(
        self, delta: ModelUsageCallDelta, key: dict[str, object], ingested_at: str
    ) -> dict[str, object]:
        """The calls-table row for ``delta``, with its backend where in force."""
        return {
            **key,
            "call_id": delta.call_id,
            "input_tokens": delta.input_tokens,
            "output_tokens": delta.output_tokens,
            "cost_usd": delta.cost_usd,
            "usage_source": delta.usage_source.value,
            **({"backend": delta.backend} if self.call else {}),
            "occurred_at": delta.occurred_at.isoformat(),
            "ingested_at": ingested_at,
        }

    def row_backends(
        self, model_id: str, calls: list[dict[str, object]]
    ) -> dict[str, object]:
        """The day row's backends field where in force, else nothing."""
        return {"backends": backends_of_calls(model_id, calls)} if self.row else {}


__all__ = [
    "UsageBackendColumns",
    "UsagePgStore",
    "backends_of_calls",
]
