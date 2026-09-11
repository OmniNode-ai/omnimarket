# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18139: real-Postgres proof that the delegation writer's aggregate
republish runs under a tenant the database can actually parse.

WHY A REAL DATABASE IS REQUIRED HERE. The defect is not a shape a mock can
observe. ``AsyncpgAdapter`` accepts the statement either way; the refusal
happens inside Postgres, when the ``tenant_isolation`` policy evaluates
``current_setting('app.tenant_id', true)::uuid`` against a GUC holding the
house SLUG ``omninode``. An ``AsyncMock`` has no policy, so every mock-DB test
in this repo passed while the deployed writer dead-lettered 26 delegations.
The shape half of this proof lives in
``tests/test_omn18139_tenant_guc_call_site_ratchet.py``; this module is the
behaviour.

WHY OWNERSHIP IS THE WHOLE FIXTURE, and the mistake this docstring exists to
stop the next reader repeating. ``FORCE ROW LEVEL SECURITY`` makes the table
OWNER subject to its own policies -- but a SUPERUSER bypasses RLS regardless,
and so does the owner of a plain (non-``security_invoker``) VIEW when that
owner is a superuser, because base-table policies for such a view are
evaluated as the view's owner. A first cut of this fixture built the schema as
``postgres`` and handed the runner a NOBYPASSRLS login role. It passed. It
passed because the VIEWS and TABLES were owned by a superuser, so no policy
ever evaluated, and the RED half could not fail. The schema is therefore
created ``AUTHORIZATION`` the writer role and every migration is applied under
``SET ROLE``, so the role owns each relation and each view.
``TestTheFixtureIsFaithful`` asserts that ownership, and asserts that a
slug-valued GUC really is refused on this schema, BEFORE any behaviour is
asserted on it.

NO DRIFT IS APPLIED, and that is itself a finding. Unlike OMN-17228 -- where a
fresh migrated schema carried defaults the deployed lane had lost, so the
fixture had to DROP them -- applying ``0007``..``0034`` in order lands exactly
the onex-dev shape, read back live 2026-09-10 on DEV-SYSTEM
``i-06169517a92b45f86``:

* ``delegation_events.tenant_id`` -- ``uuid NOT NULL DEFAULT
  '820272f9-4aaf-5add-a2df-0af942852ab2'::uuid``
* ``relrowsecurity = t``, ``relforcerowsecurity = t``
* ``tenant_isolation`` USING and WITH CHECK both
  ``(tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)``

so the RED half here is the live defect, not an approximation of it.

WHAT THIS PROVES AND WHAT IT DOES NOT. It proves the writer's statements
survive a real GUC-casting policy under a real NOBYPASSRLS identity. It is not
a staging proof, and it is not a proof on the ``.201`` compose dev lane's own
tables either: that lane has ``relrowsecurity=f`` on ``delegation_events`` and
connects as a superuser, so this defect is structurally unreachable there.
This module provisions its own disposable schema and its own role on a real
Postgres server and mutates nothing any lane reads.

Harness (``_connect_or_skip`` / disposable schema / SKIP rather than ERROR
without a reachable database) follows
``tests/test_omn17228_real_postgres_drifted_default_write_path.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)

#: ``tenant_registry_mirror`` belongs to a different node but is read on every
#: delegation write to resolve the tenant. Created from that node's own DDL so
#: the fixture cannot drift from the relation the writer queries.
_TENANT_REGISTRY_MIRROR_SQL = (
    (
        Path(__file__).resolve().parents[1]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_projection_tenant_registry"
        / "migrations"
        / "0000_create_tenant_registry_mirror.sql"
    )
    .read_text(encoding="utf-8")
    .replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")
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

_TENANT_UUID = "820272f9-4aaf-5add-a2df-0af942852ab2"
_TENANT_SLUG = "omninode"

#: The live failure string, byte-for-byte, from
#: ``onex.dlq.omnimarket.projection-delegation-malformed.v1`` offset 266,
#: 2026-09-10T10:21:18.312Z.
_LIVE_FAILURE = 'invalid input syntax for type uuid: "omninode"'

_ENVELOPE_TIMESTAMP = datetime(2026, 9, 10, 10, 21, 18, 312000, tzinfo=UTC)

#: Relations the writer's aggregate views read. Named so the faithfulness
#: control can assert the policy shape rather than assume it.
_GUC_CASTING_RELATION = "delegation_events"

#: Database-level objects the migration set needs that a NOSUPERUSER role
#: cannot install for itself. Derived from the migrations' own
#: ``CREATE EXTENSION`` statements; ``TestTheFixtureIsFaithful`` asserts this
#: list still covers them, so a migration that adds an extension is a red test
#: here rather than a CI-only failure on a fresh database.
_REQUIRED_EXTENSIONS = ("pgcrypto",)

_EXTENSION_RE = re.compile(
    r"CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?(\w+)\"?", re.I
)


def _test_schema_safe_sql(raw_sql: str) -> str:
    """``CONCURRENTLY`` refuses to run inside the implicit transaction
    asyncpg's multi-statement ``execute()`` opens, and exists only to avoid
    locking a live table -- which a disposable schema does not have."""
    return raw_sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX").replace(
        "CREATE UNIQUE INDEX CONCURRENTLY", "CREATE UNIQUE INDEX"
    )


def _password() -> str:
    return os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )


def _dsn(*, user: str | None = None, password: str | None = None) -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    user = user or os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    password = password if password is not None else _password()
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


def _database_name() -> str:
    return os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")


async def _connect_or_skip() -> asyncpg.Connection:
    if not _password():
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-18139 tenant-GUC "
            "representation gate"
        )
    try:
        return await asyncpg.connect(_dsn(), timeout=15)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-18139 gate: {exc}")


async def _capture(topic: str, value: bytes) -> None:
    """The runner's real ``publish_fn`` seam -- without one, ``get_publish_fn``
    builds a real ``AIOKafkaProducer`` and blocks on connect retries."""
    return


@asynccontextmanager
async def _rls_bound_runner() -> AsyncIterator[
    tuple[DelegationProjectionRunner, asyncpg.Connection, str]
]:
    """The live migrated schema, owned by a NOBYPASSRLS login role.

    Ownership is the point -- see the module docstring. Built under ``SET
    ROLE`` so every table, view and policy belongs to the same non-superuser
    identity the runner then connects as, which is what makes ``FORCE ROW
    LEVEL SECURITY`` actually evaluate.
    """
    admin = await _connect_or_skip()
    suffix = uuid4().hex[:12]
    schema = f"omn18139_{suffix}"
    role = f"omn18139_w_{suffix}"
    password = f"pw_{suffix}"
    database = _database_name()
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{password}'"
        )
        await admin.execute(f"GRANT CONNECT ON DATABASE {database} TO {role}")
        await admin.execute(f"CREATE SCHEMA {schema} AUTHORIZATION {role}")
        await admin.execute(_APP_DASHBOARD_ROLE_SQL)
        # Extensions are database-level and installed by the DBA, never by a
        # projection writer -- so they are created HERE, as the admin, before
        # the SET ROLE below. This is not a convenience: a migration in the set
        # issues ``CREATE EXTENSION IF NOT EXISTS pgcrypto``, which a
        # NOSUPERUSER role cannot execute on a database that does not already
        # have it. On a long-lived database the statement is a silent no-op
        # because the extension is already installed, so this fixture passed
        # against the lab's populated database and failed against CI's freshly
        # created one -- the difference being the database's history, not the
        # code. Creating them here makes the fixture independent of that
        # history, which is what a hermetic fixture has to be.
        for extension in _REQUIRED_EXTENSIONS:
            await admin.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")

        await admin.execute(f"SET ROLE {role}")
        await admin.execute(f"SET search_path TO {schema}, public")
        await admin.execute(_test_schema_safe_sql(_TENANT_REGISTRY_MIRROR_SQL))
        for migration_path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await admin.execute(
                _test_schema_safe_sql(migration_path.read_text(encoding="utf-8"))
            )
        # The writer resolves its tenant through the mirror and refuses an
        # identity nobody recorded (OMN-16804 AC3 / OMN-16831 AC2), so the
        # fixture holds the row the deployed lane holds. Seeding it keeps this
        # module's subject the GUC representation rather than tenant
        # resolution, which has its own tests.
        await admin.execute(
            "INSERT INTO tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, status) VALUES ($1, $2::uuid, 'active') "
            "ON CONFLICT (tenant_slug) DO NOTHING",
            _TENANT_SLUG,
            _TENANT_UUID,
        )
        await admin.execute("RESET ROLE")
        await admin.execute(f"SET search_path TO {schema}, public")
        # OMN-18159: this fixture reads the aggregate views as ``role``, the
        # identity that applied the migrations. On a lane nothing does that --
        # the two principals that read them are app_dashboard and
        # tenant_projection_writer, and migration 0039 grants both. ``role``
        # could read them before 0039 only because it OWNED them, and it stopped
        # owning them when 0039 had to DROP and CREATE each one (0032, 0033 and
        # 0034 each RESET ROLE, so 0039 runs as the superuser). Granting here
        # keeps this module's subject the GUC representation instead of an
        # ownership artifact; the security half of that ownership move is fixed
        # in the migration itself, by 0040's security_invoker.
        await admin.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role}")

        writer_dsn = _dsn(user=role, password=password)
        pool = await asyncpg.create_pool(
            writer_dsn,
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=writer_dsn)
        adapter._pool = pool  # type: ignore[attr-defined]
        runner = DelegationProjectionRunner(publish_fn=_capture)
        runner._db = adapter  # type: ignore[assignment]
        yield runner, admin, schema
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin.execute("RESET ROLE")
            await admin.execute("SET search_path TO public")
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await admin.execute(f"REVOKE ALL ON DATABASE {database} FROM {role}")
            await admin.execute(f"DROP OWNED BY {role}")
            await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()


def _quality_gate_delivery(*, correlation_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "correlation_id": correlation_id,
        "passed": True,
        "fail_category": "pass",
        "quality_score": 1.0,
        "failure_reasons": [],
        "fallback_recommended": False,
        "score_source": "deterministic_acceptance",
        "actual_score": 1.0,
    }
    envelope = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "correlation_id": correlation_id,
        "event_type": "omnibase-infra.quality-gate-result",
        "envelope_timestamp": _ENVELOPE_TIMESTAMP.isoformat(),
        "tenant_id": _TENANT_UUID,
    }
    unwrapped = unwrap_envelope(json.dumps(envelope).encode("utf-8"))
    assert unwrapped is not None
    return unwrapped


@pytest.mark.integration
class TestTheFixtureIsFaithful:
    """Positive control for the whole module. A first cut of this fixture
    passed the behavioural test below while proving nothing, because the
    relations were superuser-owned and no policy ever evaluated."""

    def test_the_writer_role_owns_the_relations_and_rls_is_forced(self) -> None:
        async def _run() -> None:
            async with _rls_bound_runner() as (_runner, admin, schema):
                row = await admin.fetchrow(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                    "pg_get_userbyid(c.relowner) AS owner, "
                    "(SELECT usesuper FROM pg_user "
                    " WHERE usename = pg_get_userbyid(c.relowner)) AS owner_is_super "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = $1 AND c.relname = $2",
                    schema,
                    _GUC_CASTING_RELATION,
                )
                assert row is not None
                assert row["relrowsecurity"] is True
                assert row["relforcerowsecurity"] is True, (
                    "without FORCE the owner bypasses its own policy and the "
                    "RED half below cannot fail"
                )
                assert row["owner_is_super"] is not True, (
                    "the relation is owned by a superuser, so RLS is bypassed "
                    "whatever the policy says -- this fixture would prove "
                    "nothing"
                )

        asyncio.run(_run())

    def test_every_extension_the_migrations_need_is_declared(self) -> None:
        """The fixture installs extensions as the admin because the writer role
        cannot. That list must stay complete, and it is checked against the
        migrations rather than trusted -- a missing entry does not fail on a
        database that already has the extension, only on a fresh one, which is
        precisely how this reached CI once already.
        """
        needed = {
            match.group(1).lower()
            for sql_path in _MIGRATIONS_DIR.glob("*.sql")
            for match in _EXTENSION_RE.finditer(
                sql_path.read_text(encoding="utf-8", errors="replace")
            )
        }
        assert needed, (
            "no CREATE EXTENSION found in the migration set -- the scan has "
            "stopped matching, so the assertion below would be vacuous"
        )
        missing = sorted(needed - {e.lower() for e in _REQUIRED_EXTENSIONS})
        assert missing == [], (
            f"the migrations need extension(s) {missing} that this fixture does "
            "not install as the admin. A NOSUPERUSER role cannot create them, "
            "and the failure is invisible on any database that already has "
            "them. Add them to _REQUIRED_EXTENSIONS."
        )

    def test_the_policy_casts_the_guc_to_uuid(self) -> None:
        """The premise of the whole defect: it is the ``::uuid`` cast that
        turns a slug-valued GUC from a narrowing into an abort."""

        async def _run() -> None:
            async with _rls_bound_runner() as (_runner, admin, schema):
                expr = await admin.fetchval(
                    "SELECT pg_get_expr(p.polqual, p.polrelid) "
                    "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = $1 AND c.relname = $2 "
                    "AND p.polname = 'tenant_isolation'",
                    schema,
                    _GUC_CASTING_RELATION,
                )
                assert expr is not None, "tenant_isolation policy is missing"
                assert "::uuid" in expr, (
                    f"the policy does not cast the GUC ({expr!r}) -- a slug "
                    "would merely narrow the read, not abort it, and this "
                    "module's subject would not exist on this schema"
                )

        asyncio.run(_run())

    def test_every_aggregate_view_reads_as_its_invoker(self) -> None:
        """OMN-18159 / migration 0040. A view reads as its OWNER by default.

        0039 had to DROP and CREATE each of the four aggregate views -- a
        replace cannot change the column list, and every one gained
        ``tenant_id`` -- and a DROP discards the owner. The role that reaches
        0039 is not the role that created them: 0032, 0033 and 0034 each
        ``RESET ROLE``, so everything after runs as the migration runner,
        which here and on a lane is a SUPERUSER, and a superuser is exempt
        from row-level security unconditionally. A read through any of the
        four then returned every tenant's rows whatever ``app.tenant_id``
        held -- the unscoped-serving leak this ticket exists to close,
        reintroduced by the change that closes it.

        0040 sets ``security_invoker`` so the tenant policy on
        ``delegation_events`` evaluates against the CALLER whatever the view's
        owner happens to be. Asserted on the option rather than on the owner
        deliberately: putting the owner back would fix this instance and leave
        the class open for the next migration that has to drop one of these
        views.
        """

        async def _run() -> None:
            async with _rls_bound_runner() as (_runner, admin, schema):
                rows = await admin.fetch(
                    "SELECT c.relname, c.reloptions FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = $1 AND c.relkind = 'v'",
                    schema,
                )
                options = {r["relname"]: (r["reloptions"] or []) for r in rows}
                for view in (
                    "projection_delegation_summary",
                    "projection_delegation_model_routing",
                    "projection_delegation_quality_gate",
                    "projection_delegation_token_usage",
                ):
                    assert view in options, f"{view} was not created on {schema}"
                    assert "security_invoker=true" in options[view], (
                        f"{view} reads as its owner, so a superuser-owned copy "
                        f"would bypass the tenant policy on "
                        f"{_GUC_CASTING_RELATION}; reloptions={options[view]}"
                    )

        asyncio.run(_run())

    def test_a_slug_valued_guc_is_refused_on_this_schema(self) -> None:
        """The RED proof isolated from the writer: bind the GUC to exactly the
        value the adapter's table-less fallback produces, and the read of the
        aggregate view raises the live string. This is what the writer did on
        every delegation."""

        async def _run() -> None:
            async with _rls_bound_runner() as (runner, _admin, _schema):
                pool = runner.db._pool  # type: ignore[attr-defined]
                async with pool.acquire() as conn, conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.tenant_id', $1, true)", _TENANT_SLUG
                    )
                    with pytest.raises(
                        asyncpg.exceptions.InvalidTextRepresentationError
                    ) as exc:
                        await conn.fetch(
                            "SELECT agg.* FROM projection_delegation_summary agg "
                            "LIMIT 1"
                        )
                    assert _LIVE_FAILURE in str(exc.value)

                # The same statement under the UUID form succeeds, which is
                # what makes the failure above about the REPRESENTATION rather
                # than about the read being disallowed outright.
                async with pool.acquire() as conn, conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.tenant_id', $1, true)", _TENANT_UUID
                    )
                    await conn.fetch(
                        "SELECT agg.* FROM projection_delegation_summary agg LIMIT 1"
                    )

        asyncio.run(_run())


@pytest.mark.integration
class TestTheDelegationApplySurvivesTheAggregateRepublish:
    def test_a_quality_gate_verdict_applies_and_republishes(self) -> None:
        """RED before OMN-18139.

        At the parent commit this raised ``asyncpg.exceptions
        .InvalidTextRepresentationError: invalid input syntax for type uuid:
        "omninode"`` out of ``_publish_aggregate_snapshots`` -- AFTER the
        delegation row was already written, which is why the live symptom was a
        dead-letter beside a row that exists. Matches
        ``onex.dlq.omnimarket.projection-delegation-malformed.v1`` offset 266.
        """

        async def _run() -> None:
            async with _rls_bound_runner() as (runner, admin, _schema):
                correlation_id = str(uuid4())

                ok = await runner.project_event(
                    runner._topic_quality_gate_result,
                    _quality_gate_delivery(correlation_id=correlation_id),
                    MessageMeta(partition=0, offset=266, fallback_id=correlation_id),
                )
                assert ok is True

                row = await admin.fetchrow(
                    "SELECT tenant_id, quality_gate_passed FROM delegation_events "
                    "WHERE correlation_id = $1",
                    correlation_id,
                )
                assert row is not None, "the verdict wrote no row"
                assert str(row["tenant_id"]) == _TENANT_UUID
                assert row["quality_gate_passed"] is True

        asyncio.run(_run())

    def test_the_republish_binds_the_tenant_the_write_used(self) -> None:
        """The republish must reuse the write's resolution, not run a second
        one. Asserted on the recorded value rather than on a log line, because
        two resolvers agreeing today is not the property -- there being one
        resolver is."""

        async def _run() -> None:
            async with _rls_bound_runner() as (runner, _admin, _schema):
                correlation_id = str(uuid4())
                assert runner._last_delegation_write_tenant is None

                ok = await runner.project_event(
                    runner._topic_quality_gate_result,
                    _quality_gate_delivery(correlation_id=correlation_id),
                    MessageMeta(partition=0, offset=266, fallback_id=correlation_id),
                )
                assert ok is True
                assert runner._last_delegation_write_tenant == _TENANT_UUID, (
                    "the delegation write did not record the tenant it ran "
                    "under, so the aggregate republish had nothing true to "
                    "bind and would have fallen back to the house slug"
                )

        asyncio.run(_run())
