# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16316: real-Postgres write-path gate for the tenant-credentials projection.

Required companion under this repo's projection-write-path real-DB gate
(``scripts/ci/check_projection_write_path_db_gate.py``, OMN-15909) because
this PR touches ``handler_tenant_credentials_projection.py``. The mock-DB
unit tests in ``tests/test_omn16316_tenant_credentials_projection.py`` prove
the SQL text and bound-argument shape; only a real Postgres connection
enforces actual column types via asyncpg's extended query protocol (the
OMN-15905 failure class this gate exists to catch).

Harness mirrors ``tests/test_omn16150_publish_snapshot_delta_delete_real_postgres.py``
byte-for-byte in spirit: disposable per-run schema, real ``AsyncpgAdapter``,
SKIPS (never ERRORs) without a reachable database.

OMN-14355 note: ``handler_tenant_credentials_projection.py`` also touches
this file's write-path glob when ``handle()``'s single positional param is
renamed to the canonical ``request`` (the shape the shared
``runtime_local_adapter`` dispatches through). That rename is dispatch-shim
only -- ``handle()`` still delegates unchanged to ``project_event()``, which
is what this file's tests drive against real Postgres. A dedicated real-DB
test of ``handle()`` itself was deliberately NOT added: ``handle()`` wraps
its delegation in a synchronous ``asyncio.run()`` call, which cannot be
invoked from inside a pytest-asyncio test coroutine (nested-loop
``RuntimeError``), and reusing this file's pooled ``AsyncpgAdapter`` across
two sequential ``asyncio.run()`` calls is unsafe (asyncpg pools are bound to
the event loop that created them). ``handle()``'s pure dispatch/param-shape
behavior (no DB) is covered by a mock-DB unit test in
``tests/test_omn16316_tenant_credentials_projection.py`` instead.

CodeRabbit round (omnimarket#2117) note: the same write-path glob is also
touched by threading ``tenant_id=str(row["tenant_id"])`` into
``publish_snapshot_delta()`` (previously defaulted to ``"omninode"``, a
cross-tenant snapshot-header bug). That change is downstream of the SQL
``RETURNING`` row this file's tests already prove correctly typed
(``test_registered_inserts_a_correctly_typed_row`` asserts
``row["tenant_id"] == "omninode"``) -- it does not change any SQL text or
bound-argument shape itself, only which already-real value gets forwarded to
a Kafka-facing call this file's harness has no producer to exercise. Covered
instead by a mock-DB regression test,
``test_credential_registered_snapshot_publish_uses_the_rows_own_tenant_id``,
in ``tests/test_omn16316_tenant_credentials_projection.py``.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.routing.tenant_overlay_resolver import BYOK_ALL_TASK_TYPES

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_tenant_credentials"
    / "migrations"
)
# All migrations, in filename order -- matches the deployed convention of
# running every 000N_*.sql file in sequence (see node_projection_registration
# for the precedent of later files ALTERing a table 0000 created). OMN-16324
# added 0001_relax_name_provider_not_null.sql; a single hardcoded 0000 path
# here would silently skip it and mask the NOT NULL violation it fixes.
_MIGRATIONS = sorted(_MIGRATIONS_DIR.glob("*.sql"))

# OMN-17372: this projection's SECOND write target. The relation is created and
# owned by node_delegation_routing_reducer's own migration -- this handler adds
# a writer, not a table -- so the provisioner applies that file too. Without it
# the overlay write would raise UndefinedTable and every test below would fail
# for the wrong reason.
_OVERLAY_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_routing_reducer"
    / "migrations"
    / "0001_create_delegation_routing_tenant_overlay.sql"
)

TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"
TOPIC_REVOKED = "onex.evt.omnimarket.credential-revoked.v1"

# A non-house tenant: HOUSE_TENANT_SLUG short-circuits overlay RESOLUTION, so
# proving the overlay half against "omninode" would prove a row nothing reads.
BYOK_TENANT = "omn17372-byok-tenant"


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
            "POSTGRES_PASSWORD not set -- skipping OMN-16316 real-Postgres "
            "tenant-credentials write-path proof"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-16316 write-path proof: {exc}")


@asynccontextmanager
async def _provisioned_runner() -> AsyncIterator[
    tuple[HandlerTenantCredentialsProjectionRunner, asyncpg.Connection, str]
]:
    admin_conn = await _connect_or_skip()
    schema = f"omn16316_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        for migration in (*_MIGRATIONS, _OVERLAY_MIGRATION):
            await admin_conn.execute(migration.read_text(encoding="utf-8"))

        pool = await asyncpg.create_pool(
            _base_dsn(),
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_base_dsn())
        adapter._pool = pool

        runner = HandlerTenantCredentialsProjectionRunner()
        runner._db = adapter

        yield runner, admin_conn, schema
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


@pytest.mark.integration
class TestRealPostgresWritePath:
    async def test_registered_inserts_a_correctly_typed_row(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_omninode_openrouter_{uuid4().hex[:12]}"
            ok = await runner.project_event(
                TOPIC_REGISTERED,
                {
                    "tenant_id": "omninode",
                    "provider": "openrouter",
                    "name": "real-pg-proof",
                    "api_key_ref": ref,
                },
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )
            assert ok is True

            row = await admin_conn.fetchrow(
                "SELECT api_key_ref, tenant_id, name, provider, created_at, "
                "revoked_at FROM tenant_inference_credentials WHERE api_key_ref = $1",
                ref,
            )
            assert row is not None
            assert row["api_key_ref"] == ref
            assert row["tenant_id"] == "omninode"
            assert row["name"] == "real-pg-proof"
            assert row["provider"] == "openrouter"
            assert row["created_at"] is not None
            assert row["revoked_at"] is None

    async def test_revoked_sets_revoked_at_without_deleting_the_row(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_omninode_openai_{uuid4().hex[:12]}"
            await runner.project_event(
                TOPIC_REGISTERED,
                {
                    "tenant_id": "omninode",
                    "provider": "openai",
                    "name": "real-pg-revoke-proof",
                    "api_key_ref": ref,
                },
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )

            ok = await runner.project_event(
                TOPIC_REVOKED,
                {"tenant_id": "omninode", "api_key_ref": ref},
                MessageMeta(
                    partition=0, offset=1, fallback_id=ref, topic=TOPIC_REVOKED
                ),
            )
            assert ok is True

            row = await admin_conn.fetchrow(
                "SELECT api_key_ref, revoked_at FROM tenant_inference_credentials "
                "WHERE api_key_ref = $1",
                ref,
            )
            assert row is not None, "revoke must never delete the row"
            assert row["revoked_at"] is not None

    async def test_revoke_of_unknown_ref_persists_a_tombstone_not_an_error(
        self,
    ) -> None:
        """OMN-16324: a revoke for a ref never seen by register is no longer
        a bare no-op -- it must persist a tombstone row (name/provider NULL,
        revoked_at set) rather than silently doing nothing."""
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = "cred_never_registered"
            ok = await runner.project_event(
                TOPIC_REVOKED,
                {"tenant_id": "omninode", "api_key_ref": ref},
                MessageMeta(
                    partition=0,
                    offset=0,
                    fallback_id="never",
                    topic=TOPIC_REVOKED,
                ),
            )
            assert ok is True

            row = await admin_conn.fetchrow(
                "SELECT api_key_ref, tenant_id, name, provider, revoked_at "
                "FROM tenant_inference_credentials WHERE api_key_ref = $1",
                ref,
            )
            assert row is not None, "revoke-before-register must persist a tombstone"
            assert row["tenant_id"] == "omninode"
            assert row["name"] is None
            assert row["provider"] is None
            assert row["revoked_at"] is not None

    async def test_revoke_before_register_out_of_order_keeps_credential_revoked(
        self,
    ) -> None:
        """OMN-16324 regression: this is the exact reproduction from the
        ticket -- a credential-revoked event lands BEFORE its matching
        credential-registered event (the two are published to separate Kafka
        topics with no cross-topic ordering guarantee). Against the
        pre-fix handler this fails: the revoke matches zero rows (no-op),
        then the register's ``INSERT ... ON CONFLICT DO UPDATE`` creates the
        row fresh with revoked_at = NULL, silently un-revoking the
        credential. The fixed handler must leave the credential revoked."""
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_omninode_openrouter_{uuid4().hex[:12]}"

            revoke_ok = await runner.project_event(
                TOPIC_REVOKED,
                {"tenant_id": "omninode", "api_key_ref": ref},
                MessageMeta(
                    partition=1, offset=0, fallback_id=ref, topic=TOPIC_REVOKED
                ),
            )
            assert revoke_ok is True

            register_ok = await runner.project_event(
                TOPIC_REGISTERED,
                {
                    "tenant_id": "omninode",
                    "provider": "openrouter",
                    "name": "raced-registration",
                    "api_key_ref": ref,
                },
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )
            assert register_ok is True

            row = await admin_conn.fetchrow(
                "SELECT api_key_ref, tenant_id, name, provider, revoked_at "
                "FROM tenant_inference_credentials WHERE api_key_ref = $1",
                ref,
            )
            assert row is not None
            assert row["tenant_id"] == "omninode"
            # The register event fills in name/provider onto the tombstone...
            assert row["name"] == "raced-registration"
            assert row["provider"] == "openrouter"
            # ...but must NOT resurrect the credential.
            assert row["revoked_at"] is not None, (
                "revoke-before-register must not be silently un-revoked by "
                "the later out-of-order register"
            )

    async def test_revoke_is_idempotent_and_keeps_earliest_timestamp(self) -> None:
        """A repeat revoke (normal order: register, revoke, revoke again)
        must not bump revoked_at -- the UPSERT's COALESCE keeps the earliest
        revocation timestamp, matching the pre-fix no-op-on-second-revoke
        behavior."""
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_omninode_openai_{uuid4().hex[:12]}"
            await runner.project_event(
                TOPIC_REGISTERED,
                {
                    "tenant_id": "omninode",
                    "provider": "openai",
                    "name": "double-revoke-proof",
                    "api_key_ref": ref,
                },
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )
            await runner.project_event(
                TOPIC_REVOKED,
                {"tenant_id": "omninode", "api_key_ref": ref},
                MessageMeta(
                    partition=0, offset=1, fallback_id=ref, topic=TOPIC_REVOKED
                ),
            )
            first_revoked_at = await admin_conn.fetchval(
                "SELECT revoked_at FROM tenant_inference_credentials "
                "WHERE api_key_ref = $1",
                ref,
            )
            assert first_revoked_at is not None

            await runner.project_event(
                TOPIC_REVOKED,
                {"tenant_id": "omninode", "api_key_ref": ref},
                MessageMeta(
                    partition=0, offset=2, fallback_id=ref, topic=TOPIC_REVOKED
                ),
            )
            second_revoked_at = await admin_conn.fetchval(
                "SELECT revoked_at FROM tenant_inference_credentials "
                "WHERE api_key_ref = $1",
                ref,
            )
            assert second_revoked_at == first_revoked_at


@pytest.mark.integration
class TestRealPostgresRoutingOverlayWritePath:
    """OMN-17372 blocker b3, against real Postgres.

    The unit tests prove SQL text and bound-argument shape. Only a real
    connection proves the ``INSERT ... SELECT ... WHERE NOT EXISTS ... ON
    CONFLICT`` form parses, that the explicit ``::TEXT`` / ``::INTEGER`` casts
    satisfy asyncpg's extended query protocol with NULL-able integers, and that
    the unique constraint actually converges a redelivery instead of raising --
    the OMN-15905 failure class this whole file exists to catch.
    """

    @staticmethod
    def _register(ref: str, *, provider: str = "openrouter") -> dict[str, Any]:
        return {
            "tenant_id": BYOK_TENANT,
            "provider": provider,
            "name": "byok-route-proof",
            "api_key_ref": ref,
        }

    async def test_registration_mints_a_routing_overlay_row(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_{BYOK_TENANT}_openrouter_{uuid4().hex[:12]}"
            assert await runner.project_event(
                TOPIC_REGISTERED,
                self._register(ref),
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )

            row = await admin_conn.fetchrow(
                "SELECT tenant_id, task_type, backend_id, endpoint_url, model_name, "
                "secret_ref, timeout_ms, max_tokens "
                "FROM delegation_routing_tenant_overlay WHERE tenant_id = $1",
                BYOK_TENANT,
            )
            assert row is not None, (
                "registering a BYOK key must mint the route that selects it; "
                "before OMN-17372 this table had no writer of any kind"
            )
            assert row["task_type"] == BYOK_ALL_TASK_TYPES
            assert row["secret_ref"] == ref
            assert row["endpoint_url"].startswith("https://openrouter.ai/")
            # Never a house ref.
            assert not row["secret_ref"].startswith("llm.")

    async def test_redelivery_converges_to_one_row(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_{BYOK_TENANT}_openrouter_{uuid4().hex[:12]}"
            for offset in (0, 1, 2):
                assert await runner.project_event(
                    TOPIC_REGISTERED,
                    self._register(ref),
                    MessageMeta(
                        partition=0,
                        offset=offset,
                        fallback_id=ref,
                        topic=TOPIC_REGISTERED,
                    ),
                )

            count = await admin_conn.fetchval(
                "SELECT count(*) FROM delegation_routing_tenant_overlay "
                "WHERE tenant_id = $1",
                BYOK_TENANT,
            )
            assert count == 1

    async def test_a_second_key_repoints_the_same_row(self) -> None:
        """One route per tenant: re-registering swaps the ref, never forks."""
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            first = f"cred_{BYOK_TENANT}_openrouter_{uuid4().hex[:12]}"
            second = f"cred_{BYOK_TENANT}_openrouter_{uuid4().hex[:12]}"
            for offset, ref in ((0, first), (1, second)):
                await runner.project_event(
                    TOPIC_REGISTERED,
                    self._register(ref),
                    MessageMeta(
                        partition=0,
                        offset=offset,
                        fallback_id=ref,
                        topic=TOPIC_REGISTERED,
                    ),
                )

            rows = await admin_conn.fetch(
                "SELECT secret_ref FROM delegation_routing_tenant_overlay "
                "WHERE tenant_id = $1",
                BYOK_TENANT,
            )
            assert [r["secret_ref"] for r in rows] == [second]

    async def test_undeclared_provider_mints_no_route(self) -> None:
        """Ruling 3: a provider with no declared backend gets no route at all.

        Not a route to the nearest platform rung -- that rung carries a HOUSE
        secret_ref, and answering a customer on it is the forbidden outcome.
        """
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_{BYOK_TENANT}_openai_{uuid4().hex[:12]}"
            assert await runner.project_event(
                TOPIC_REGISTERED,
                self._register(ref, provider="openai"),
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )

            assert (
                await admin_conn.fetchval(
                    "SELECT count(*) FROM delegation_routing_tenant_overlay "
                    "WHERE tenant_id = $1",
                    BYOK_TENANT,
                )
                == 0
            )
            # The credential itself is still catalogued and visible to its owner.
            assert (
                await admin_conn.fetchval(
                    "SELECT count(*) FROM tenant_inference_credentials "
                    "WHERE api_key_ref = $1",
                    ref,
                )
                == 1
            )

    async def test_revocation_nulls_the_ref_and_keeps_the_row(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_{BYOK_TENANT}_openrouter_{uuid4().hex[:12]}"
            await runner.project_event(
                TOPIC_REGISTERED,
                self._register(ref),
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )
            assert await runner.project_event(
                TOPIC_REVOKED,
                {"tenant_id": BYOK_TENANT, "api_key_ref": ref},
                MessageMeta(
                    partition=0, offset=1, fallback_id=ref, topic=TOPIC_REVOKED
                ),
            )

            row = await admin_conn.fetchrow(
                "SELECT secret_ref, backend_id FROM delegation_routing_tenant_overlay "
                "WHERE tenant_id = $1",
                BYOK_TENANT,
            )
            assert row is not None, (
                "revocation must NOT drop the row -- with no overlay row the "
                "tenant falls through to the platform default, i.e. the house "
                "ladder on OmniNode's own provider credential"
            )
            assert row["secret_ref"] is None
            assert row["backend_id"]

    async def test_revoke_before_register_never_mints_a_live_route(self) -> None:
        """The OMN-16324 cross-topic race, applied to the route.

        The revoke lands first and writes the credential tombstone; the later
        register must not hand the tenant a working route to a credential they
        already revoked.
        """
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_{BYOK_TENANT}_openrouter_{uuid4().hex[:12]}"
            await runner.project_event(
                TOPIC_REVOKED,
                {"tenant_id": BYOK_TENANT, "api_key_ref": ref},
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REVOKED
                ),
            )
            await runner.project_event(
                TOPIC_REGISTERED,
                self._register(ref),
                MessageMeta(
                    partition=0, offset=1, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )

            assert (
                await admin_conn.fetchval(
                    "SELECT count(*) FROM delegation_routing_tenant_overlay "
                    "WHERE tenant_id = $1 AND secret_ref IS NOT NULL",
                    BYOK_TENANT,
                )
                == 0
            ), "a revoked credential must never end up behind a live route"

    async def test_the_written_row_resolves_to_cost_tier_tenant_byok(self) -> None:
        """The whole bridge, over real Postgres and the real reducer.

        Reads the row back through ``PostgresReadDatabaseAdapter`` -- the same
        adapter ``resolve_tenant_overlay_db()`` builds in the runtime -- so the
        proof spans the actual write and the actual read, not two fakes that
        agree with each other.
        """
        from omnimarket.nodes.node_delegation_orchestrator.models import (
            ModelDelegationRequest,
        )
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            delta,
        )
        from omnimarket.projection.postgres_read_database import (
            PostgresReadDatabaseAdapter,
        )
        from omnimarket.routing.tenant_overlay_resolver import resolve_tenant_overlay

        async with _provisioned_runner() as (runner, _admin_conn, schema):
            ref = f"cred_{BYOK_TENANT}_openrouter_{uuid4().hex[:12]}"
            await runner.project_event(
                TOPIC_REGISTERED,
                self._register(ref),
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )

            reader = PostgresReadDatabaseAdapter(
                f"{_base_dsn()}?options=-csearch_path%3D{schema}%2Cpublic",
                tenant_id=BYOK_TENANT,
            )
            try:
                overlay = resolve_tenant_overlay(
                    reader, tenant_id=BYOK_TENANT, task_type="code_generation"
                )
            finally:
                with contextlib.suppress(Exception):
                    reader.close()

            assert overlay is not None, (
                "the row the projection just wrote must resolve for a task type "
                "the sentinel does not name"
            )
            decision = delta(
                ModelDelegationRequest(
                    correlation_id=uuid4(),
                    prompt="write a function that returns 4",
                    task_type="code_generation",
                    emitted_at=datetime.now(tz=UTC),
                    tenant_id=BYOK_TENANT,
                ),
                tenant_overlay=overlay,
            )

            assert decision.cost_tier == "tenant_byok"
            assert decision.api_key_ref == ref
            assert decision.endpoint_url.startswith("https://openrouter.ai/")
