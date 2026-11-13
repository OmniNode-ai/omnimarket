# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The table-backed conditional store of the fill's rows: the ``floor_fill_state`` table of omnibase_infra.

It implements ``ProtocolFloorStateStore`` over the table migration 113 creates (key ``scope``, sequence column
``version``). The write is one statement: an insert that does nothing when the row exists (version 0 is "no row"), or
an update that lands only when the stored version is the one the writer read. A lost race is a refused write
(``False``), never a lock. A store that cannot be reached or read raises ``FloorFillUnreadableError``: an absent row
answers ``ModelStateRead(None, 0)``, an unreadable store never does.

The columns the runtime adapter derives from the payload (``tenant_id``, ``state``, ``in_flight``) take the
migration's own degraded values (empty, empty, false), as the migration documents for a row written without them.
"""

from __future__ import annotations

import importlib
from contextlib import closing
from typing import Any

from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_store_settings import (
    ModelFloorFillStoreSettings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelStateRead,
)

# psycopg2 ships no type stubs; load it as Any rather than suppress the import.
psycopg2: Any = importlib.import_module("psycopg2")
sql: Any = importlib.import_module("psycopg2.sql")


class PostgresFloorStateStore:
    """Versioned rows in the ``floor_fill_state`` table; one connection per call, so instances share nothing."""

    def __init__(self, settings: ModelFloorFillStoreSettings) -> None:
        self._settings = settings
        self._relation = sql.Identifier(settings.schema_name, settings.table_name)

    def _connect(self) -> Any:
        # This class IS the ProtocolFloorStateStore I/O boundary over the table (the connection is its purpose, the DSN
        # and timeout are injected settings), the same sanctioned per-line boundary mark as
        # omnimarket.projection.postgres_sync_database: not a path-allowlist broadening (OMN-23002).
        connection = psycopg2.connect(  # no-contract-check: store boundary, DSN and timeout injected
            self._settings.dsn.get_secret_value(),
            connect_timeout=self._settings.connect_timeout_seconds,
        )
        connection.autocommit = True
        return connection

    @staticmethod
    def _unreadable(error: Exception) -> FloorFillUnreadableError:
        detail = (str(error).strip().splitlines() or [""])[0]
        return FloorFillUnreadableError(
            f"store-unreachable:{type(error).__name__}:{detail}"
        )

    def _update_statement(self) -> Any:
        """The conditional update: it lands only on the version the writer read."""
        return sql.SQL(
            "UPDATE {relation} SET payload = %s::jsonb, version = %s WHERE scope = %s AND version = %s"
        ).format(relation=self._relation)

    def load(self, key: str) -> ModelStateRead:
        try:
            with closing(self._connect()) as connection, connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "SELECT payload::text, version FROM {relation} WHERE scope = %s"
                    ).format(relation=self._relation),
                    (key,),
                )
                row = cursor.fetchone()
        except psycopg2.Error as error:
            raise self._unreadable(error) from error
        if row is None:
            return ModelStateRead(payload=None, version=0)
        return ModelStateRead(payload=row[0], version=row[1])

    def compare_and_set(
        self, key: str, expected_version: int, new_version: int, payload: str
    ) -> bool:
        try:
            with closing(self._connect()) as connection, connection.cursor() as cursor:
                if expected_version == 0:
                    cursor.execute(
                        sql.SQL(
                            "INSERT INTO {relation} (scope, tenant_id, state, in_flight, payload, version) "
                            "VALUES (%s, '', '', FALSE, %s::jsonb, %s) ON CONFLICT (scope) DO NOTHING"
                        ).format(relation=self._relation),
                        (key, payload, new_version),
                    )
                else:
                    cursor.execute(
                        self._update_statement(),
                        (payload, new_version, key, expected_version),
                    )
                return bool(cursor.rowcount == 1)
        except psycopg2.Error as error:
            raise self._unreadable(error) from error


__all__: list[str] = ["PostgresFloorStateStore"]
