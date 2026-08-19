# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16146: real-Postgres write-path proof for the projection_watermarks table.

The ticket's defect: ``BaseProjectionRunner._update_watermark()`` (shared by
every projection writer node -- registration, llm-cost, savings, delegation,
baselines, routing-decision, session-outcome) upserts into
``projection_watermarks`` after every projected batch, but the table was
never migrated in this repo. On ``.201`` dev Postgres the writer logged
``Failed to update watermark: relation "projection_watermarks" does not
exist`` on every batch and a writer restart could not resume from a
persisted watermark.

Unit coverage (``tests/unit/test_projection_migration_runner.py``) proves the
migration FILE exists with the right DDL shape, but only a real Postgres
connection proves the DDL actually creates a working table and that
``_update_watermark``'s ``INSERT ... ON CONFLICT`` upsert -- including the
``GREATEST(last_offset, EXCLUDED.last_offset)`` monotonicity guard -- behaves
correctly against real column types (``last_offset``/``events_projected`` as
BIGINT, not a mock double that would accept a string just as happily, the
exact class of escape ``tests/test_omn15909_real_postgres_projection_write_path_gate.py``
exists to close). Skips (never errors) without a reachable database, mirroring
``tests/test_writer_tenant_isolation_omn14898.py``'s ``_connect_or_skip``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

_MIGRATION_SQL = (
    Path(__file__).resolve().parent
    / ".."
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_registration"
    / "migrations"
    / "0005_create_projection_watermarks.sql"
).resolve()

_SCHEMA = "omninode_internal"


class _WatermarkOnlyRunner(BaseProjectionRunner):
    """Minimal concrete BaseProjectionRunner: exercises only _update_watermark.

    topics/project_event are unused by this test (it drives _update_watermark
    directly) but must exist to satisfy BaseProjectionRunner's ABC contract.
    """

    @property
    def topics(self) -> list[str]:
        return []

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        raise NotImplementedError


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping projection_watermarks DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (
        OSError,
        asyncpg.PostgresError,
    ) as exc:  # pragma: no cover - infra-dependent
        pytest.skip(f"no reachable Postgres for projection_watermarks DB proof: {exc}")


def _pool_dsn_for_schema(schema: str) -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    options = quote(f"-c search_path={schema},public")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
        f"?options={options}"
    )


@pytest.mark.integration
async def test_real_postgres_update_watermark_upserts_with_monotonic_offset() -> None:
    """Apply migration 0005, then drive the REAL _update_watermark seam twice.

    Asserts: (1) the migration's DDL creates a working table matching the
    INSERT/ON CONFLICT shape _update_watermark already assumes; (2) a fresh
    row lands with correctly-typed BIGINT columns; (3) a second call with a
    LOWER offset does not regress last_offset (GREATEST monotonicity guard)
    but does increment events_projected -- proving the exact upsert semantics
    a writer restart depends on to resume from a persisted watermark.
    """
    conn = await _connect_or_skip()

    # OMN-16146 follow-up: the migration is schema-qualified
    # (omninode_internal.projection_watermarks, matching contract.yaml's
    # db_io.db_tables[].schema declaration and check_application_database_sql.py's
    # OMN-15361 requirement), so it can no longer be redirected into a disposable
    # per-test schema via search_path -- it hardcodes its own target. Apply it
    # for real (idempotent: every DDL statement is IF NOT EXISTS / guarded) and
    # isolate by row (a unique projection_name), not by schema; clean up by
    # deleting only this test's own rows, never the shared table.
    #
    # The migration's own GRANT to omninode_runtime is deliberately
    # unconditional (no guarded CREATE ROLE -- see the migration file's own
    # header for why a DO $$ ... $$ role-creation block is not an option
    # here), matching the real-deploy precondition that the role is already
    # provisioned "at the provisioning seam" before any node-owned migration
    # runs. This test replicates that precondition rather than relying on
    # migration-side defensiveness: on a CI-provisioned Postgres, the role
    # does not exist yet, so create it here, first, exactly as a real deploy
    # would have already done by the time this migration executes.
    await conn.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omninode_runtime') THEN "
        "CREATE ROLE omninode_runtime WITH NOLOGIN NOSUPERUSER NOBYPASSRLS "
        "NOCREATEDB NOCREATEROLE NOREPLICATION; "
        "END IF; "
        "END $$;"
    )
    test_projection_name = "omn16146-test-topic:0"
    await conn.execute(_MIGRATION_SQL.read_text(encoding="utf-8"))
    await conn.execute(
        f"DELETE FROM {_SCHEMA}.projection_watermarks WHERE projection_name = $1",
        test_projection_name,
    )
    try:
        runner = _WatermarkOnlyRunner()
        runner._db = AsyncpgAdapter(dsn=_pool_dsn_for_schema(_SCHEMA))
        await runner._db.connect()
        try:
            await runner._update_watermark("omn16146-test-topic:0", 42)

            rows = await conn.fetch(
                f"SELECT projection_name, last_offset, events_projected, "
                f"last_projected_at, updated_at FROM {_SCHEMA}.projection_watermarks "
                f"WHERE projection_name = $1",
                "omn16146-test-topic:0",
            )
            assert len(rows) == 1
            row = rows[0]
            assert row["last_offset"] == 42
            assert isinstance(row["last_offset"], int)
            assert row["events_projected"] == 1
            assert row["updated_at"] is not None
            # _update_watermark's INSERT column list is (projection_name,
            # last_offset, events_projected, updated_at) -- last_projected_at
            # is only set on the ON CONFLICT DO UPDATE branch, so a brand-new
            # row's first write legitimately leaves it NULL until the second
            # write for the same projection_name. Real, pre-existing behavior
            # of runner.py (not introduced by this ticket) -- documented here
            # rather than silently asserted away.
            assert row["last_projected_at"] is None

            # A lower offset must not regress last_offset (GREATEST guard),
            # but events_projected still increments and last_projected_at is
            # now populated (this write hits ON CONFLICT) -- exactly the
            # semantics a writer restart's resume-from-persisted-watermark
            # depends on.
            await runner._update_watermark("omn16146-test-topic:0", 10)

            rows_after = await conn.fetch(
                f"SELECT last_offset, events_projected, last_projected_at "
                f"FROM {_SCHEMA}.projection_watermarks WHERE projection_name = $1",
                "omn16146-test-topic:0",
            )
            assert rows_after[0]["last_offset"] == 42
            assert rows_after[0]["events_projected"] == 2
            assert rows_after[0]["last_projected_at"] is not None
        finally:
            await runner._db._pool.close()  # type: ignore[union-attr]
    finally:
        # omninode_internal is the real, shared schema (not a disposable
        # per-test one) -- clean up only this test's own row, never DROP it.
        await conn.execute(
            f"DELETE FROM {_SCHEMA}.projection_watermarks WHERE projection_name = $1",
            test_projection_name,
        )
        await conn.close()
