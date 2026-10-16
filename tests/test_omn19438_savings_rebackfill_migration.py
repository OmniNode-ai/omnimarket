# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19438 AC3: the second house-slug backfill for ``savings_estimates``.

Migration 091 re-attributed the rows stored under the house slug, but the lab
applied it about 28 hours before the writer fix was live. The unfixed writer then
wrote 770 more slug rows, and the migration runner records 091 as applied, so it
never runs again. The rows are only reachable by a new forward migration with the
same idempotent body.

The source-level tests run everywhere. The real-Postgres test builds a disposable
database from the full migration corpus minus the re-backfill, plants slug rows the
way the pre-fix writer left them, applies the re-backfill, and reads them back. It
SKIPS without ``INTEGRATION_POSTGRES_PASSWORD`` and a reachable server, like the
sibling savings gate it borrows its harness from.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG, HOUSE_TENANT_UUID
from tests.test_omn15583_savings_writer_binds_envelope_tenant import (
    _LANE_ROLES_SQL,
    _MIGRATION_DIRS,
    _admin_or_skip,
    _dsn_for_db,
)

_SAVINGS_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_savings"
    / "migrations"
)
_FIRST_BACKFILL = "091_savings_estimates_house_tenant_uuid_backfill.sql"
_REBACKFILL = "095_savings_estimates_house_tenant_uuid_rebackfill.sql"


@pytest.mark.unit
class TestRebackfillMigrationSource:
    def test_a_forward_migration_re_runs_the_backfill_after_the_first(self) -> None:
        names = sorted(p.name for p in _SAVINGS_MIGRATIONS.glob("*.sql"))
        assert _REBACKFILL in names
        assert names.index(_REBACKFILL) > names.index(_FIRST_BACKFILL)

    def test_it_moves_only_exact_slug_rows_to_the_house_uuid(self) -> None:
        sql = (_SAVINGS_MIGRATIONS / _REBACKFILL).read_text(encoding="utf-8")
        assert (
            f"SET tenant_id = v_house_uuid\n  WHERE tenant_id = '{HOUSE_TENANT_SLUG}'"
            in sql
        )
        assert f"v_house_uuid  TEXT := '{HOUSE_TENANT_UUID}'" in sql
        assert f"ALTER COLUMN tenant_id SET DEFAULT '{HOUSE_TENANT_UUID}'" in sql

    def test_it_refuses_to_commit_while_a_slug_row_remains(self) -> None:
        sql = (_SAVINGS_MIGRATIONS / _REBACKFILL).read_text(encoding="utf-8")
        assert "IF v_left <> 0 THEN" in sql
        assert "RAISE EXCEPTION" in sql


_PLANT_SLUG_ROW = (
    "INSERT INTO savings_estimates ("
    "  event_timestamp, session_id, model_local, model_cloud_baseline,"
    "  local_cost_usd, cloud_cost_usd, savings_usd, tenant_id) "
    "VALUES ($1, $2, 'glm-5.2', 'claude-opus-4-6', 0.001, 0.501, 0.5, $3)"
)


@asynccontextmanager
async def _corpus_without_rebackfill() -> AsyncIterator[asyncpg.Connection]:
    admin = await _admin_or_skip()
    database = f"omn19438_{uuid4().hex[:12]}"
    target: asyncpg.Connection | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        target = await asyncpg.connect(_dsn_for_db(database))
        await target.execute(_LANE_ROLES_SQL)
        await target.execute("CREATE SCHEMA IF NOT EXISTS omninode_internal")
        for migrations_dir in _MIGRATION_DIRS:
            for path in sorted(migrations_dir.glob("*.sql")):
                if path.name == _REBACKFILL:
                    continue
                await target.execute(
                    path.read_text(encoding="utf-8").replace(
                        "CREATE INDEX CONCURRENTLY", "CREATE INDEX"
                    )
                )
        yield target
    finally:
        if target is not None:
            with contextlib.suppress(Exception):
                await target.close()
        with contextlib.suppress(Exception):
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebackfill_moves_slug_rows_written_after_the_first_backfill() -> None:
    house_uuid = str(HOUSE_TENANT_UUID)
    other_tenant = "soak-seed-tenant-a"
    stamp = datetime(2026, 9, 25, 5, 0, 0, tzinfo=UTC)
    async with _corpus_without_rebackfill() as db:
        # 091 has run; the unfixed writer then lands rows under the slug.
        for session in ("slug-1", "slug-2"):
            await db.execute(_PLANT_SLUG_ROW, stamp, session, HOUSE_TENANT_SLUG)
        await db.execute(_PLANT_SLUG_ROW, stamp, "uuid-1", house_uuid)
        await db.execute(_PLANT_SLUG_ROW, stamp, "other-1", other_tenant)

        sql = (_SAVINGS_MIGRATIONS / _REBACKFILL).read_text(encoding="utf-8")
        await db.execute(sql)

        counts = {
            row["tenant_id"]: row["n"]
            for row in await db.fetch(
                "SELECT tenant_id, count(*) AS n FROM savings_estimates GROUP BY 1"
            )
        }
        assert counts == {house_uuid: 3, other_tenant: 1}

        await db.execute(sql)  # idempotent: a second apply moves nothing
        again = await db.fetchval("SELECT count(*) FROM savings_estimates")
        assert again == 4
