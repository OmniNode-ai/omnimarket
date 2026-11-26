# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend profile of the floor panel's row reader: one read-only asyncpg read (OMN-19998).

The DSN is read at read time from the environment variable the constructor names (never a
literal), and the table name is a validated identifier interpolated into the one SELECT — every
value is bound. The durable payload carries the three well-known keys omnibase_infra's state_io
wiring injects (the same keys ``node_work_state_panel_reducer.state_codec`` strips); they come
off before the row validates, because ``ModelWorkStatePanelRow`` declares ``extra="forbid"``.
Read-only: this reader runs one SELECT and never writes.
"""

from __future__ import annotations

import json
import os
import re

from omnimarket.models.floor_panel.model_floor_panel_stored_row import (
    ModelFloorPanelStoredRow,
)
from omnimarket.models.work_state_panel.model_work_state_panel_row import (
    ModelWorkStatePanelRow,
)

_TABLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")

_STATE_KEY = "state"
_IN_FLIGHT_KEY = "in_flight"
_TENANT_ID_KEY = "tenant_id"
_INJECTED_KEYS = (_STATE_KEY, _IN_FLIGHT_KEY, _TENANT_ID_KEY)


def _decode_payload(raw: str) -> ModelWorkStatePanelRow:
    """The stored JSON back into a panel row: the injected state_io keys come off, then it validates."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(
            "work_state_panel row payload is not a JSON object "
            f"(got {type(payload).__name__})"
        )
    for key in _INJECTED_KEYS:
        payload.pop(key, None)
    return ModelWorkStatePanelRow(**payload)


class AsyncpgFloorPanelRowReader:
    """The real-backend profile: one row per projector, read read-only from the panel table.

    ``dsn_env`` names the environment variable the DSN is read from at read time; the reader
    holds the variable's name, never a DSN. ``table`` is validated as a lower-case identifier
    before it is interpolated; the projector key is a bound parameter.
    """

    def __init__(self, dsn_env: str, table: str, timeout_s: int) -> None:
        self._dsn_env = dsn_env
        self._timeout_s = timeout_s
        if not _TABLE_PATTERN.fullmatch(table):
            raise ValueError(f"table {table!r} must match ^[a-z_][a-z0-9_]*$")
        self._table = table

    async def read(self, key: str) -> ModelFloorPanelStoredRow | None:
        dsn = os.environ.get(self._dsn_env, "")
        if not dsn:
            raise RuntimeError(
                f"{self._dsn_env} is not set; the panel rows have no store route"
            )
        import asyncpg

        connection = await asyncpg.connect(  # no-contract-check: read-only panel row read, the env-named DSN is the boundary
            dsn, timeout=self._timeout_s, command_timeout=self._timeout_s
        )
        try:
            record = await connection.fetchrow(
                f"SELECT payload, version FROM {self._table} WHERE projector = $1",
                key,
            )
        finally:
            await connection.close()
        if record is None:
            return None
        return ModelFloorPanelStoredRow(
            row=_decode_payload(record["payload"]),
            version=int(record["version"]),
        )


__all__ = ["AsyncpgFloorPanelRowReader"]
