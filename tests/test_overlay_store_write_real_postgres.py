# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26635: the store's revision check, provenance, audit row and write id against a real Postgres.

The unit tests prove the rules over an in-memory store; only a real connection shows that the migration is valid, the
statements bind, the per-overlay lock turns two racing writes into a commit and a conflict, and an audit insert that fails
rolls the record back with it. It SKIPS without a reachable database; signal ``INTEGRATION_POSTGRES_PASSWORD``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.handlers.overlay_store.store_postgres import PostgresOverlayStore
from omnimarket.models.overlay_store import (
    EnumOverlayWriteOutcome,
    ModelOverlayStoreWrite,
)
from tests.test_overlay_store_real_postgres import (
    MIGRATION_FILES,
    MIGRATIONS,
    _dsn,
)
from tests.unit.nodes.node_projection_overlay_store.support import (
    OVERLAY_ID,
    PATH_LANE,
    PATH_OWNER,
    default_load,
    set_write,
)

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _store() -> AsyncIterator[tuple[PostgresOverlayStore, asyncpg.Pool]]:
    if not os.environ.get("INTEGRATION_POSTGRES_PASSWORD"):
        pytest.skip(
            "no INTEGRATION_POSTGRES_PASSWORD for the settings store write path"
        )
    try:
        admin = await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for the settings store write path: {exc}")
    database = f"overlay_write_{uuid4().hex[:12]}"
    await admin.execute(f"CREATE DATABASE {database}")
    pool = await asyncpg.create_pool(_dsn(database), min_size=2, max_size=4)
    try:
        async with pool.acquire() as connection:
            await connection.execute("CREATE SCHEMA IF NOT EXISTS omninode_internal")
            for name in MIGRATION_FILES:
                await connection.execute(
                    (MIGRATIONS / name).read_text(encoding="utf-8")
                )
        yield PostgresOverlayStore(pool), pool
    finally:
        await pool.close()
        await admin.execute(f"DROP DATABASE {database}")
        await admin.close()


def _write(**kwargs: Any) -> ModelOverlayStoreWrite:
    return ModelOverlayStoreWrite.model_validate(set_write(**kwargs))


async def _count(pool: asyncpg.Pool, table: str, where: str = "true") -> int:
    return int(
        await pool.fetchval(
            f"SELECT count(*) FROM omninode_internal.{table} WHERE {where}"
        )
    )


@pytest.mark.asyncio
async def test_a_set_is_stored_with_provenance_a_revision_and_one_audit_row() -> None:
    async with _store() as (store, pool):
        await store.apply_write(ModelOverlayStoreWrite.model_validate(default_load()))
        reply = await store.apply_write(_write(based_on_revision=1))
        assert (reply.outcome, reply.revision) == (EnumOverlayWriteOutcome.COMMITTED, 2)

        current = await store.read_current(OVERLAY_ID)
        assert current is not None
        assert (current["revision"], current["operator_paths"]) == (2, [PATH_OWNER])
        provenance = await store.read_provenance(OVERLAY_ID)
        assert provenance[PATH_OWNER].source == "operator"
        assert provenance[PATH_OWNER].write_id == "w-set"
        assert await _count(pool, "overlay_store_audit", "write_id = 'w-set'") == 1
        pending = await store.pending_audit("w-set")
        assert pending is not None
        assert pending.actor == "ccp3-test"
        await store.mark_audit_published("w-set")
        assert await store.pending_audit("w-set") is None


@pytest.mark.asyncio
async def test_a_stale_revision_is_a_conflict_and_a_write_delivered_twice_applies_once() -> (
    None
):
    async with _store() as (store, pool):
        await store.apply_write(ModelOverlayStoreWrite.model_validate(default_load()))
        await store.apply_write(_write(based_on_revision=1))
        before = await pool.fetch(
            "SELECT digest, revision FROM omninode_internal.overlay_store ORDER BY digest"
        )

        late = await store.apply_write(
            _write(
                write_id="w-late",
                based_on_revision=1,
                key="alarm_lane",
                path=PATH_LANE,
                value="lane-z",
            )
        )
        assert (late.write_id, late.outcome, late.revision) == (
            "w-late",
            EnumOverlayWriteOutcome.CONFLICT,
            2,
        )
        again = await store.apply_write(_write(based_on_revision=1))
        assert (again.outcome, again.revision, again.replayed) == (
            EnumOverlayWriteOutcome.COMMITTED,
            2,
            True,
        )
        assert (
            await pool.fetch(
                "SELECT digest, revision FROM omninode_internal.overlay_store ORDER BY digest"
            )
            == before
        )
        assert await _count(pool, "overlay_store_audit") == 1
        assert await _count(pool, "overlay_store_write") == 1


@pytest.mark.asyncio
async def test_audit_atomic_an_audit_insert_that_fails_rolls_the_record_back() -> None:
    async with _store() as (store, pool):
        await store.apply_write(ModelOverlayStoreWrite.model_validate(default_load()))
        before = await store.read_current(OVERLAY_ID)
        # An audit row already holds this write id (with no write row), so the audit insert violates its key.
        await pool.execute(
            "INSERT INTO omninode_internal.overlay_store_audit "
            "(write_id, overlay_id, revision, key, setting_path, actor, audit) "
            "VALUES ('w-set', $1, 0, 'k', 'p', 'a', '{}'::json)",
            OVERLAY_ID,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await store.apply_write(_write(based_on_revision=1))
        assert await store.read_current(OVERLAY_ID) == before
        assert await _count(pool, "overlay_store_write") == 0
        assert await store.read_provenance(OVERLAY_ID) == {}
        assert (
            int(
                await pool.fetchval(
                    "SELECT revision FROM omninode_internal.overlay_store_revision"
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_concurrent_sets_on_one_revision_one_commits_and_one_conflicts() -> None:
    async with _store() as (store, pool):
        await store.apply_write(ModelOverlayStoreWrite.model_validate(default_load()))
        first = _write(write_id="w-a", based_on_revision=1)
        second = _write(
            write_id="w-b",
            based_on_revision=1,
            key="alarm_lane",
            path=PATH_LANE,
            value="lane-z",
        )
        replies = await asyncio.gather(
            store.apply_write(first), store.apply_write(second)
        )
        assert sorted(r.outcome for r in replies) == [
            EnumOverlayWriteOutcome.COMMITTED,
            EnumOverlayWriteOutcome.CONFLICT,
        ]
        current = await store.read_current(OVERLAY_ID)
        assert current is not None
        assert current["revision"] == 2
        assert len(current["operator_paths"]) == 1
        assert await _count(pool, "overlay_store_audit") == 1
        stored = json.loads(
            await pool.fetchval(
                "SELECT document FROM omninode_internal.overlay_store ORDER BY last_loaded_at DESC LIMIT 1"
            )
        )
        assert stored == current["document"]
