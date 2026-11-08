# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres overlay store: the backend of a runtime, the one component that holds a database URL.

A host never selects this backend: it reads the snapshot topic the projection publishes. One row per
overlay id and digest keeps every version; the row asserted last is the current one. The document
column is JSON and not JSONB, because JSONB reorders keys and a table file's key order is part of how
the flat parsers read it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from omnibase_core.types import JsonType

from omnimarket.models.overlay_store import ModelOverlayStoreRecord

STORE_TABLE: Final = "omninode_internal.overlay_store"

_LOCK = "SELECT pg_advisory_xact_lock(hashtext($1))"
_READ_NEWEST = f"""
    SELECT max(last_loaded_at) AS last_loaded_at FROM {STORE_TABLE} WHERE overlay_id = $1
"""
_UPSERT = f"""
    INSERT INTO {STORE_TABLE}
        (overlay_id, digest, document, source_ref, loaded_by, first_loaded_at, last_loaded_at)
    VALUES ($1, $2, $3::json, $4, $5, $6, $6)
    ON CONFLICT (overlay_id, digest) DO UPDATE SET
        last_loaded_at = EXCLUDED.last_loaded_at,
        source_ref = EXCLUDED.source_ref,
        loaded_by = EXCLUDED.loaded_by,
        projected_at = now()
"""
_READ_CURRENT = f"""
    SELECT overlay_id, digest, document, source_ref, loaded_by, last_loaded_at
    FROM {STORE_TABLE} WHERE overlay_id = $1
    ORDER BY last_loaded_at DESC LIMIT 1
"""


class PostgresOverlayStore:
    """Reads and writes through a pool the runtime's topology binding opened."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def write_version(self, record: Mapping[str, JsonType]) -> bool:
        loaded = ModelOverlayStoreRecord.model_validate(record)
        document = json.dumps(loaded.document)
        fields = (
            loaded.overlay_id,
            loaded.digest,
            document,
            loaded.source_ref,
            loaded.loaded_by,
            loaded.loaded_at,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(_LOCK, f"overlay_store:{loaded.overlay_id}")
            newest = await conn.fetchval(_READ_NEWEST, loaded.overlay_id)
            if newest is not None and newest >= loaded.loaded_at:
                return False
            await conn.execute(_UPSERT, *fields)
            return True

    async def read_current(self, overlay_id: str) -> Mapping[str, JsonType] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_READ_CURRENT, overlay_id)
        if row is None:
            return None
        return ModelOverlayStoreRecord(
            overlay_id=str(row["overlay_id"]),
            digest=str(row["digest"]),
            document=json.loads(row["document"]),
            source_ref=str(row["source_ref"]),
            loaded_by=str(row["loaded_by"]),
            loaded_at=row["last_loaded_at"],
        ).model_dump(mode="json")


__all__ = ["STORE_TABLE", "PostgresOverlayStore"]
