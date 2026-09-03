# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15909: real-Postgres write-path integration gate for the delegation
projection (``DelegationProjectionRunner``, node_projection_delegation).

Why this module exists, and why it is NOT redundant with
``tests/test_omn15905_delegation_projection_writer_seam.py``: that seam test
drives the same ``project_event()`` entrypoint but against an ``AsyncMock`` DB
double, which accepts a bound parameter of ANY Python type -- a wall-clock
``.isoformat()`` string binds just as "successfully" as a real
``datetime.datetime``. Only a real Postgres connection enforces column types
via asyncpg's extended query protocol. That gap is exactly how the OMN-15905
str-where-datetime defect (``event.timestamp or now`` bound to a
``TIMESTAMPTZ`` column) reached a merged, deployed, CrashLoopBackOff-ing
runtime: every layer of mock-DB coverage passed.

Real Postgres, never SQLite: SQLite's dynamic, loosely-affinity-typed columns
accept a Python `str` into a column declared TIMESTAMP without complaint --
the exact hole this gate exists to close. asyncpg against real Postgres is
also the actual runtime the deployed writer uses (``AsyncpgAdapter``), so this
is not a second, different code path from what production runs.

Structure:
  1. ``TestRealPostgresEndToEndWritePath`` -- drives the REAL
     ``DelegationProjectionRunner.project_event()`` end to end (canonical
     terminal + quality-gate-result + budget-state materialization) against a
     real, migrated Postgres schema and asserts a correctly-typed row lands.
  2. ``TestRedProofFixtures`` -- isolates the exact write primitive
     (``_dynamic_upsert``, the shared seam every write path in this class
     funnels through) and proves each of the three escape-class defects is
     REJECTED by real Postgres, and that the corrected shape is ACCEPTED --
     paired RED/GREEN tests per defect class, independent of whichever higher
     -level caller happens to be bug-free on a given day.

Harness pattern (asyncpg ``_connect_or_skip`` / disposable schema / guarded
``app_dashboard`` role) mirrors ``tests/test_writer_tenant_isolation_omn14898.py``
and ``tests/test_rls_tranche2_omn14894.py`` byte-for-byte in spirit -- SKIPS
(never ERRORs) without a reachable database, and provisions its own throwaway
schema so runs never collide.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest
from omnibase_core.models.delegation.wire import EnumTierCostType, ModelTierCost

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    SNAPSHOT_GRAIN_COLUMN,
    DelegationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import TenantRequiredError

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)


def _live_migration_files() -> list[Path]:
    """Every migration for this node, in the SAME numeric/lexical order the
    real migration runner applies them.

    Deliberately the FULL live set, not just the four (0007/0019/0022/0023)
    that create the base tables + tenant_id + RLS: the canonical
    delegation-completed.v1 write path (``_project_typed_event_async``)
    writes columns (``context_pack_hash`` from 0020,
    ``actual_score``/``quality_gate_detail`` from 0016_delegation_quality_bar
    _evidence, etc.) added by LATER migrations. A gate that only applies the
    base four tables passes for the wrong reason -- it never reaches most of
    the real column set the live writer touches, and would silently miss an
    UndefinedColumnError class of regression on any of those later-added
    columns. Confirmed empirically: driving project_event() end to end
    against only the base four raised
    ``asyncpg.exceptions.UndefinedColumnError: column "context_pack_hash" ...
    does not exist`` before this fix -- proof the narrower schema was
    insufficient, not merely a style preference.
    """
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def _test_schema_safe_sql(raw_sql: str) -> str:
    """Strip ``CONCURRENTLY`` from ``CREATE INDEX`` statements for test apply.

    ``CREATE INDEX CONCURRENTLY`` (migration 0029) refuses to run inside a
    transaction block, and asyncpg's simple-query ``execute()`` wraps a
    multi-statement string (this migration mixes it with three ``ALTER
    TABLE`` statements) in an implicit transaction, raising
    ``asyncpg.exceptions.ActiveSQLTransactionError``. ``CONCURRENTLY`` exists
    only to avoid locking a live table with real traffic -- irrelevant on a
    disposable, empty, single-connection test schema -- so a plain
    ``CREATE INDEX`` is schema-equivalent here.
    """
    return raw_sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")


# Minimal, self-contained mirror of omnibase_infra forward migration
# 094_create_app_dashboard_role.sql (OMN-14899, see
# tests/test_writer_tenant_isolation_omn14898.py for the same inline copy) --
# 0023 RAISEs if this cluster-wide role is absent. Idempotent (guarded
# duplicate_object catch), so safe to run once per test in this module.
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
ALTER ROLE app_dashboard
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
"""

_HANDLER_MODULE = (
    "omnimarket.nodes.node_projection_delegation.handlers.handler_delegation"
)


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
            "POSTGRES_PASSWORD not set -- skipping OMN-15909 real-Postgres "
            "write-path gate"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-15909 write-path gate: {exc}")


def _budgeted_tier() -> ModelTierCost:
    """Synthetic BUDGETED tier -- mirrors
    ``tests/test_writer_tenant_isolation_omn14898.py::_budgeted_tier``. No
    live tier in ``routing_tiers.yaml`` is BUDGETED today, so the budget-state
    materialization path is only reachable in tests by patching
    ``resolve_tier_cost`` to return one, exactly as the existing OMN-14898
    suite already does for the same reason.
    """
    return ModelTierCost(
        cost_type=EnumTierCostType.BUDGETED,
        rate_per_1k_usd=0.01,
        monthly_cap_usd=1.0,
        overage_rate_per_1k_usd=0.05,
    )


def _real_delegation_completed_payload(
    *, correlation_id: str, tenant_id: str
) -> dict[str, object]:
    """The real ``onex.evt.omnibase-infra.delegation-completed.v1`` shape.

    Byte-identical to
    ``tests/test_omn15905_delegation_projection_writer_seam.py::_real_delegation_completed_payload``
    -- the same vetted "real producer" fixture, reused rather than
    reinvented so both suites drive the exact same wire shape.
    """
    return {
        "correlation_id": correlation_id,
        "tenant_id": tenant_id,
        "task_type": "code-review",
        "model_used": "glm-5.2",
        "content": "the model's real answer",
        "quality_passed": True,
        "quality_score": 0.95,
        "latency_ms": 1800,
        "prompt_tokens": 210,
        "completion_tokens": 480,
        "cumulative_attempt_cost": 0.0142,
        "cost_tier_name": "cheap_cloud",
        "context_pack_hash": "sha256:omn15909-real-db-gate",
    }


def _real_quality_gate_result_payload(
    *, correlation_id: str, passed: bool
) -> dict[str, object]:
    """The real ``onex.evt.omnibase-infra.quality-gate-result.v1`` shape."""
    return {
        "correlation_id": correlation_id,
        "passed": passed,
        "fail_category": "pass" if passed else "fail_deterministic",
        "quality_score": 0.91 if passed else 0.40,
        "failure_reasons": () if passed else ("score_below_required_bar",),
        "score_source": "deterministic_acceptance",
    }


@asynccontextmanager
async def _provisioned_runner() -> AsyncIterator[
    tuple[DelegationProjectionRunner, asyncpg.Connection, str]
]:
    """Provision a disposable schema with the live migrated delegation
    projection schema, bind a real asyncpg-backed ``DelegationProjectionRunner``
    to it, and yield ``(runner, admin_conn, schema)``.

    ``admin_conn`` is a raw superuser connection scoped to the same schema via
    ``search_path`` -- used for readback assertions and for direct SQL the
    runner's own write path does not expose (row existence / column values).
    Writes go through ``runner`` (asyncpg pool, ``AsyncpgAdapter``); superuser
    writers bypass RLS regardless of migration 0023's FORCE ROW LEVEL
    SECURITY (see that migration's own BLAST RADIUS note), so this proves the
    write-path TYPE/shape contract, not the RLS boundary -- that boundary is
    already proven by
    ``tests/test_writer_tenant_isolation_omn14898.py::test_real_postgres_cross_tenant_write_is_rls_isolated_on_read``.
    """
    admin_conn = await _connect_or_skip()
    schema = f"omn15909_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        # app_dashboard must exist before ANY RLS migration in this set runs
        # (0023/0026/0027 each RAISE EXCEPTION if it is absent) -- create it
        # first, order relative to the base-table migrations does not matter.
        await admin_conn.execute(_APP_DASHBOARD_ROLE_SQL)
        for migration_path in _live_migration_files():
            sql = _test_schema_safe_sql(migration_path.read_text(encoding="utf-8"))
            await admin_conn.execute(sql)

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

        yield runner, admin_conn, schema
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


# ---------------------------------------------------------------------------
# 0. OMN-15800 AC6 regression sentinel: BaseProjectionRunner.__init__ now
#    lazy-imports AsyncpgAdapter (moved out of runner.py's module scope so
#    the projection-api process never loads asyncpg -- see
#    tests/integration/test_projection_bus_seam.py::
#    test_api_server_import_never_loads_asyncpg_or_psycopg2_in_sys_modules).
#    This proves the lazy import didn't silently break real DB connectivity:
#    constructing a BaseProjectionRunner subclass still yields a working
#    AsyncpgAdapter against a real, migrated Postgres schema.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOmn15800LazyAsyncpgImportStillConnects:
    async def test_runner_constructor_lazy_import_still_yields_working_db(
        self,
    ) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            # runner.db is the AsyncpgAdapter BaseProjectionRunner.__init__
            # constructed via the now-lazy `from omnimarket.adapters
            # .asyncpg_adapter import AsyncpgAdapter` -- proves the import
            # move didn't break attribute wiring or the pool binding this
            # fixture performs on top of it.
            assert isinstance(runner.db, AsyncpgAdapter)
            correlation_id = str(uuid4())
            # OMN-15683: delegation_events.tenant_id is now UUID; "acme-corp"
            # has no canonical mapping and would raise UnmappedTenantIdentityError
            # before this write ever reaches Postgres. Use a mapped tenant.
            data = _real_delegation_completed_payload(
                correlation_id=correlation_id, tenant_id="beta-business-proof"
            )
            meta = MessageMeta(partition=0, offset=0, fallback_id=correlation_id)

            ok = await runner.project_event(
                runner._topic_delegation_completed, data, meta
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT 1 FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None, "the lazy-import-backed write must still land"


# ---------------------------------------------------------------------------
# 1. End-to-end write path: the REAL DelegationProjectionRunner.project_event()
#    against real, migrated Postgres.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRealPostgresEndToEndWritePath:
    async def test_canonical_terminal_writes_correctly_typed_row(self) -> None:
        """delegation-completed.v1 -> delegation_events, driven through the
        FULL write path (typed event -> measured cost -> tenant stamp ->
        dynamic UPSERT), against real Postgres. This is the exact path that
        crashed in production with ``asyncpg.exceptions.DataError`` on the
        unmodified pre-OMN-15909 handler -- if the timestamp binding
        regresses to a string, this test fails with the identical exception
        the live CrashLoopBackOff carried, not a mock-DB false green.
        """
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())
            # OMN-15683: delegation_events.tenant_id is now UUID -- "acme-corp"
            # has no canonical mapping, so use a mapped tenant and assert the
            # resolved UUID rather than the raw slug.
            data = _real_delegation_completed_payload(
                correlation_id=correlation_id, tenant_id="beta-business-proof"
            )
            meta = MessageMeta(partition=0, offset=0, fallback_id=correlation_id)

            ok = await runner.project_event(
                runner._topic_delegation_completed, data, meta
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT * FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None, "the write must have landed a real row"
            assert row["tenant_id"] == UUID("91c74442-1233-4c97-b191-911a10346fdf")
            assert row["quality_gate_passed"] is True
            assert row["cost_tier_name"] == "cheap_cloud"
            assert float(row["cost_usd"]) > 0
            # The core column-TYPE proof: asyncpg reads TIMESTAMPTZ columns
            # back as real datetime objects. If the INSERT had instead bound
            # a string, the INSERT itself would have raised DataError and
            # this fetchrow would never see a row at all.
            assert isinstance(row["timestamp"], datetime)
            assert isinstance(row["created_at"], datetime)

    async def test_quality_gate_result_creates_row_with_real_created_at(
        self,
    ) -> None:
        """quality-gate-result.v1 (deterministic-scoring path, no LLM judge)
        -> delegation_events, first-arrival stamps created_at (OMN-13171
        pattern) via ``_project_quality_gate_result``, real Postgres.
        """
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())
            data = _real_quality_gate_result_payload(
                correlation_id=correlation_id, passed=True
            )
            meta = MessageMeta(partition=0, offset=1, fallback_id=correlation_id)

            ok = await runner.project_event(
                runner._topic_quality_gate_result, data, meta
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT * FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None
            assert row["quality_gate_passed"] is True
            assert row["score_source"] == "deterministic_acceptance"
            assert isinstance(row["created_at"], datetime)

    async def test_budget_state_materializes_with_real_timestamp_columns(
        self,
    ) -> None:
        """OMN-13235 event-sourced budget-state drawdown, real Postgres.

        Drives ``_materialize_budget_state_async`` directly (the precise
        write primitive OMN-15905's four defect sites span, one of which is
        this method) with a patched BUDGETED tier -- the same technique
        ``tests/test_writer_tenant_isolation_omn14898.py`` uses, since no
        live tier in ``routing_tiers.yaml`` is BUDGETED today.
        """
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            with patch(
                f"{_HANDLER_MODULE}.resolve_tier_cost",
                return_value=_budgeted_tier(),
            ):
                await runner._materialize_budget_state_async(
                    correlation_id=str(uuid4()),
                    cost_tier_name="ceiling-budgeted",
                    cost_measurement_source="measured",
                    budget_headroom_consumed_usd=0.05,
                    cost_usd=0.0,
                    tenant_id="acme-corp",
                    timestamp=None,
                )

            row = await admin_conn.fetchrow(
                "SELECT * FROM delegation_budget_state "
                "WHERE tenant_id = $1 AND cost_tier_name = $2",
                "acme-corp",
                "ceiling-budgeted",
            )
            assert row is not None
            assert float(row["consumed_usd"]) == pytest.approx(0.05)
            for column in (
                "first_event_at",
                "last_event_at",
                "created_at",
                "updated_at",
            ):
                assert isinstance(row[column], datetime), (
                    f"delegation_budget_state.{column} must bind a real "
                    f"datetime, not a string (got {type(row[column])!r})"
                )


# ---------------------------------------------------------------------------
# 2. RED proof fixtures: isolate the shared write primitive
#    (``_dynamic_upsert``) and prove each escape-class defect is caught by
#    real Postgres. Paired RED/GREEN per defect class.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRedProofFixtures:
    """Injects each OMN-15909 escape-class defect directly at the shared
    ``_dynamic_upsert`` write primitive (the seam every project_event() path
    in this class funnels through) and proves real Postgres rejects it --
    then proves the corrected shape is accepted. This is deliberately
    independent of whether any particular *caller* of ``_dynamic_upsert`` is
    bug-free on a given day: it is the proof that the MECHANISM (a real DB
    connection enforcing types/constraints) works, which is what makes this
    gate durable against a regression reintroduced at a fifth call site.
    """

    # -- (a) str-typed timestamp param -------------------------------------

    async def test_str_timestamp_param_is_rejected_by_real_postgres(self) -> None:
        """RED: THE bug that shipped. A wall-clock ``.isoformat()`` STRING
        bound to ``timestamp`` (TIMESTAMPTZ NOT NULL) -- byte-identical to
        the shape ``event.timestamp or now`` produced pre-OMN-15909 (where
        ``now = datetime.now(tz=UTC).isoformat()``) -- must raise
        ``asyncpg.exceptions.DataError`` and leave zero rows behind.
        """
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())
            bad_row: dict[str, object] = {
                "correlation_id": correlation_id,
                # OMN-15683: _dynamic_upsert bypasses handler-level slug->UUID
                # resolution entirely, so tenant_id here must already be a
                # raw UUID string -- this test's defect under proof is the
                # TIMESTAMP typing, not tenant resolution.
                "tenant_id": str(uuid4()),
                "task_type": "code-review",
                "delegated_to": "local-runtime",
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }
            with pytest.raises(asyncpg.exceptions.DataError):
                await runner._dynamic_upsert(
                    table="delegation_events",
                    conflict_key="correlation_id",
                    row=bad_row,
                )
            rows = await admin_conn.fetch(
                "SELECT 1 FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert rows == [], "a rejected write must never produce a partial row"

    async def test_real_datetime_timestamp_param_is_accepted(self) -> None:
        """GREEN: the identical row shape with a real ``datetime`` object
        (the OMN-15909 fix) succeeds and reads back as a real datetime.
        """
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())
            good_row: dict[str, object] = {
                "correlation_id": correlation_id,
                # OMN-15683: see the RED test above -- raw UUID string.
                "tenant_id": str(uuid4()),
                "task_type": "code-review",
                "delegated_to": "local-runtime",
                "timestamp": datetime.now(tz=UTC),
            }
            await runner._dynamic_upsert(
                table="delegation_events",
                conflict_key="correlation_id",
                row=good_row,
            )
            row = await admin_conn.fetchrow(
                "SELECT correlation_id, timestamp FROM delegation_events "
                "WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None
            assert isinstance(row["timestamp"], datetime)

    # -- (b) missing tenant_id stamp (fail-closed refusal, real DB) --------

    async def test_missing_tenant_id_refused_with_enforcement_zero_real_rows(
        self,
    ) -> None:
        """RED (fail-closed by design): with ENFORCE_TENANT_ISOLATION=True, a
        canonical terminal carrying no tenant_id must raise
        ``TenantRequiredError`` BEFORE any write reaches Postgres -- proven
        here against a REAL database (the OMN-14898 suite proves the same
        guard against ``InmemoryDatabaseAdapter`` only; this closes that gap
        for the real write path end to end).
        """
        from omnimarket.config.settings import Settings
        from omnimarket.projection import tenant_isolation as tenant_isolation_module

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            with patch.object(
                tenant_isolation_module,
                "get_settings",
                lambda: Settings(enforce_tenant_isolation=True),
            ):
                correlation_id = str(uuid4())
                data = _real_delegation_completed_payload(
                    correlation_id=correlation_id, tenant_id=""
                )
                del data["tenant_id"]
                meta = MessageMeta(partition=0, offset=2, fallback_id=correlation_id)

                with pytest.raises(TenantRequiredError):
                    await runner.project_event(
                        runner._topic_delegation_completed, data, meta
                    )

            rows = await admin_conn.fetch(
                "SELECT 1 FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert rows == [], (
                "a tenant-refused write must leave zero rows in the real DB"
            )

    async def test_present_tenant_id_accepted_with_enforcement_real_row(
        self,
    ) -> None:
        """GREEN: the same enforcement lane, but the source event carries a
        real tenant_id -- the write proceeds and the real row is stamped.
        """
        from omnimarket.config.settings import Settings
        from omnimarket.projection import tenant_isolation as tenant_isolation_module

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            with patch.object(
                tenant_isolation_module,
                "get_settings",
                lambda: Settings(enforce_tenant_isolation=True),
            ):
                correlation_id = str(uuid4())
                # OMN-15683: "acme-corp" has no canonical UUID mapping.
                data = _real_delegation_completed_payload(
                    correlation_id=correlation_id, tenant_id="beta-business-proof"
                )
                meta = MessageMeta(partition=0, offset=3, fallback_id=correlation_id)

                ok = await runner.project_event(
                    runner._topic_delegation_completed, data, meta
                )
                assert ok is True

            row = await admin_conn.fetchrow(
                "SELECT tenant_id FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None
            assert row["tenant_id"] == UUID("91c74442-1233-4c97-b191-911a10346fdf")

    # -- (c) undefined/renamed column ---------------------------------------

    async def test_undefined_column_is_rejected_by_real_postgres(self) -> None:
        """RED: a renamed/typo'd column name (the OMN-13350 class of defect,
        an event field that no longer matches a real column) must raise
        ``asyncpg.exceptions.UndefinedColumnError`` and leave zero rows.
        """
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())
            bad_row: dict[str, object] = {
                "correlation_id": correlation_id,
                "task_type": "code-review",
                "delegated_to": "local-runtime",
                "timestamp": datetime.now(tz=UTC),
                "nonexistent_column_omn15909": "value",
            }
            with pytest.raises(asyncpg.exceptions.UndefinedColumnError):
                await runner._dynamic_upsert(
                    table="delegation_events",
                    conflict_key="correlation_id",
                    row=bad_row,
                )
            rows = await admin_conn.fetch(
                "SELECT 1 FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert rows == []

    async def test_defined_column_is_accepted(self) -> None:
        """GREEN: the identical row shape minus the bad column succeeds."""
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())
            good_row: dict[str, object] = {
                "correlation_id": correlation_id,
                "task_type": "code-review",
                "delegated_to": "local-runtime",
                "timestamp": datetime.now(tz=UTC),
            }
            await runner._dynamic_upsert(
                table="delegation_events",
                conflict_key="correlation_id",
                row=good_row,
            )
            rows = await admin_conn.fetch(
                "SELECT 1 FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert len(rows) == 1


# ---------------------------------------------------------------------------
# 3. OMN-15919: real-Postgres RLS write-context resolver proof.
#
# ``_provisioned_runner`` above writes as the ``postgres`` SUPERUSER, which
# bypasses RLS unconditionally regardless of migration 0023's FORCE ROW LEVEL
# SECURITY (see that helper's own docstring) -- it structurally cannot
# exercise the OMN-15919 defect (GUC-vs-row tenant mismatch) at all, since a
# superuser write never evaluates a policy. This section provisions a
# genuinely non-superuser, NOBYPASSRLS LOGIN role -- the same shape the live
# k8s writer connects as -- so a regression here reproduces the exact live
# failure mode ("new row violates row-level security policy for table
# \"delegation_events\"", InsufficientPrivilegeError / SQLSTATE 42501), not a
# bypassed no-op.
# ---------------------------------------------------------------------------

_RLS_WRITER_ROLE = "omn15919_rls_writer"
_RLS_WRITER_PASSWORD = "omn15919-test-only-throwaway"

_RLS_WRITER_ROLE_SQL = f"""
DO $$
BEGIN
  BEGIN
    CREATE ROLE {_RLS_WRITER_ROLE} WITH
      LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
      PASSWORD '{_RLS_WRITER_PASSWORD}';
  EXCEPTION
    WHEN duplicate_object THEN
      NULL;
  END;
END;
$$;
ALTER ROLE {_RLS_WRITER_ROLE}
  LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD '{_RLS_WRITER_PASSWORD}';
"""


def _rls_writer_dsn(schema: str) -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    options = quote(f"-c search_path={schema},public")
    return (
        f"postgresql://{_RLS_WRITER_ROLE}:{_RLS_WRITER_PASSWORD}@{host}:{port}/{db}"
        f"?options={options}"
    )


@asynccontextmanager
async def _provisioned_rls_writer() -> AsyncIterator[
    tuple[DelegationProjectionRunner, AsyncpgAdapter, asyncpg.Connection, str]
]:
    """Provision a disposable schema, migrate it, grant a genuinely
    RLS-bound (non-superuser, NOBYPASSRLS) writer role real DML on it, and
    bind a ``DelegationProjectionRunner`` to a pool connected AS THAT ROLE
    (not the superuser ``admin_conn``, which is retained only for DDL setup
    and RLS-bypassing readback assertions).

    Yields ``(runner, adapter, admin_conn, schema)``.
    """
    admin_conn = await _connect_or_skip()
    schema = f"omn15919_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_APP_DASHBOARD_ROLE_SQL)
        await admin_conn.execute(_RLS_WRITER_ROLE_SQL)
        db_name = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        # The role has no CONNECT privilege on any database by default.
        await admin_conn.execute(
            f'GRANT CONNECT ON DATABASE "{db_name}" TO {_RLS_WRITER_ROLE}'
        )
        for migration_path in _live_migration_files():
            sql = _test_schema_safe_sql(migration_path.read_text(encoding="utf-8"))
            await admin_conn.execute(sql)
        # The writer needs real DML -- INSERT/UPDATE for the projection
        # writes themselves, SELECT for the handler's own existing-row
        # lookups (_preserve_existing_evidence_async, the quality-gate-result
        # idempotency check, the budget-state accumulation read).
        await admin_conn.execute(
            f"GRANT USAGE ON SCHEMA {schema} TO {_RLS_WRITER_ROLE}"
        )
        await admin_conn.execute(
            f"GRANT INSERT, UPDATE, SELECT ON ALL TABLES IN SCHEMA {schema} "
            f"TO {_RLS_WRITER_ROLE}"
        )

        pool = await asyncpg.create_pool(
            _rls_writer_dsn(schema),
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_rls_writer_dsn(schema))
        adapter._pool = pool  # type: ignore[attr-defined]

        runner = DelegationProjectionRunner()
        runner._db = adapter  # type: ignore[assignment]

        yield runner, adapter, admin_conn, schema
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


@pytest.mark.integration
class TestRlsWriteContextResolver:
    """OMN-15919: proves the GUC/row-tenant resolver mismatch is REJECTED by
    a genuinely RLS-bound writer role, and that threading the resolved
    tenant through ``AsyncpgAdapter``'s explicit ``tenant`` kwarg (the fix)
    is what makes the write -- and only a correctly-scoped read-back --
    succeed.
    """

    async def test_ambient_default_guc_with_mismatched_row_tenant_is_rejected(
        self,
    ) -> None:
        """Permanent regression sentinel for the OMN-15919 defect shape: a
        row stamped for a real tenant, written through ``AsyncpgAdapter``
        with NO explicit ``tenant`` kwarg -- the exact pre-fix
        ``_set_tenant_context`` shape, which fell through to
        ``resolve_read_tenant(None)`` (the house tenant) regardless of the
        row -- is rejected by real Postgres under a genuinely RLS-bound
        writer role. If a future call site regresses by omitting the
        ``tenant`` kwarg for a real-tenant row, this is the mechanism that
        catches it.

        OMN-15683 UPDATE: delegation_events.tenant_id is now UUID, and
        ``AsyncpgAdapter._set_tenant_context``'s no-``tenant``-kwarg fallback
        (``resolve_read_tenant(None)``, no table in scope) still returns the
        SLUG house-tenant default -- it is deliberately not made table-aware
        (that would require threading ``table`` through every
        ``AsyncpgAdapter`` public method). So for THIS table the rejection
        now happens one step earlier than before: the GUC itself fails the
        RLS policy's ``::uuid`` cast (``InvalidTextRepresentationError``,
        SQLSTATE 22P02) rather than the WITH CHECK permission clause
        (``InsufficientPrivilegeError``) -- the write is still rejected
        either way, which is what this sentinel actually guards.
        """
        async with _provisioned_rls_writer() as (_runner, adapter, admin_conn, _schema):
            correlation_id = str(uuid4())
            with pytest.raises(asyncpg.exceptions.InvalidTextRepresentationError):
                await adapter.execute(
                    "INSERT INTO delegation_events "
                    "(correlation_id, tenant_id, task_type, delegated_to, timestamp) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    correlation_id,
                    "91c74442-1233-4c97-b191-911a10346fdf",
                    "code-review",
                    "local-runtime",
                    datetime.now(tz=UTC),
                )
            rows = await admin_conn.fetch(
                "SELECT 1 FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert rows == [], "a rejected write must never produce a partial row"

    async def test_explicit_tenant_kwarg_matching_row_is_accepted_and_rls_scoped(
        self,
    ) -> None:
        """GREEN: the identical row, written WITH the resolved tenant
        threaded into the SAME transaction's GUC (the ``tenant=`` kwarg),
        succeeds -- and the row is visible back ONLY under that tenant's RLS
        scope, proving this is real RLS binding, not a bypassed no-op.
        """
        async with _provisioned_rls_writer() as (
            _runner,
            adapter,
            _admin_conn,
            _schema,
        ):
            correlation_id = str(uuid4())
            # OMN-15683: delegation_events.tenant_id is UUID -- both the row
            # value and the tenant= GUC kwarg must be the canonical UUID, not
            # the slug.
            beta_uuid = "91c74442-1233-4c97-b191-911a10346fdf"
            house_uuid = "820272f9-4aaf-5add-a2df-0af942852ab2"
            await adapter.execute(
                "INSERT INTO delegation_events "
                "(correlation_id, tenant_id, task_type, delegated_to, timestamp) "
                "VALUES ($1, $2, $3, $4, $5)",
                correlation_id,
                beta_uuid,
                "code-review",
                "local-runtime",
                datetime.now(tz=UTC),
                tenant=beta_uuid,
            )
            same_tenant = await adapter.execute(
                "SELECT tenant_id FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
                tenant=beta_uuid,
            )
            assert [dict(r) for r in same_tenant] == [{"tenant_id": UUID(beta_uuid)}]
            # A DIFFERENT valid UUID (not the slug -- that would fail the
            # policy's ::uuid cast outright rather than returning empty) must
            # see zero rows: real isolation, not a bypassed no-op.
            other_tenant = await adapter.execute(
                "SELECT tenant_id FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
                tenant=house_uuid,
            )
            assert other_tenant == [], (
                "the row must be invisible outside its own tenant's RLS scope"
            )

    async def test_delegation_completed_event_with_real_tenant_writes_and_reads_back_scoped(
        self,
    ) -> None:
        """RED/GREEN at the HANDLER level (the actual OMN-15919 escape
        fixture): the FULL ``project_event()`` path (typed event ->
        ``_project_typed_event_async`` -> ``_dynamic_upsert``), driven
        through a genuinely RLS-bound writer, for an event whose tenant
        (``beta-business-proof``) differs from the ambient house-tenant
        default -- the exact live business-proof-gate shape from the
        OMN-15919 diagnosis. Pre-fix, ``_dynamic_upsert`` called
        ``self.db.execute(query, *values)`` with no ``tenant`` kwarg, so this
        raised ``asyncpg.exceptions.InsufficientPrivilegeError`` (uncaught --
        ``project_event`` has no try/except around this path) instead of
        returning. Post-fix it returns ``True`` and the row lands under, and
        is only readable under, the correct tenant scope.
        """
        async with _provisioned_rls_writer() as (runner, _adapter, admin_conn, _schema):
            correlation_id = str(uuid4())
            data = _real_delegation_completed_payload(
                correlation_id=correlation_id, tenant_id="beta-business-proof"
            )
            meta = MessageMeta(partition=0, offset=0, fallback_id=correlation_id)

            ok = await runner.project_event(
                runner._topic_delegation_completed, data, meta
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT * FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None, "the write must have landed a real row"
            # OMN-15683: the handler resolves the verified slug to its
            # canonical UUID before the row ever reaches Postgres.
            assert row["tenant_id"] == UUID("91c74442-1233-4c97-b191-911a10346fdf")

    async def test_missing_envelope_tenant_falls_back_consistently_on_both_sides(
        self,
    ) -> None:
        """The event/row genuinely has no tenant -- ``_dynamic_upsert``
        resolves the SAME house-tenant fallback for the GUC
        (``resolve_write_tenant``) as the column DEFAULT the omitted key
        falls through to on INSERT, so the write succeeds under a real
        RLS-bound writer role. This is NOT a coincidental pre-fix match:
        pre-fix, both sides happened to land on ``'omninode'`` too, but for
        the wrong reason (an unconditional read-path resolver with no
        connection to the row at all) -- this test pins that the post-fix
        shape is a single shared derivation, not two independent resolvers
        that happen to agree today.
        """
        async with _provisioned_rls_writer() as (runner, _adapter, admin_conn, _schema):
            correlation_id = str(uuid4())
            row: dict[str, object] = {
                "correlation_id": correlation_id,
                "task_type": "code-review",
                "delegated_to": "local-runtime",
                "timestamp": datetime.now(tz=UTC),
            }
            await runner._dynamic_upsert(
                table="delegation_events", conflict_key="correlation_id", row=row
            )
            landed = await admin_conn.fetch(
                "SELECT tenant_id FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            # OMN-15683: resolve_write_tenant's interim-default fallback is
            # now table-aware -- for this UUID-converted table it returns the
            # house tenant's UUID (matching the column's own new UUID
            # DEFAULT), not the slug, so the GUC and the stored value still
            # agree by construction, just in the new representation.
            assert [dict(r) for r in landed] == [
                {"tenant_id": UUID("820272f9-4aaf-5add-a2df-0af942852ab2")}
            ]


# ---------------------------------------------------------------------------
# 4. OMN-17773: the singleton-aggregate republish issues REAL SQL against REAL
#    SQL VIEWS. A mock DB accepts any statement text and any returned shape,
#    so it can prove neither that the re-read parses nor that the row it
#    yields is serializable onto the bus. Both are only enforced by a real
#    Postgres connection against the real migrated views.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOmn17773AggregateSnapshotRepublish:
    async def test_aggregate_reread_runs_against_the_real_migrated_views(
        self,
    ) -> None:
        """Every declared singleton aggregate re-read parses and returns a row.

        The four exposures this covers are SQL VIEWS, not tables -- the write
        path never creates them, so a typo in a view name, a renamed view, or
        a migration that drops one is invisible to every mock-DB test in this
        repo and would surface only as a silent ``UndefinedTableError`` inside
        the deployed writer's exception handler, leaving the exposure
        permanently empty on the page while the writer reported healthy.
        """
        async with _provisioned_runner() as (runner, _admin_conn, _schema):
            assert runner._aggregate_exposures, (
                "the contract must declare at least one singleton aggregate"
            )
            for exposure in runner._aggregate_exposures:
                rows = await runner.db.execute(
                    f"SELECT $1::text AS {SNAPSHOT_GRAIN_COLUMN}, agg.* "
                    f"FROM {exposure.table} agg LIMIT 1",
                    exposure.topic,
                )
                assert rows, (
                    f"{exposure.table} must yield its singleton row; an "
                    "aggregate view that returns nothing publishes nothing"
                )
                assert rows[0][SNAPSHOT_GRAIN_COLUMN] == exposure.topic

    async def test_apply_publishes_serializable_aggregate_snapshots(self) -> None:
        """A real apply publishes one delta per aggregate, and the row a real
        Postgres returns survives ``model_dump_json``.

        The views return ``jsonb`` columns (``byTaskType``, ``byModel``,
        ``by_tier``, ``provenance_summary``) and ``numeric``/``timestamptz``
        values that asyncpg materializes as ``Decimal`` and ``datetime``. The
        mock-DB seam test feeds back plain Python literals, so it cannot catch
        a value the snapshot serializer refuses -- which would raise INSIDE
        the deployed writer on every event.
        """
        published: list[tuple[str, bytes | None, bytes | None]] = []

        class _CapturingProducer:
            async def send_and_wait(
                self,
                topic: str,
                value: bytes | None = None,
                key: bytes | None = None,
                headers: list[tuple[str, bytes]] | None = None,
            ) -> None:
                published.append((topic, key, value))

        async with _provisioned_runner() as (runner, _admin_conn, _schema):
            runner._producer = _CapturingProducer()  # type: ignore[assignment]
            correlation_id = str(uuid4())
            data = _real_delegation_completed_payload(
                correlation_id=correlation_id, tenant_id="beta-business-proof"
            )
            meta = MessageMeta(
                partition=0,
                offset=17773,
                fallback_id=correlation_id,
                topic=runner._topic_delegation_completed,
            )

            ok = await runner.project_event(
                runner._topic_delegation_completed, data, meta
            )
            assert ok is True

            expected = {e.topic for e in runner._aggregate_exposures}
            by_topic = {topic: (key, value) for topic, key, value in published}
            assert expected <= set(by_topic), (
                f"missing aggregate snapshots: {sorted(expected - set(by_topic))}"
            )
            for topic in expected:
                key, value = by_topic[topic]
                # One constant compaction key per topic, forever.
                assert key == topic.encode("utf-8")
                assert value is not None, "an upsert, not a tombstone"
                delta = json.loads(value)
                assert delta["op"] == "upsert"
                assert delta["key"] == [topic]
                assert delta["source_offset"] == 17773
                # The row this apply just created is inside the aggregate.
                summary_topic = "onex.snapshot.projection.delegation.summary.v1"
                if topic == summary_topic:
                    assert delta["row"]["totalDelegations"] >= 1
