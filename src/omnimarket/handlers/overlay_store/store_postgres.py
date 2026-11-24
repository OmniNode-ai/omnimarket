# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres overlay store: the backend of a runtime, the one component that holds a database URL.

A host never selects this backend: it reads the snapshot topic the projection publishes. One row per
overlay id and digest keeps every version; the version with the highest revision is the current one. The document
column is JSON and not JSONB, because JSONB reorders keys and a table file's key order is part of how
the flat parsers read it.

One write is one transaction under the overlay's advisory lock. A load is applied when it is newer than what the store
holds and is numbered when it changes the document. A set is applied only when the revision it was built on is the
current one; its record, its provenance, its write id and its audit row commit together, so an audit insert that fails
takes the record with it, and a write id that was applied is answered and never applied twice.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from omnibase_core.types import JsonType

from omnimarket.models.overlay_store import (
    EnumOverlayValueSource,
    EnumOverlayWriteOutcome,
    ModelOverlayPathProvenance,
    ModelOverlaySettingAudit,
    ModelOverlayStoreRecord,
    ModelOverlayStoreWrite,
    ModelOverlayWriteReply,
)

STORE_TABLE: Final = "omninode_internal.overlay_store"
REVISION_TABLE: Final = "omninode_internal.overlay_store_revision"
WRITE_TABLE: Final = "omninode_internal.overlay_store_write"
PROVENANCE_TABLE: Final = "omninode_internal.overlay_store_provenance"
AUDIT_TABLE: Final = "omninode_internal.overlay_store_audit"

_LOCK = "SELECT pg_advisory_xact_lock(hashtext($1))"
_READ_NEWEST = f"""
    SELECT max(last_loaded_at) AS last_loaded_at FROM {STORE_TABLE} WHERE overlay_id = $1
"""
_READ_REVISION = f"SELECT revision FROM {REVISION_TABLE} WHERE overlay_id = $1"
_READ_WRITE = f"SELECT revision FROM {WRITE_TABLE} WHERE write_id = $1"
_READ_HELD = f"""
    SELECT digest, operator_paths FROM {STORE_TABLE}
    WHERE overlay_id = $1 ORDER BY revision DESC, last_loaded_at DESC LIMIT 1
"""
_UPSERT = f"""
    INSERT INTO {STORE_TABLE}
        (overlay_id, digest, document, source_ref, loaded_by, first_loaded_at, last_loaded_at, revision, operator_paths)
    VALUES ($1, $2, $3::json, $4, $5, $6, $6, $7, $8)
    ON CONFLICT (overlay_id, digest) DO UPDATE SET
        last_loaded_at = EXCLUDED.last_loaded_at,
        source_ref = EXCLUDED.source_ref,
        loaded_by = EXCLUDED.loaded_by,
        revision = EXCLUDED.revision,
        operator_paths = EXCLUDED.operator_paths,
        projected_at = now()
"""
_UPSERT_REVISION = f"""
    INSERT INTO {REVISION_TABLE} (overlay_id, revision) VALUES ($1, $2)
    ON CONFLICT (overlay_id) DO UPDATE SET revision = EXCLUDED.revision, updated_at = now()
"""
_INSERT_WRITE = f"""
    INSERT INTO {WRITE_TABLE} (write_id, overlay_id, revision, based_on_revision) VALUES ($1, $2, $3, $4)
"""
_INSERT_AUDIT = f"""
    INSERT INTO {AUDIT_TABLE} (write_id, overlay_id, revision, key, setting_path, actor, audit)
    VALUES ($1, $2, $3, $4, $5, $6, $7::json)
"""
_UPSERT_PROVENANCE = f"""
    INSERT INTO {PROVENANCE_TABLE} (overlay_id, path, source, actor, revision, write_id)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (overlay_id, path) DO UPDATE SET
        source = EXCLUDED.source, actor = EXCLUDED.actor,
        revision = EXCLUDED.revision, write_id = EXCLUDED.write_id
"""
_READ_PROVENANCE = f"""
    SELECT path, source, actor, revision, write_id FROM {PROVENANCE_TABLE} WHERE overlay_id = $1
"""
_READ_PENDING_AUDIT = f"""
    SELECT audit FROM {AUDIT_TABLE} WHERE write_id = $1 AND published_at IS NULL
"""
_MARK_AUDIT_PUBLISHED = f"""
    UPDATE {AUDIT_TABLE} SET published_at = now() WHERE write_id = $1 AND published_at IS NULL
"""
_READ_CURRENT = f"""
    SELECT overlay_id, digest, document, source_ref, loaded_by, last_loaded_at, revision, operator_paths
    FROM {STORE_TABLE} WHERE overlay_id = $1
    ORDER BY revision DESC, last_loaded_at DESC LIMIT 1
"""


class PostgresOverlayStore:
    """Reads and writes through a pool the runtime's topology binding opened."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def write_version(self, record: Mapping[str, JsonType]) -> bool:
        """Apply one load; True when the store took it."""
        reply = await self.apply_write(ModelOverlayStoreWrite.model_validate(record))
        return not reply.replayed

    async def apply_write(
        self, write: ModelOverlayStoreWrite
    ) -> ModelOverlayWriteReply:
        """Apply one load or one set in one transaction; a failure of any statement rolls the whole write back."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(_LOCK, f"overlay_store:{write.overlay_id}")
            if write.audit is None:
                return await self._apply_load(conn, write)
            return await self._apply_set(conn, write, write.audit)

    async def _apply_load(
        self, conn: Any, write: ModelOverlayStoreWrite
    ) -> ModelOverlayWriteReply:
        revision = int(await conn.fetchval(_READ_REVISION, write.overlay_id) or 0)
        newest = await conn.fetchval(_READ_NEWEST, write.overlay_id)
        if newest is not None and newest >= write.loaded_at:
            return ModelOverlayWriteReply(
                overlay_id=write.overlay_id,
                outcome=EnumOverlayWriteOutcome.COMMITTED,
                revision=revision,
                replayed=True,
            )
        held = await conn.fetchrow(_READ_HELD, write.overlay_id)
        if held is None or held["digest"] != write.digest:
            revision += 1
        # A loader's load names no operator path: the paths the store holds stay marked.
        paths = list(held["operator_paths"]) if held is not None else []
        await self._put_version(conn, write, revision, paths)
        await conn.execute(_UPSERT_REVISION, write.overlay_id, revision)
        return ModelOverlayWriteReply(
            overlay_id=write.overlay_id,
            outcome=EnumOverlayWriteOutcome.COMMITTED,
            revision=revision,
        )

    async def _apply_set(
        self, conn: Any, write: ModelOverlayStoreWrite, audit: ModelOverlaySettingAudit
    ) -> ModelOverlayWriteReply:
        write_id = str(write.write_id)
        applied = await conn.fetchval(_READ_WRITE, write_id)
        if applied is not None:
            return ModelOverlayWriteReply(
                write_id=write_id,
                overlay_id=write.overlay_id,
                outcome=EnumOverlayWriteOutcome.COMMITTED,
                revision=int(applied),
                replayed=True,
            )
        revision = int(await conn.fetchval(_READ_REVISION, write.overlay_id) or 0)
        if write.based_on_revision != revision:
            return ModelOverlayWriteReply(
                write_id=write_id,
                overlay_id=write.overlay_id,
                outcome=EnumOverlayWriteOutcome.CONFLICT,
                revision=revision,
            )
        new_revision = revision + 1
        held = await conn.fetchrow(_READ_HELD, write.overlay_id)
        paths = sorted(
            {
                *(held["operator_paths"] if held is not None else ()),
                *write.operator_paths,
            }
        )
        await self._put_version(conn, write, new_revision, paths)
        await conn.execute(
            _INSERT_WRITE, write_id, write.overlay_id, new_revision, revision
        )
        await conn.execute(
            _INSERT_AUDIT,
            write_id,
            write.overlay_id,
            new_revision,
            audit.key,
            audit.setting_path,
            audit.actor,
            audit.model_dump_json(),
        )
        await conn.execute(
            _UPSERT_PROVENANCE,
            write.overlay_id,
            audit.setting_path,
            EnumOverlayValueSource.OPERATOR.value,
            audit.actor,
            new_revision,
            write_id,
        )
        await conn.execute(_UPSERT_REVISION, write.overlay_id, new_revision)
        return ModelOverlayWriteReply(
            write_id=write_id,
            overlay_id=write.overlay_id,
            outcome=EnumOverlayWriteOutcome.COMMITTED,
            revision=new_revision,
        )

    async def _put_version(
        self,
        conn: Any,
        write: ModelOverlayStoreWrite,
        revision: int,
        paths: list[str],
    ) -> None:
        await conn.execute(
            _UPSERT,
            write.overlay_id,
            write.digest,
            json.dumps(write.document),
            write.source_ref,
            write.loaded_by,
            write.loaded_at,
            revision,
            paths,
        )

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
            operator_paths=tuple(row["operator_paths"]),
            revision=int(row["revision"]),
        ).model_dump(mode="json")

    async def read_provenance(
        self, overlay_id: str
    ) -> dict[str, ModelOverlayPathProvenance]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_READ_PROVENANCE, overlay_id)
        return {
            str(row["path"]): ModelOverlayPathProvenance(
                source=EnumOverlayValueSource(row["source"]),
                actor=str(row["actor"]),
                revision=int(row["revision"]),
                write_id=str(row["write_id"]),
            )
            for row in rows
        }

    async def pending_audit(self, write_id: str) -> ModelOverlaySettingAudit | None:
        """The audit row of the write when the audit topic has not carried it yet."""
        async with self._pool.acquire() as conn:
            text = await conn.fetchval(_READ_PENDING_AUDIT, write_id)
        if text is None:
            return None
        return ModelOverlaySettingAudit.model_validate_json(text)

    async def mark_audit_published(self, write_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(_MARK_AUDIT_PUBLISHED, write_id)


__all__ = [
    "AUDIT_TABLE",
    "PROVENANCE_TABLE",
    "REVISION_TABLE",
    "STORE_TABLE",
    "WRITE_TABLE",
    "PostgresOverlayStore",
]
