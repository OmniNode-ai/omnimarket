# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only Postgres boundary: hook events per host, actor and time from claude_hook_events (OMN-26658)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_hook_install_emission_compute.models import (
    ModelHookInstallObservedEvent,
)

DSN_ENV = "OMNINODE_INTERNAL_DB_URL"
_QUERY = (
    "SELECT host, payload->>'actor', hook_event_name, emitted_at "
    "FROM omninode_internal.claude_hook_events "
    "WHERE emitted_at >= %s AND host IS NOT NULL"
)


class PostgresHookInstallEventsReader:
    """One read-only connection, opened on first use and dropped after an error."""

    def __init__(self, *, dsn: str) -> None:
        if not dsn.strip():
            raise RuntimeError("hook events reads DSN is empty")
        self._dsn = dsn
        self._conn: Any | None = None

    def _connect(self) -> Any:
        if self._conn is not None and getattr(self._conn, "closed", 0) == 0:
            return self._conn
        from omnimarket.projection.postgres_read_database import connect_read_only

        self._conn = connect_read_only(self._dsn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            connection, self._conn = self._conn, None
            connection.close()

    def read(self, since: datetime) -> Sequence[ModelHookInstallObservedEvent]:
        try:
            with self._connect().cursor() as cursor:
                cursor.execute(_QUERY, (since,))
                fetched = cursor.fetchall()
            if fetched and all(row[1] is None for row in fetched):
                # The writer does not project the wire event's `actor` today (no column; the payload does not carry
                # it), so every row would read as `unknown` and every install as silent. Refuse instead.
                raise RuntimeError(
                    "claude_hook_events rows carry no actor: the projection does not store it"
                )
            return tuple(
                ModelHookInstallObservedEvent(
                    host=row[0],
                    actor=row[1] if row[1] in ("claude", "codex") else "unknown",
                    hook_event_name=row[2],
                    emitted_at=row[3],
                )
                for row in fetched
            )
        except Exception:
            self.close()
            raise
