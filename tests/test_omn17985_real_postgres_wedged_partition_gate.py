# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17985: real-Postgres proof that a tenant-identity refusal ADVANCES the offset.

``omnimarket-projection-delegation-writer`` crash-looped on onex-dev from
roughly 2026-09-06T11:04Z. Its own log, read through the crash-forensics probe
section added in omninode_infra#1207 (run ``34073692873``)::

    RECOVERABLE error projecting onex.evt.omnibase-infra.delegation-failed.v1
    (offset uncommitted, will retry): OMN-16804: no canonical UUID for verified
    tenant slug 'operator-ledger-probe'. ...
    Consumer attempt 10/10 failed: ...
    Consumer failed after 10 retries

    lastState.terminated reason=Completed exitCode=0

The classification fix is unit-proven in
``tests/test_projection_wedge_omn17985.py`` against a mock DB. **That is not
sufficient here**, and the projection write-path gate is right to demand this
module: the refusal under test is raised by a lookup against a REAL relation
(``tenant_registry_mirror``) through asyncpg, and the fix's central claim is a
statement about the DATABASE -- "the offset advanced and no row was written".
A mock DB accepts any write and can prove neither half. Only a real Postgres
connection can show that the quarantine path leaves the table empty rather
than leaving a half-written or NULL-tenant row behind, and that the positive
control genuinely writes.

``tests/test_omn16804_registry_resolved_write_tenant_real_postgres.py`` already
proves the HANDLER raises and writes no row. This module proves what the
RUNNER does with that raise, which is the part that wedged nine partitions:
route to the contract-declared poison DLQ, commit the offset, continue.

Harness pattern (``_connect_or_skip`` / disposable schema / guarded
``app_dashboard`` role) mirrors
``tests/test_omn15909_real_postgres_projection_write_path_gate.py`` and
``tests/test_omn16804_registry_resolved_write_tenant_real_postgres.py`` --
SKIPS (never ERRORs) without a reachable database, and provisions its own
throwaway schema so runs never collide.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
)

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)

# 0031 converts delegation_events.tenant_id to UUID against the LIVE schema and
# is fenced out of test applies by the sibling OMN-16804 module for the same
# reason: the conversion is replayed explicitly below so the column ends up in
# the post-0032 shape regardless of apply order.
_FENCED_MIGRATION = "0031_delegation_events_tenant_id_to_uuid.sql"

# The slug the live wedge carried. Kept verbatim so this gate is anchored to
# the event that actually stopped the fleet, not to a paraphrase of it.
WEDGING_TENANT_SLUG = "operator-ledger-probe"

# The positive control: a slug the compiled legacy map has never heard of,
# materialized into the registry mirror by this test.
PROVISIONED_SLUG = "beta-omn17985-6f21a0"
PROVISIONED_UUID = UUID("6f21a0d2-4b18-4c07-a5f1-9d2e6c4b7a30")

_APP_DASHBOARD_ROLE_SQL = """
DO $$
BEGIN
  BEGIN
    CREATE ROLE app_dashboard WITH
      NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
  EXCEPTION
    WHEN duplicate_object OR unique_violation THEN
      NULL;
  END;
END;
$$;
"""

_MIRROR_DDL = """
CREATE TABLE IF NOT EXISTS tenant_registry_mirror (
    tenant_slug         TEXT PRIMARY KEY,
    tenant_uuid         UUID NOT NULL,
    display_name        TEXT,
    status              TEXT NOT NULL,
    registry_created_at TIMESTAMPTZ,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_event_id     TEXT
);
"""

_CONVERT_TENANT_ID_TO_UUID = """
DO $$
DECLARE
    v_data_type TEXT;
BEGIN
    SELECT data_type
      INTO v_data_type
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'delegation_events'
       AND column_name = 'tenant_id';

    IF v_data_type = 'uuid' THEN
        RETURN;
    END IF;

    DROP POLICY IF EXISTS tenant_isolation ON delegation_events;
    ALTER TABLE delegation_events ALTER COLUMN tenant_id DROP DEFAULT;
    ALTER TABLE delegation_events
        ALTER COLUMN tenant_id TYPE UUID USING (NULLIF(tenant_id, '')::uuid);
    CREATE POLICY tenant_isolation ON delegation_events
      FOR ALL
      USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
END$$;
"""


def _test_schema_safe_sql(raw_sql: str) -> str:
    """``CREATE INDEX CONCURRENTLY`` cannot run inside asyncpg's implicit
    transaction for a multi-statement string; it is pointless on a disposable
    empty schema and schema-equivalent without it."""
    return raw_sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")


def _live_migration_files() -> list[Path]:
    return [
        path
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if path.name != _FENCED_MIGRATION
    ]


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping the OMN-17985 "
            "real-Postgres wedged-partition gate"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-17985 gate: {exc}")


class _RecordingConsumer:
    """Records committed offsets. The offset is the whole point of this gate."""

    def __init__(self) -> None:
        self.commits: list[dict[Any, int]] = []

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commits.append(offsets)


def _terminal_payload(*, correlation_id: str, tenant_slug: str) -> dict[str, Any]:
    """A ``delegation-failed.v1`` terminal, the topic the live wedge arrived on.

    ``delegation-completed.v1`` and ``delegation-failed.v1`` both dispatch to
    ``_project_delegation_terminal_result``, so the tenant-resolution seam under
    test is identical on either; the failed topic is used because that is what
    the crash-looping pod logged.
    """
    return {
        "correlation_id": correlation_id,
        "tenant_id": tenant_slug,
        "task_type": "code-review",
        "delegated_to": "node_delegate_skill_orchestrator",
        "success": False,
        "model_used": "glm-5.2",
        "quality_passed": False,
        "quality_score": 0.10,
        "latency_ms": 1200,
        "prompt_tokens": 100,
        "completion_tokens": 0,
        "cost_tier_name": "cheap_cloud",
    }


class _Msg:
    def __init__(
        self, *, topic: str, partition: int, offset: int, value: bytes
    ) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.value = value


@asynccontextmanager
async def _provisioned_runner() -> AsyncIterator[
    tuple[DelegationProjectionRunner, asyncpg.Connection, list[tuple[str, bytes]]]
]:
    """A real, migrated, disposable schema behind a real DelegationProjectionRunner.

    Yields ``(runner, admin_conn, dlq_published)``. The runner's consumer is a
    recording double so the committed offset is observable; everything below it
    -- adapter, pool, schema, registry mirror -- is real.
    """
    admin_conn = await _connect_or_skip()
    schema = f"omn17985_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    dlq_published: list[tuple[str, bytes]] = []
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_APP_DASHBOARD_ROLE_SQL)
        for migration_path in _live_migration_files():
            await admin_conn.execute(
                _test_schema_safe_sql(migration_path.read_text(encoding="utf-8"))
            )
        await admin_conn.execute(_MIRROR_DDL)
        await admin_conn.execute(_CONVERT_TENANT_ID_TO_UUID)

        pool = await asyncpg.create_pool(
            _base_dsn(),
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_base_dsn())
        adapter._pool = pool  # type: ignore[attr-defined]

        runner = DelegationProjectionRunner()
        runner._db = adapter  # type: ignore[assignment]
        runner._consumer = _RecordingConsumer()  # type: ignore[assignment]

        async def _capture_dlq(topic: str, value: bytes) -> None:
            dlq_published.append((topic, value))

        runner.publish_dlq = _capture_dlq  # type: ignore[method-assign]

        async def _no_watermark(projection_name: str, offset: int) -> None:
            return None

        runner._update_watermark = _no_watermark  # type: ignore[method-assign]

        yield runner, admin_conn, dlq_published
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


@pytest.mark.integration
class TestWedgedPartitionAdvancesAgainstRealPostgres:
    async def test_unprovisioned_tenant_dlqs_advances_and_writes_no_row(self) -> None:
        """The defect, end to end, against a real database.

        Before this fix the same call raised out of ``_handle_message``, the
        offset was never committed, the identical message was re-read, and the
        writer's nine partitions never advanced -- three tables at 0 rows for
        fourteen hours while the Deployment reported ``readyReplicas=1``.
        """
        async with _provisioned_runner() as (runner, admin_conn, dlq_published):
            correlation_id = str(uuid4())
            payload = _terminal_payload(
                correlation_id=correlation_id, tenant_slug=WEDGING_TENANT_SLUG
            )
            msg = _Msg(
                topic=runner._topic_delegation_failed,
                partition=0,
                offset=17,
                value=json.dumps({"payload": payload}).encode("utf-8"),
            )

            await runner._handle_message(msg)

            commits = runner._consumer.commits  # type: ignore[attr-defined]
            assert commits, "the offset was never committed at all"
            assert list(commits[0].values()) == [18], (
                "the offset MUST advance past an unattributable event; not "
                "advancing is what wedged nine partitions"
            )
            assert len(dlq_published) == 1, (
                "quarantine is not a drop -- the event must reach the "
                "contract-declared poison DLQ"
            )
            topic, value = dlq_published[0]
            assert topic == "onex.dlq.omnimarket.projection-delegation-malformed.v1"
            envelope = json.loads(value.decode("utf-8"))
            assert WEDGING_TENANT_SLUG in envelope["failure_reason"], (
                "the typed refusal must survive onto the DLQ so a reader can "
                "tell an identity refusal from a malformed payload"
            )

            row = await admin_conn.fetchrow(
                "SELECT 1 FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is None, (
                "an unattributable event must not become a row -- and must not "
                "become a NULL-tenant row either. Only a real database can "
                "show that the quarantine path left the table untouched."
            )

    async def test_a_registry_provisioned_tenant_still_writes_its_row(self) -> None:
        """POSITIVE CONTROL, against the same real schema.

        Without this, "the refusal quarantines" would be indistinguishable from
        "the writer quarantines everything" -- a gate that DLQ'd every event
        would pass the test above and produce exactly the zero-row projection
        this ticket exists to end.
        """
        async with _provisioned_runner() as (runner, admin_conn, dlq_published):
            await admin_conn.execute(
                f"INSERT INTO {TENANT_REGISTRY_MIRROR_TABLE} "
                "(tenant_slug, tenant_uuid, status, source_event_id) "
                "VALUES ($1, $2, $3, $4)",
                PROVISIONED_SLUG,
                PROVISIONED_UUID,
                "active",
                str(uuid4()),
            )

            correlation_id = str(uuid4())
            payload = _terminal_payload(
                correlation_id=correlation_id, tenant_slug=PROVISIONED_SLUG
            )
            msg = _Msg(
                topic=runner._topic_delegation_failed,
                partition=0,
                offset=41,
                value=json.dumps({"payload": payload}).encode("utf-8"),
            )

            await runner._handle_message(msg)

            commits = runner._consumer.commits  # type: ignore[attr-defined]
            assert commits, "the offset was never committed at all"
            assert list(commits[0].values()) == [42]
            assert dlq_published == [], "a resolvable event must NOT be quarantined"
            stored = await admin_conn.fetchrow(
                "SELECT tenant_id FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert stored is not None, "the projection row must land"
            assert stored["tenant_id"] == PROVISIONED_UUID
