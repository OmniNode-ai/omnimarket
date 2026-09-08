# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17422: the quality-gate-result projection must bind the tenant scope the
event carries, and must never leave ``tenant_id`` to the deployed column default.

Live-measured cause (onex-dev, i-06169517a92b45f86, 2026-09-07). The staging
business-proof gate FAILs its ``quality_gate`` check with "no delegation
projection row for correlation_id ...: the quality verdict never reached the
projection plane (absent is FAIL, never skip)". The dedicated
``omnimarket-projection-delegation-writer`` consumed the verdict and logged::

    POISON event ... routed to DLQ=True (offset committed): new row violates
    row-level security policy for table "delegation_events"

The offset is committed on the POISON path, so the row is permanently absent.

MECHANISM, measured rather than assumed. ``delegation_events`` on onex-dev
carries ``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` with

    tenant_isolation  USING/WITH CHECK (tenant_id = current_setting('app.tenant_id', true))

and the writer connects as ``role_omnidash`` (``rolbypassrls = false``). Two
independent things then decided the write:

* the ROW omitted ``tenant_id`` entirely, so Postgres applied the column
  DEFAULT -- ``'omninode'::text`` on that lane, because migrations 0031-0034
  (the OMN-15683 TEXT-slug -> UUID conversion) are NOT in
  ``omnimarket_schema_migrations`` there; and
* the GUC was synthesised separately by ``resolve_write_tenant(None, table=...)``,
  which consults ``_UUID_CONVERTED_TABLES`` -- a hardcoded frozenset naming
  ``delegation_events`` -- and therefore returned the house tenant UUID.

``'omninode' = '820272f9-4aaf-5add-a2df-0af942852ab2'`` is false, so the policy
refused the row. The two halves of the same comparison were resolved by two
different authorities, and the write only ever succeeded on a lane where those
authorities happened to agree.

The fix is not to weaken the policy or to widen a default. It is to make the
write bind ONE tenant to both halves:

1. the producer-recorded envelope stamp (``ModelEventEnvelope.tenant_id``) when
   the event carries one -- ``ModelQualityGateResult`` is ``extra="forbid"`` and
   has no tenant field of its own, and a writer under FORCE RLS cannot discover
   a row's tenant by reading (unset GUC => NULL predicate => zero rows), so the
   envelope is the only attribution available;
2. otherwise the house tenant, stamped EXPLICITLY on the row by
   ``house_tenant_write_stamp`` -- the same helper ``generation_events`` already
   uses under the 2026-08-02 house-tenant ruling -- so the stored value and the
   GUC are the same string by construction, whatever the deployed column
   default is.

A recorded-but-unresolvable tenant still raises (``resolve_registry_tenant_uuid``),
which is the typed refusal; nothing here invents a tenant.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    ModelQualityGateResult,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.envelope import envelope_tenant_identity
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
    resolve_write_tenant,
)

_BETA_TENANT_SLUG = "beta-business-proof"
_BETA_TENANT_UUID = "91c74442-1233-4c97-b191-911a10346fdf"
_ENVELOPE_TIMESTAMP = datetime(2026, 9, 8, 10, 2, 41, 550000, tzinfo=UTC)


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=None)
    return db


def _quality_gate_wire_record(
    *, correlation_id: str, tenant_id: str | None
) -> dict[str, Any]:
    """The real ``onex.evt.omnibase-infra.quality-gate-result.v1`` record shape.

    Copied from a live record read off the .201 dev-lane broker on
    2026-09-07 (``rpk topic consume ... -o -3 -n 1``): a ``ModelEventEnvelope``
    whose ``payload`` is the ``ModelQualityGateResult``, and whose own
    ``tenant_id`` field is the envelope-side tenant stamp. The runner's
    ``unwrap_envelope`` returns the payload with the whole raw record attached
    under ``_envelope``, which is what ``project_event`` is handed.
    """
    payload = {
        "correlation_id": correlation_id,
        "passed": True,
        "fail_category": "pass",
        "quality_score": 1.0,
        "failure_reasons": [],
        "fallback_recommended": False,
        "score_source": SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
        "acceptance_version": "delegation-deterministic-acceptance.v1",
        "actual_score": 1.0,
        "pass": True,
        "failure_cases": [],
        "skipped_checks": [],
    }
    envelope = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        # OMN-15583: ``ModelEventEnvelope.envelope_timestamp`` is
        # ``default_factory``-populated, so EVERY real envelope on this topic
        # carries one. The fixture omitted it, which is why no test here
        # noticed that the row this write proposes named no ``timestamp`` at
        # all.
        "envelope_timestamp": _ENVELOPE_TIMESTAMP.isoformat(),
        "correlation_id": correlation_id,
        "event_type": "omnibase-infra.quality-gate-result",
        "tenant_id": tenant_id,
    }
    unwrapped = dict(payload)
    unwrapped["_envelope"] = envelope
    return unwrapped


def _upsert_call(mock_db: AsyncMock) -> Any:
    writes = [
        call
        for call in mock_db.execute.await_args_list
        if "INSERT INTO delegation_events" in str(call.args[0])
    ]
    assert writes, "expected a delegation_events INSERT"
    return writes[-1]


def _upsert_row(mock_db: AsyncMock) -> dict[str, Any]:
    call = _upsert_call(mock_db)
    sql = str(call.args[0])
    columns_segment = sql.split("(", 1)[1].split(")", 1)[0]
    columns = [c.strip() for c in columns_segment.split(",")]
    return dict(zip(columns, call.args[1:], strict=True))


class TestEnvelopeTenantIdentity:
    """The reader that turns the producer's envelope stamp into an identity."""

    def test_returns_the_recorded_tenant(self) -> None:
        data = _quality_gate_wire_record(
            correlation_id=str(uuid4()), tenant_id=_BETA_TENANT_SLUG
        )
        assert envelope_tenant_identity(data) == _BETA_TENANT_SLUG

    def test_returns_none_for_an_unattributed_envelope(self) -> None:
        data = _quality_gate_wire_record(correlation_id=str(uuid4()), tenant_id=None)
        assert envelope_tenant_identity(data) is None

    def test_returns_none_rather_than_inventing_one_with_no_envelope(self) -> None:
        assert envelope_tenant_identity({"correlation_id": str(uuid4())}) is None


@pytest.mark.asyncio
class TestAsyncWriterBindsOneTenantToBothHalvesOfThePolicy:
    """``DelegationProjectionRunner._project_quality_gate_result`` (the class the
    ``omnimarket-projection-delegation-writer`` Deployment runs)."""

    async def test_unattributed_verdict_stamps_the_house_tenant_on_the_row(
        self,
    ) -> None:
        """RED before OMN-17422: the row carried NO ``tenant_id`` key at all, so
        the stored value was whatever the deployed column DEFAULT happened to be
        while the GUC was synthesised from ``_UUID_CONVERTED_TABLES``. That is
        the disagreement the RLS policy refused on onex-dev."""
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        runner._db = mock_db  # type: ignore[assignment]
        correlation_id = str(uuid4())

        projected = await runner._project_quality_gate_result(
            _quality_gate_wire_record(correlation_id=correlation_id, tenant_id=None),
            MessageMeta(partition=0, offset=1, fallback_id=correlation_id, topic="t"),
        )

        assert projected is True
        row = _upsert_row(mock_db)
        assert "tenant_id" in row, (
            "the verdict row must record its own tenant; leaving it to the "
            "column DEFAULT is what let the stored value and the app.tenant_id "
            "GUC disagree (OMN-17422)"
        )
        assert row["tenant_id"] in {HOUSE_TENANT_SLUG, str(HOUSE_TENANT_UUID)}

    async def test_row_tenant_equals_the_guc_the_same_write_binds(self) -> None:
        """The whole policy comparison, asserted as one property: whatever the
        row stores for ``tenant_id`` is exactly what ``resolve_write_tenant``
        will set ``app.tenant_id`` to for that same statement. RED before
        OMN-17422 because the row had no ``tenant_id`` for the resolver to
        read."""
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        runner._db = mock_db  # type: ignore[assignment]
        correlation_id = str(uuid4())

        await runner._project_quality_gate_result(
            _quality_gate_wire_record(correlation_id=correlation_id, tenant_id=None),
            MessageMeta(partition=0, offset=1, fallback_id=correlation_id, topic="t"),
        )

        row = _upsert_row(mock_db)
        bound_guc = _upsert_call(mock_db).kwargs["tenant"]
        assert row["tenant_id"] == bound_guc

    async def test_verdict_is_attributed_to_the_tenant_the_envelope_recorded(
        self,
    ) -> None:
        """RED before OMN-17422: the producer-recorded tenant was ignored
        outright, so a beta-tenant delegation's verdict was written under the
        house tenant -- refused by the policy when the delegation row already
        existed under the real tenant, and invisible to the tenant-scoped read
        when it did not."""
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        runner._db = mock_db  # type: ignore[assignment]
        correlation_id = str(uuid4())

        await runner._project_quality_gate_result(
            _quality_gate_wire_record(
                correlation_id=correlation_id, tenant_id=_BETA_TENANT_SLUG
            ),
            MessageMeta(partition=0, offset=1, fallback_id=correlation_id, topic="t"),
        )

        row = _upsert_row(mock_db)
        assert row["tenant_id"] == _BETA_TENANT_UUID
        assert _upsert_call(mock_db).kwargs["tenant"] == _BETA_TENANT_UUID

    async def test_existing_row_probe_is_scoped_to_the_same_tenant_as_the_write(
        self,
    ) -> None:
        """An RLS-enforced writer that probes under one tenant and writes under
        another cannot see the delegation row it is annotating, and stamps a
        fresh ``created_at`` over a terminal event's own."""
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()
        runner._db = mock_db  # type: ignore[assignment]
        correlation_id = str(uuid4())

        await runner._project_quality_gate_result(
            _quality_gate_wire_record(
                correlation_id=correlation_id, tenant_id=_BETA_TENANT_SLUG
            ),
            MessageMeta(partition=0, offset=1, fallback_id=correlation_id, topic="t"),
        )

        probes = [
            call
            for call in mock_db.execute.await_args_list
            if str(call.args[0]).lstrip().startswith("SELECT 1 FROM delegation_events")
        ]
        assert probes, "expected the existing-row probe"
        assert probes[-1].kwargs["tenant"] == _BETA_TENANT_UUID

    async def test_existing_row_is_never_re_attributed_but_the_guc_is_still_bound(
        self,
    ) -> None:
        """Attribution belongs to whoever CREATED the row. A verdict annotating a
        delegation row a terminal event already recorded must leave ``tenant_id``
        out of the statement entirely (so ``DO UPDATE SET`` cannot rewrite it),
        while still binding ``app.tenant_id`` to the tenant it resolved -- the
        policy's ``WITH CHECK`` is evaluated against the UNCHANGED stored tenant,
        so an unbound or house-defaulted GUC refuses the update."""
        runner = DelegationProjectionRunner()
        mock_db = _mock_db()

        async def _execute(query: str, *_args: Any, **_kwargs: Any) -> Any:
            if str(query).lstrip().startswith("SELECT 1 FROM delegation_events"):
                return [{"?column?": 1}]
            return []

        mock_db.execute = AsyncMock(side_effect=_execute)
        runner._db = mock_db  # type: ignore[assignment]
        correlation_id = str(uuid4())

        await runner._project_quality_gate_result(
            _quality_gate_wire_record(
                correlation_id=correlation_id, tenant_id=_BETA_TENANT_SLUG
            ),
            MessageMeta(partition=0, offset=1, fallback_id=correlation_id, topic="t"),
        )

        call = _upsert_call(mock_db)
        sql = str(call.args[0])
        insert_columns, update_clause = sql.split("ON CONFLICT", 1)
        assert "tenant_id" in insert_columns, (
            "the proposed INSERT row must NAME its tenant -- Postgres evaluates "
            "the policy WITH CHECK against it before the conflict is resolved, "
            "so an omitted tenant_id inherits the column DEFAULT and is refused "
            "whenever app.tenant_id is anything else (OMN-17422)"
        )
        assert "tenant_id" not in update_clause, (
            "a verdict must never re-attribute a delegation row a terminal "
            "event already recorded"
        )
        assert call.kwargs["tenant"] == _BETA_TENANT_UUID


class TestSyncTwinKeepsTheSameRule:
    """``HandlerProjectionDelegation`` -- the shared-kernel twin. The two write
    paths must not drift on attribution."""

    def test_unattributed_verdict_stamps_the_house_tenant_on_the_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionDelegation()
        correlation_id = uuid4()

        handler.project_quality_gate_result(
            ModelQualityGateResult(
                correlation_id=correlation_id,
                passed=True,
                quality_score=1.0,
                actual_score=1.0,
                score_source=SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
            ),
            db,
            event_timestamp=_ENVELOPE_TIMESTAMP,
        )

        rows = db.query(TABLE, {"correlation_id": str(correlation_id)})
        assert rows, "expected the verdict row"
        assert rows[0]["tenant_id"] in {HOUSE_TENANT_SLUG, str(HOUSE_TENANT_UUID)}
        assert rows[0]["tenant_id"] == resolve_write_tenant(
            rows[0]["tenant_id"], table=TABLE
        )

    def test_dispatch_shim_threads_the_envelope_tenant_through(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionDelegation()
        correlation_id = str(uuid4())
        payload = _quality_gate_wire_record(
            correlation_id=correlation_id, tenant_id=_BETA_TENANT_SLUG
        )
        payload["_db"] = db
        payload["_event_type"] = "omnibase-infra.quality-gate-result"

        handler.handle(payload)

        rows = db.query(TABLE, {"correlation_id": correlation_id})
        assert rows, "expected the verdict row"
        assert rows[0]["tenant_id"] == _BETA_TENANT_UUID


# ---------------------------------------------------------------------------
# Real-Postgres RLS gate.
#
# Every assertion above runs against a mock DB, which accepts any GUC/row pair
# without complaint -- exactly the blind spot that let this defect reach a
# deployed writer. Only a real connection, as a NON-superuser role, with
# FORCE ROW LEVEL SECURITY actually binding, evaluates the policy that refused
# the write on onex-dev. Superusers are never subject to RLS (FORCE or not), so
# the established ``_provisioned_runner`` harnesses in
# tests/test_omn15909_real_postgres_projection_write_path_gate.py and
# tests/test_writer_tenant_isolation_omn14898.py cannot reach this boundary --
# this module provisions its own least-privilege LOGIN role for it.
#
# SKIPS (never ERRORs) without a reachable database, per the module idiom.
# ---------------------------------------------------------------------------

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)

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


def _pg_settings() -> tuple[str, str, str, str, str]:
    return (
        os.environ.get("INTEGRATION_POSTGRES_USER", "postgres"),
        os.environ.get(
            "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
        ),
        os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost"),
        os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"),
        os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra"),
    )


def _dsn_for(user: str, secret: str) -> str:
    _, _, host, port, db = _pg_settings()
    return f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"


async def _admin_or_skip() -> asyncpg.Connection:
    user, secret, _, _, _ = _pg_settings()
    if not secret:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD / POSTGRES_PASSWORD not set -- "
            "skipping the OMN-17422 real-Postgres RLS gate"
        )
    try:
        return await asyncpg.connect(_dsn_for(user, secret))
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-17422 RLS gate: {exc}")


@asynccontextmanager
async def _rls_enforced_runner() -> AsyncIterator[
    tuple[DelegationProjectionRunner, asyncpg.Connection, str]
]:
    """Yield ``(runner, admin_conn, writer_role)`` where the runner's pool is
    authenticated as a NOSUPERUSER / NOBYPASSRLS role, so migration 0023's
    ``tenant_isolation`` policy actually binds its writes."""
    admin = await _admin_or_skip()
    suffix = uuid4().hex[:12]
    schema = f"omn17422_{suffix}"
    writer_role = f"omn17422_w_{suffix}"
    writer_secret = uuid4().hex
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}, public")
        await admin.execute(_APP_DASHBOARD_ROLE_SQL)
        for migration_path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await admin.execute(
                migration_path.read_text(encoding="utf-8").replace(
                    "CREATE INDEX CONCURRENTLY", "CREATE INDEX"
                )
            )
        await admin.execute(
            f"CREATE ROLE {writer_role} WITH LOGIN PASSWORD '{writer_secret}' "
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
        )
        await admin.execute(
            f'GRANT CONNECT ON DATABASE "{_pg_settings()[4]}" TO {writer_role}'
        )
        await admin.execute(f"GRANT USAGE ON SCHEMA {schema} TO {writer_role}")
        # The tenant-registry mirror is a PUBLIC-schema relation another node's
        # migration owns. The deployed writer role can read it; this throwaway
        # role must be able to too, or the slug -> UUID resolution fails on a
        # permission error rather than exercising the seam under test. Guarded
        # because a lane that has not applied the OMN-16930 migration has no
        # such relation at all (the resolver degrades to the closed legacy map
        # there, which this test also covers).
        await admin.execute(
            f"""
            DO $$
            BEGIN
              IF to_regclass('public.tenant_registry_mirror') IS NOT NULL THEN
                EXECUTE 'GRANT SELECT ON public.tenant_registry_mirror TO {writer_role}';
              END IF;
            END;
            $$;
            """
        )
        await admin.execute(
            f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {schema} "
            f"TO {writer_role}"
        )
        pool = await asyncpg.create_pool(
            _dsn_for(writer_role, writer_secret),
            min_size=1,
            max_size=2,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_dsn_for(writer_role, writer_secret))
        adapter._pool = pool  # type: ignore[attr-defined]
        runner = DelegationProjectionRunner()
        runner._db = adapter  # type: ignore[assignment]
        yield runner, admin, writer_role
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin.execute("SET search_path TO public")
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await admin.execute(f"DROP ROLE IF EXISTS {writer_role}")
        await admin.close()


async def _seed_beta_delegation_row(
    admin: asyncpg.Connection, correlation_id: str
) -> None:
    """A delegation row created by the terminal path under the real tenant."""
    async with admin.transaction():
        await admin.execute(
            "SELECT set_config('app.tenant_id', $1, true)", _BETA_TENANT_UUID
        )
        await admin.execute(
            "INSERT INTO delegation_events "
            "(correlation_id, tenant_id, task_type, delegated_to, model_name, "
            " created_at) "
            "VALUES ($1, $2::uuid, 'code-review', 'glm-5.2', 'glm-5.2', NOW())",
            correlation_id,
            _BETA_TENANT_UUID,
        )


@pytest.mark.integration
@pytest.mark.asyncio
class TestRealPostgresRlsRefusesTheUnboundWriteAndAcceptsTheBoundOne:
    async def test_red_house_tenant_guc_is_refused_against_a_beta_tenant_row(
        self,
    ) -> None:
        """RED control, proven by execution rather than argument: the pre-fix
        shape -- a verdict row with no ``tenant_id`` written under the house
        tenant GUC -- is refused by the live policy when the delegation row
        belongs to another tenant. This is the onex-dev log line verbatim."""
        async with _rls_enforced_runner() as (runner, admin, _role):
            correlation_id = str(uuid4())
            await _seed_beta_delegation_row(admin, correlation_id)

            with pytest.raises(asyncpg.PostgresError) as excinfo:
                await runner.db.execute(
                    "INSERT INTO delegation_events "
                    "(correlation_id, quality_gate_passed) VALUES ($1, $2) "
                    "ON CONFLICT (correlation_id) DO UPDATE SET "
                    "quality_gate_passed = EXCLUDED.quality_gate_passed",
                    correlation_id,
                    True,
                    tenant=str(HOUSE_TENANT_UUID),
                )
            assert "row-level security policy" in str(excinfo.value)

    async def test_red_the_statement_shape_alone_is_refused_even_on_the_right_tenant(
        self,
    ) -> None:
        """The non-obvious half, isolated. Even with ``app.tenant_id`` bound to
        the row's OWN tenant, a targeted-column UPSERT that omits ``tenant_id``
        is refused -- because the policy's WITH CHECK is evaluated against the
        proposed INSERT row, which carries the column DEFAULT. The equivalent
        plain UPDATE of the same row under the same GUC succeeds, which is what
        proves the refusal is about the statement shape and not about the
        tenant."""
        async with _rls_enforced_runner() as (runner, admin, _role):
            correlation_id = str(uuid4())
            await _seed_beta_delegation_row(admin, correlation_id)

            with pytest.raises(asyncpg.PostgresError) as excinfo:
                await runner.db.execute(
                    "INSERT INTO delegation_events "
                    "(correlation_id, quality_gate_passed) VALUES ($1, $2) "
                    "ON CONFLICT (correlation_id) DO UPDATE SET "
                    "quality_gate_passed = EXCLUDED.quality_gate_passed",
                    correlation_id,
                    True,
                    tenant=_BETA_TENANT_UUID,
                )
            assert "row-level security policy" in str(excinfo.value)

            await runner.db.execute(
                "UPDATE delegation_events SET quality_gate_passed = $2 "
                "WHERE correlation_id = $1",
                correlation_id,
                True,
                tenant=_BETA_TENANT_UUID,
            )

    async def test_green_envelope_bound_tenant_lands_the_verdict(self) -> None:
        """The fixed path: the verdict binds the tenant the envelope recorded,
        the policy accepts it, the verdict columns land, and the row's own
        attribution is left exactly as the terminal event recorded it."""
        async with _rls_enforced_runner() as (runner, admin, _role):
            correlation_id = str(uuid4())
            await _seed_beta_delegation_row(admin, correlation_id)

            projected = await runner._project_quality_gate_result(
                _quality_gate_wire_record(
                    correlation_id=correlation_id, tenant_id=_BETA_TENANT_SLUG
                ),
                MessageMeta(
                    partition=0, offset=1, fallback_id=correlation_id, topic="t"
                ),
            )
            assert projected is True

            async with admin.transaction():
                await admin.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", _BETA_TENANT_UUID
                )
                row = await admin.fetchrow(
                    "SELECT tenant_id, quality_gate_passed, score_source "
                    "FROM delegation_events WHERE correlation_id = $1",
                    correlation_id,
                )
            assert row is not None, (
                "the verdict must reach the projection plane -- an absent row is "
                "what the business-proof quality_gate check scores FAIL"
            )
            assert str(row["tenant_id"]) == _BETA_TENANT_UUID
            assert row["quality_gate_passed"] is True
            assert row["score_source"] == SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE

    async def test_green_unattributed_verdict_creates_a_house_tenant_row(
        self,
    ) -> None:
        """A verdict whose producer recorded no tenant creates its row under the
        house tenant with the value STAMPED, not defaulted -- the stored tenant
        and the GUC are the same string, so the policy accepts it whatever the
        deployed column DEFAULT is."""
        async with _rls_enforced_runner() as (runner, admin, _role):
            correlation_id = str(uuid4())

            projected = await runner._project_quality_gate_result(
                _quality_gate_wire_record(
                    correlation_id=correlation_id, tenant_id=None
                ),
                MessageMeta(
                    partition=0, offset=1, fallback_id=correlation_id, topic="t"
                ),
            )
            assert projected is True

            async with admin.transaction():
                await admin.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(HOUSE_TENANT_UUID),
                )
                row = await admin.fetchrow(
                    "SELECT tenant_id, quality_gate_passed FROM delegation_events "
                    "WHERE correlation_id = $1",
                    correlation_id,
                )
            assert row is not None
            assert str(row["tenant_id"]) == str(HOUSE_TENANT_UUID)
            assert row["quality_gate_passed"] is True
