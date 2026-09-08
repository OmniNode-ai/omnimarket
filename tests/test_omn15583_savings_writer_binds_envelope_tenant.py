# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15583: the savings projection writer must NAME the tenant the producer
recorded, and must never let ``savings_estimates``' column DEFAULT author it.

Live-measured cause (onex-dev, dev-system EC2 ``i-06169517a92b45f86``,
2026-09-08, recorded at ``docs/tracking/ROLLING_WORK_LEDGER.md:4718``).
``savings_estimates.tenant_id`` is ``TEXT NOT NULL DEFAULT 'omninode'``
(migration 080). ``SavingsProjectionRunner._upsert_savings_estimate`` -- the
deployed Kafka writer, and the only writer that has produced a row on that lane
-- listed FIFTEEN columns and ``tenant_id`` was not one of them, and issued the
statement through ``AsyncpgAdapter.execute`` with no ``tenant=``. So every row
it has ever written took the column DEFAULT:

    96 rows under 'omninode'          (newest 2026-09-08T10:02:41Z -- the SAME
                                       proof event whose delegation_events row
                                       is correctly UUID-stamped)
    16 rows under 'beta-business-proof' (last written 2026-08-01)
     0 rows under 91c74442-1233-4c97-b191-911a10346fdf (the proof tenant)

Since the reader fix (``omninode_infra#1236``, merged 2026-09-08T11:54:52Z)
binds the tenant UUID on both auth paths, ``GET /v1/tenants/me/savings`` for the
proof tenant answers ``data_state=insufficient_data``. The fix is WRITER-side
only: RULING ``docs/tracking/ROLLING_WORK_LEDGER.md:4667`` says tenant identity
is deployment-scoped with ONE authoritative form, the UUID, so the reader must
NOT grow a second identifier form to meet a slug-stamped row halfway.

Same defect class as OMN-17422 (the quality-gate verdict omitted ``tenant_id``
and let the DEFAULT stand in) and OMN-15919 (the adapter derived the GUC half
from a read-path resolver that had never seen the row). This module is the
savings-side proof, and it is deliberately shaped as RED CONTROL / GREEN pairs:
each control names the pre-fix shape and proves it is refused or wrong, so a
regression cannot pass by re-baselining an assertion.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    TenantScopedWriteUnboundError,
)
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
)

SAVINGS_ESTIMATED_TOPIC = "onex.evt.omnibase-infra.savings-estimated.v1"
DELEGATION_COMPLETED_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
DELEGATE_SKILL_COMPLETED_TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"

# The beta proof tenant. Its UUID is the identity the delegation writer already
# stamps on ``delegation_events`` and the identity the reader binds since
# omninode_infra#1236 -- it is quoted here so the assertion is about the ONE
# authoritative form, not about "some UUID".
BETA_TENANT_UUID = "91c74442-1233-4c97-b191-911a10346fdf"
BETA_TENANT_SLUG = "beta-business-proof"

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_savings"
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


def _meta(topic: str) -> MessageMeta:
    return MessageMeta(topic=topic, partition=0, offset=1, fallback_id=str(uuid4()))


def _mock_db(*, registry_uuid: str | None) -> AsyncMock:
    """An async DB double whose ``fetchval`` answers as ``tenant_registry_mirror``.

    ``registry_uuid=None`` models a lane whose mirror holds no row for the
    identity -- the resolver then falls to the closed legacy map, and raises for
    anything that map does not already contain.
    """
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(
        return_value=UUID(registry_uuid) if registry_uuid is not None else None
    )
    return db


def _savings_estimated_record(
    *, tenant_id: str | None, session_id: str | None = None
) -> dict[str, Any]:
    """The real ``savings-estimated.v1`` wire shape as ``unwrap_envelope`` hands it on.

    ``ModelSavingsEstimate`` (omnibase_infra ``node_savings_estimation_compute``)
    is the real producer; its field names are re-keyed by
    ``_normalize_savings_estimate_payload``. The payload model is
    ``extra="forbid"`` and declares NO tenant field, so the envelope stamp is the
    only producer-recorded attribution this path can ever have -- exactly the
    position ``ModelQualityGateResult`` is in on the delegation side.
    """
    payload = {
        "source_event_id": str(uuid4()),
        "session_id": session_id or str(uuid4()),
        "actual_model_id": "glm-5.2",
        "counterfactual_model_id": "claude-opus-4-6",
        "actual_cost_usd": "0.001000",
        "estimated_total_savings_usd": "0.500000",
        "timestamp_iso": "2026-09-08T10:02:41+00:00",
        "is_measured": True,
        "usage_source": "measured",
        "pricing_manifest_version": "2026-09-01",
    }
    envelope = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "envelope_timestamp": "2026-09-08T10:02:41+00:00",
        "correlation_id": str(uuid4()),
        "event_type": "omnibase-infra.savings-estimated",
        "tenant_id": tenant_id,
    }
    unwrapped = dict(payload)
    unwrapped["_envelope"] = envelope
    return unwrapped


def _canonical_delegation_record(*, tenant_id: str | None) -> dict[str, Any]:
    """A canonical ``delegation-completed.v1`` terminal, tenant on the PAYLOAD.

    This is the shape the delegation writer's own terminal path resolves
    ``event.tenant_id`` from, so the savings row and the ``delegation_events``
    row for one delegation are attributed from the one field.
    """
    payload: dict[str, Any] = {
        "correlation_id": str(uuid4()),
        "task_type": "code_generation",
        "model_used": "glm-5.2",
        "quality_passed": True,
        "cumulative_attempt_cost": 0.003,
        "final_attempt_cost": 0.003,
        "cumulative_input_tokens": 1000,
        "cumulative_output_tokens": 500,
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "timestamp": "2026-09-08T10:02:41+00:00",
    }
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    payload["_envelope"] = {
        "envelope_id": str(uuid4()),
        "envelope_timestamp": "2026-09-08T10:02:41+00:00",
        "event_type": "omnibase-infra.delegation-completed",
        "tenant_id": None,
    }
    return payload


def _insert_call(db: AsyncMock) -> Any:
    writes = [
        call
        for call in db.execute.await_args_list
        if "INSERT INTO savings_estimates" in str(call.args[0])
    ]
    assert writes, "expected a savings_estimates INSERT"
    return writes[-1]


def _insert_row(db: AsyncMock) -> dict[str, Any]:
    """The proposed row, reconstructed from the statement's own column list."""
    call = _insert_call(db)
    sql = str(call.args[0])
    columns_segment = sql.split("(", 1)[1].split(")", 1)[0]
    columns = [c.strip() for c in columns_segment.split(",") if c.strip()]
    return dict(zip(columns, call.args[1:], strict=True))


@pytest.mark.unit
class TestTheStatementNamesItsTenant:
    """RED CONTROL. Every one of these assertions FAILS against the pre-fix
    writer, whose INSERT named fifteen columns and no ``tenant_id``. That is the
    whole defect: a statement that does not name the column cannot state an
    attribution, whatever the runtime resolves.
    """

    def test_insert_column_list_names_tenant_id(self) -> None:
        db = _mock_db(registry_uuid=BETA_TENANT_UUID)
        runner = SavingsProjectionRunner()
        runner._db = db  # type: ignore[assignment]
        asyncio.run(
            runner.project_event(
                SAVINGS_ESTIMATED_TOPIC,
                _savings_estimated_record(tenant_id=BETA_TENANT_UUID),
                _meta(SAVINGS_ESTIMATED_TOPIC),
            )
        )
        assert "tenant_id" in _insert_row(db), (
            "the savings INSERT must NAME tenant_id -- an omitted column hands "
            "the attribution to savings_estimates' DEFAULT 'omninode'"
        )

    def test_tenant_id_is_insert_only_and_never_re_attributes_a_row(self) -> None:
        """``tenant_id`` must not appear in ``DO UPDATE SET``.

        ``savings_estimates`` upserts on
        (session_id, event_timestamp, model_local, model_cloud_baseline): a later
        event for the same key may refine the costs it measured, but must never
        move a row to a different tenant. Same rule, same reason, as
        ``delegation_events`` (OMN-17422).
        """
        db = _mock_db(registry_uuid=BETA_TENANT_UUID)
        runner = SavingsProjectionRunner()
        runner._db = db  # type: ignore[assignment]
        asyncio.run(
            runner.project_event(
                SAVINGS_ESTIMATED_TOPIC,
                _savings_estimated_record(tenant_id=BETA_TENANT_UUID),
                _meta(SAVINGS_ESTIMATED_TOPIC),
            )
        )
        sql = str(_insert_call(db).args[0])
        insert_arm = sql.split("VALUES", 1)[0]
        assert "tenant_id" in insert_arm, (
            "precondition: the INSERT arm must name tenant_id, or the DO UPDATE "
            "assertion below is vacuous"
        )
        update_arm = sql.split("DO UPDATE SET", 1)[1]
        assert "tenant_id" not in update_arm.split("RETURNING", 1)[0], (
            "tenant_id must be INSERT-only: a later savings event must never "
            "re-attribute a row an earlier one already placed under a tenant"
        )


@pytest.mark.unit
class TestTheEnvelopeTenantBecomesTheRowTenant:
    def test_envelope_tenant_uuid_is_the_row_tenant_and_the_bound_guc(self) -> None:
        db = _mock_db(registry_uuid=BETA_TENANT_UUID)
        runner = SavingsProjectionRunner()
        runner._db = db  # type: ignore[assignment]
        ok = asyncio.run(
            runner.project_event(
                SAVINGS_ESTIMATED_TOPIC,
                _savings_estimated_record(tenant_id=BETA_TENANT_UUID),
                _meta(SAVINGS_ESTIMATED_TOPIC),
            )
        )
        assert ok is True
        row = _insert_row(db)
        assert row["tenant_id"] == BETA_TENANT_UUID
        assert row["tenant_id"] != HOUSE_TENANT_SLUG
        # Both halves of the RLS comparison come from ONE resolver -- the whole
        # point of OMN-15919.
        assert _insert_call(db).kwargs["tenant"] == BETA_TENANT_UUID

    def test_envelope_slug_resolves_through_the_registry_to_the_same_uuid(
        self,
    ) -> None:
        """A slug-shaped identity is NOT stored as a slug.

        There is one authoritative form on this surface (RULING
        ``ROLLING_WORK_LEDGER.md:4667``). Resolving the slug here is what stops
        the reader from ever needing a second one.
        """
        db = _mock_db(registry_uuid=BETA_TENANT_UUID)
        runner = SavingsProjectionRunner()
        runner._db = db  # type: ignore[assignment]
        asyncio.run(
            runner.project_event(
                SAVINGS_ESTIMATED_TOPIC,
                _savings_estimated_record(tenant_id=BETA_TENANT_SLUG),
                _meta(SAVINGS_ESTIMATED_TOPIC),
            )
        )
        row = _insert_row(db)
        assert row["tenant_id"] == BETA_TENANT_UUID
        assert row["tenant_id"] != BETA_TENANT_SLUG

    def test_payload_tenant_on_the_canonical_delegation_terminal(self) -> None:
        db = _mock_db(registry_uuid=BETA_TENANT_UUID)
        runner = SavingsProjectionRunner()
        runner._db = db  # type: ignore[assignment]
        ok = asyncio.run(
            runner.project_event(
                DELEGATION_COMPLETED_TOPIC,
                _canonical_delegation_record(tenant_id=BETA_TENANT_UUID),
                _meta(DELEGATION_COMPLETED_TOPIC),
            )
        )
        assert ok is True
        assert _insert_row(db)["tenant_id"] == BETA_TENANT_UUID


@pytest.mark.unit
class TestTheAbsentAndUnresolvableCasesAreDifferentThings:
    def test_no_recorded_tenant_stamps_the_house_tenant_explicitly(self) -> None:
        """Absent attribution is the house tenant, STATED by the writer.

        The stored byte is the one the column DEFAULT would have supplied; what
        changes is that the row records a decision instead of an accident
        (OMN-16831, operator ruling 2026-08-28, option D).
        """
        db = _mock_db(registry_uuid=None)
        runner = SavingsProjectionRunner()
        runner._db = db  # type: ignore[assignment]
        asyncio.run(
            runner.project_event(
                SAVINGS_ESTIMATED_TOPIC,
                _savings_estimated_record(tenant_id=None),
                _meta(SAVINGS_ESTIMATED_TOPIC),
            )
        )
        row = _insert_row(db)
        assert "tenant_id" in row, "the key is NAMED even for the house tenant"
        assert row["tenant_id"] == HOUSE_TENANT_SLUG
        assert _insert_call(db).kwargs["tenant"] == HOUSE_TENANT_SLUG
        db.fetchval.assert_not_awaited()

    def test_unresolvable_recorded_tenant_refuses_and_writes_nothing(self) -> None:
        """A tenant NOBODY can resolve is quarantined, never house-stamped.

        This is the assertion that keeps the fix honest: the easy way to make a
        savings row appear for every event is to fall back to ``'omninode'`` on
        any resolution failure, which is precisely the 96-row defect. The
        refusal reaches the runner's POISON path and the contract-declared DLQ.
        """
        db = _mock_db(registry_uuid=None)
        runner = SavingsProjectionRunner()
        runner._db = db  # type: ignore[assignment]
        with pytest.raises(TenantRegistryResolutionError):
            asyncio.run(
                runner.project_event(
                    SAVINGS_ESTIMATED_TOPIC,
                    _savings_estimated_record(
                        tenant_id="a1b2c3d4-0000-4000-8000-000000000001"
                    ),
                    _meta(SAVINGS_ESTIMATED_TOPIC),
                )
            )
        writes = [
            call
            for call in db.execute.await_args_list
            if "INSERT INTO savings_estimates" in str(call.args[0])
        ]
        assert writes == [], "a refused write must leave zero rows, not a house row"


@pytest.mark.unit
class TestTheAdapterGuardIsWhatMakesThisUnforgettable:
    def test_the_statement_is_refused_when_the_tenant_is_not_threaded(self) -> None:
        """OMN-15919 sentinel, scoped to this table.

        Now that the statement NAMES ``tenant_id``, the adapter refuses it
        outright if a future edit drops the ``tenant=`` argument -- before a
        connection is acquired, so a refused write issues no statement. This is
        the mechanism that makes the fix hold without anyone remembering it.
        """
        adapter = AsyncpgAdapter(dsn="postgresql://unused/unused")
        with pytest.raises(TenantScopedWriteUnboundError):
            asyncio.run(
                adapter.execute(
                    "INSERT INTO savings_estimates (session_id, tenant_id) "
                    "VALUES ($1, $2)",
                    "sess-1",
                    BETA_TENANT_UUID,
                )
            )


# ---------------------------------------------------------------------------
# Real Postgres. SKIPS (never ERRORs) without a reachable database.
# ---------------------------------------------------------------------------


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
            "skipping the OMN-15583 real-Postgres savings write-path gate"
        )
    try:
        return await asyncpg.connect(_dsn_for(user, secret))
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-15583 savings gate: {exc}")


@asynccontextmanager
async def _rls_enforced_savings_runner() -> AsyncIterator[
    tuple[SavingsProjectionRunner, asyncpg.Connection]
]:
    """Yield ``(runner, admin_conn)`` where the runner writes as a NOSUPERUSER /
    NOBYPASSRLS role, so migration 081's ``tenant_isolation`` policy actually
    binds -- a superuser writer bypasses RLS and would prove nothing."""
    admin = await _admin_or_skip()
    suffix = uuid4().hex[:12]
    schema = f"omn15583_{suffix}"
    writer_role = f"omn15583_w_{suffix}"
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
        await admin.execute(
            f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {schema} "
            f"TO {writer_role}"
        )
        await admin.execute(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {writer_role}"
        )
        pool = await asyncpg.create_pool(
            _dsn_for(writer_role, writer_secret),
            min_size=1,
            max_size=2,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_dsn_for(writer_role, writer_secret))
        adapter._pool = pool  # type: ignore[attr-defined]
        runner = SavingsProjectionRunner()
        runner._db = adapter  # type: ignore[assignment]
        yield runner, admin
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin.execute("SET search_path TO public")
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await admin.execute(f"DROP ROLE IF EXISTS {writer_role}")
        await admin.close()


@pytest.mark.integration
@pytest.mark.asyncio
class TestRealPostgresSavingsWritePath:
    async def test_red_the_pre_fix_statement_lands_under_the_house_slug(self) -> None:
        """RED CONTROL, executed rather than asserted from memory.

        The pre-fix fifteen-column statement, run verbatim against the real
        migrated schema while the session is bound to the beta tenant, produces
        a row attributed to ``'omninode'`` -- because the column DEFAULT, not the
        writer, decided. This is the 96-row defect reproduced in one statement,
        and it is what every assertion above is measured against.
        """
        async with _rls_enforced_savings_runner() as (_runner, admin):
            async with admin.transaction():
                await admin.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", BETA_TENANT_UUID
                )
                await admin.execute(
                    "INSERT INTO savings_estimates ("
                    "  event_timestamp, session_id, model_local,"
                    "  model_cloud_baseline, local_cost_usd, cloud_cost_usd,"
                    "  savings_usd) "
                    "VALUES ($1, $2, 'glm-5.2', 'claude-opus-4-6', 0.001, 0.501, 0.5)",
                    datetime(2026, 9, 8, 10, 2, 41, tzinfo=UTC),
                    "sess-red-control",
                )
                stored = await admin.fetchval(
                    "SELECT tenant_id FROM savings_estimates WHERE session_id = $1",
                    "sess-red-control",
                )
            assert stored == HOUSE_TENANT_SLUG, (
                "the pre-fix shape is expected to land under the house slug -- "
                "if this control ever stops reproducing, the premise of this "
                "whole module has changed and the green tests below are vacuous"
            )
            assert stored != BETA_TENANT_UUID

    async def test_green_the_writer_lands_the_row_under_the_envelope_tenant(
        self,
    ) -> None:
        """The same lane, the same schema, the real writer: the row is the
        tenant's."""
        async with _rls_enforced_savings_runner() as (runner, admin):
            session_id = f"sess-{uuid4().hex[:8]}"
            record = _savings_estimated_record(
                tenant_id=BETA_TENANT_UUID, session_id=session_id
            )
            ok = await runner.project_event(
                SAVINGS_ESTIMATED_TOPIC, record, _meta(SAVINGS_ESTIMATED_TOPIC)
            )
            assert ok is True

            async with admin.transaction():
                await admin.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", BETA_TENANT_UUID
                )
                visible = await admin.fetch(
                    "SELECT tenant_id, session_id FROM savings_estimates "
                    "WHERE session_id = $1",
                    session_id,
                )
            assert len(visible) == 1
            assert visible[0]["tenant_id"] == BETA_TENANT_UUID

            # NEGATIVE CONTROL. The same row, read under the house-slug GUC the
            # pre-fix writer's rows sit behind, is INVISIBLE -- so the assertion
            # above is about tenant scoping and not about the row merely
            # existing.
            async with admin.transaction():
                await admin.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", HOUSE_TENANT_SLUG
                )
                under_house = await admin.fetch(
                    "SELECT session_id FROM savings_estimates WHERE session_id = $1",
                    session_id,
                )
            assert under_house == []

    async def test_green_an_unattributed_event_lands_an_explicit_house_row(
        self,
    ) -> None:
        async with _rls_enforced_savings_runner() as (runner, admin):
            session_id = f"sess-{uuid4().hex[:8]}"
            ok = await runner.project_event(
                SAVINGS_ESTIMATED_TOPIC,
                _savings_estimated_record(tenant_id=None, session_id=session_id),
                _meta(SAVINGS_ESTIMATED_TOPIC),
            )
            assert ok is True
            async with admin.transaction():
                await admin.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", HOUSE_TENANT_SLUG
                )
                rows = await admin.fetch(
                    "SELECT tenant_id FROM savings_estimates WHERE session_id = $1",
                    session_id,
                )
            assert len(rows) == 1
            assert rows[0]["tenant_id"] == HOUSE_TENANT_SLUG
