# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store-neutral fold of ``local-identity-minted`` events into one row per tenant.

The identity was minted once, so its row keeps the earliest ``minted_at`` any
mint event announced for that tenant. A replayed event, or a later announcement
of the same identity, writes nothing; an earlier one moves ``minted_at`` back.

``plan_local_identity_write`` is the whole decision, pure, over the rows already
stored; the local SQLite path applies its plan here and the Postgres writer
that follows applies the same plan, so the two stores cannot fold differently.
The read and the write run in one write transaction when the store offers one.

It lives beside the event model, not as a handler, until the projection node's
contract lands and binds a handler that calls it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.models.model_local_identity_minted import ModelLocalIdentityMinted
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter

LOCAL_IDENTITY_TABLE = "local_identity"
LOCAL_IDENTITY_KEY = "tenant_id"
#: The local SQLite store step that creates the table. A store step, not a
#: statement on every connect: the cursor seed is a write, so a read-only store,
#: or a reader on a busy one, must not run it.
LOCAL_IDENTITY_SQLITE_STEP = "omn19986_local_identity"

# One row per tenant. Its cursor is stamped from a one-row sequence on insert
# and update, as local_environment's is, so a changed row sorts after the rest.
_STAMP = """
    UPDATE local_identity_projection_cursor_seq SET last_value = last_value + 1;
    UPDATE local_identity
    SET projection_cursor = (SELECT last_value FROM local_identity_projection_cursor_seq)
    WHERE rowid = NEW.rowid;
"""
_SQLITE_DDL = (
    """
CREATE TABLE IF NOT EXISTS local_identity (
    tenant_id TEXT NOT NULL PRIMARY KEY,
    minted_at TEXT NOT NULL,
    event_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    projection_cursor INTEGER
)
""",
    """
CREATE TABLE IF NOT EXISTS local_identity_projection_cursor_seq (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_value INTEGER NOT NULL
)
""",
    "INSERT OR IGNORE INTO local_identity_projection_cursor_seq (id, last_value) "
    "VALUES (1, 0)",
    "CREATE TRIGGER IF NOT EXISTS local_identity_cursor_on_insert "
    "AFTER INSERT ON local_identity BEGIN" + _STAMP + "END",
    "CREATE TRIGGER IF NOT EXISTS local_identity_cursor_on_update "
    "AFTER UPDATE OF updated_at ON local_identity BEGIN" + _STAMP + "END",
)


def _create_local_identity_sqlite(conn: sqlite3.Connection) -> None:
    for statement in _SQLITE_DDL:
        conn.execute(statement)


def local_identity_sqlite_steps(
    conn: sqlite3.Connection, recorded: Callable[[sqlite3.Connection, str], bool]
) -> list[tuple[str, Callable[[sqlite3.Connection], None], str]]:
    """The local SQLite store steps this module owns that ``conn`` has not recorded."""
    if recorded(conn, LOCAL_IDENTITY_SQLITE_STEP):
        return []
    return [
        (
            LOCAL_IDENTITY_SQLITE_STEP,
            _create_local_identity_sqlite,
            "created the local identity table and its cursor",
        )
    ]


def _as_datetime(value: object) -> datetime:
    """A stored time: an ISO string from SQLite, a datetime from Postgres."""
    parsed = (
        value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    )
    return parsed.astimezone(UTC)


def plan_local_identity_write(
    event: ModelLocalIdentityMinted,
    stored: Sequence[Mapping[str, object]],
    now: datetime,
) -> dict[str, object] | None:
    """The row to upsert for ``event``, or ``None`` when it changes nothing."""
    tenant = str(event.tenant_id)
    current = next((row for row in stored if str(row.get("tenant_id")) == tenant), None)
    if current is not None:
        if str(current.get("event_id")) == str(event.event_id):
            return None
        if _as_datetime(current["minted_at"]) <= event.minted_at.astimezone(UTC):
            return None
    return {
        "tenant_id": tenant,
        "minted_at": event.minted_at.astimezone(UTC).isoformat(),
        "event_id": str(event.event_id),
        "updated_at": now.astimezone(UTC).isoformat(),
    }


def apply_local_identity_minted(
    event: ModelLocalIdentityMinted, db: DatabaseAdapter
) -> bool:
    """Fold one mint event through the portable adapter; ``False`` when nothing changed."""
    transaction = getattr(db, "write_transaction", None)
    if transaction is None:
        return _apply(event, db)
    with transaction() as tx:
        return _apply(event, tx)


def _apply(event: ModelLocalIdentityMinted, db: DatabaseAdapter) -> bool:
    stored = db.query(LOCAL_IDENTITY_TABLE, {"tenant_id": str(event.tenant_id)})
    row = plan_local_identity_write(event, stored, datetime.now(UTC))
    if row is None:
        return False
    db.upsert(LOCAL_IDENTITY_TABLE, LOCAL_IDENTITY_KEY, row)
    return True


__all__ = [
    "LOCAL_IDENTITY_KEY",
    "LOCAL_IDENTITY_SQLITE_STEP",
    "LOCAL_IDENTITY_TABLE",
    "apply_local_identity_minted",
    "local_identity_sqlite_steps",
    "plan_local_identity_write",
]
