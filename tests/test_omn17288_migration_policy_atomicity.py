# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17288: the two defects that made 0032 need superseding, proven and fixed.

Both come from the same shape -- three statements after ``END$$``::

    DROP POLICY IF EXISTS tenant_isolation ON delegation_events;
    CREATE POLICY tenant_isolation ON delegation_events ...;
    GRANT SELECT ON delegation_events TO app_dashboard;

(a) THE DOCUMENTED NO-OP PATH ABORTED. ``RETURN`` leaves the DO block, not the
    file, so on a lane without ``delegation_events`` those three statements ran
    anyway and raised ``relation "delegation_events" does not exist``. The one
    path the file described as "nothing to convert" was the one path that could
    not complete.

(b) RLS ENABLED, ZERO POLICIES, BETWEEN TRANSACTIONS. The runner is
    ``psql -v ON_ERROR_STOP=1 -f <file>`` with no ``--single-transaction``, so
    ``END$$`` COMMITS. Between that commit and the standalone ``CREATE POLICY``
    the table is committed with RLS on and no policy -- every application read
    denied. An interruption there (^C, eviction, reset, OOM) makes it permanent
    until someone re-runs.

These are asserted against 0032's real bytes, not described, so the supersession
carries its own evidence. Each is paired with the same assertion against 0033,
which must not reproduce it.

Why a real database: (b) is a property of transaction boundaries and (a) is a
property of PL/pgSQL control flow. Neither is visible to a mocked connection --
the standing proof is OMN-16493, where 0031 shipped two fail-closed guards that
were green in review and RLS-blinded on the lane. SKIPS (never ERRORs) without a
reachable Postgres.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

pytestmark = pytest.mark.integration

_DELEGATION_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)
_REGISTRY_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_tenant_registry"
    / "migrations"
)

_SUPERSEDED_0031 = "0031_delegation_events_tenant_id_to_uuid.sql"
_SUPERSEDED_0032 = "0032_delegation_events_tenant_id_uuid_via_registry.sql"
_CONVERSION = "0033_delegation_events_uuid_via_registry_single_transaction.sql"
_MIRROR = "0000_create_tenant_registry_mirror.sql"

# Synthetic throughout -- see OMN-17288 finding 2. uuid5 over an RFC 2606
# reserved domain, so the pair is provably not a real tenant's.
_SLUG = "t-external-fixture-omn17288"
_UUID = "7527359e-3c87-53fd-a0ae-09fb9c2fe82d"

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


def _base_dsn() -> str:
    secret = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip("POSTGRES_PASSWORD not set -- skipping OMN-17288 replay")
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-17288 replay: {exc}")


def _sql(name: str) -> str:
    """A migration's real bytes, with the one scratch-schema accommodation the
    0031 and 0032 proofs already use."""
    directory = _REGISTRY_MIGRATIONS if name == _MIRROR else _DELEGATION_MIGRATIONS
    raw = (directory / name).read_text(encoding="utf-8")
    return raw.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")


def _statements_before_the_block_commits(name: str) -> str:
    """Everything up to and including ``END$$;``.

    This is precisely what a ``psql -f`` run has committed at the moment the DO
    block ends -- the state an interruption immediately after it would leave
    behind. For 0033 it is the whole file, which is the point.
    """
    body = _sql(name)
    marker = "END$$;"
    index = body.index(marker)
    return body[: index + len(marker)]


def _statements_after_the_block_commits(name: str) -> str:
    body = _sql(name)
    marker = "END$$;"
    return body[body.index(marker) + len(marker) :].strip()


def _pre_conversion_migrations() -> list[Path]:
    return [
        path
        for path in sorted(_DELEGATION_MIGRATIONS.glob("*.sql"), key=lambda f: f.name)
        if path.name not in {_SUPERSEDED_0031, _SUPERSEDED_0032, _CONVERSION}
    ]


@asynccontextmanager
async def _empty_schema() -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    """A schema where ``delegation_events`` genuinely does not exist.

    ``public`` is deliberately OUT of search_path: leaving it in would let an
    unrelated table satisfy ``to_regclass`` and quietly vacate the test.
    """
    conn = await _connect_or_skip()
    schema = f"omn17288_{uuid4().hex[:16]}"
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}")
        await conn.execute(_APP_DASHBOARD_ROLE_SQL)
        assert await conn.fetchval("SELECT to_regclass('delegation_events')") is None
        yield conn, schema
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@asynccontextmanager
async def _convertible_schema() -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    """delegation_events under the pre-0031 TEXT shape, with a mirror that can
    resolve every row -- i.e. the state in which the conversion SUCCEEDS."""
    conn = await _connect_or_skip()
    schema = f"omn17288_{uuid4().hex[:16]}"
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute(_APP_DASHBOARD_ROLE_SQL)
        for path in _pre_conversion_migrations():
            await conn.execute(_sql(path.name))
        await conn.execute(_sql(_MIRROR))
        await conn.execute(
            "INSERT INTO tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, status) VALUES ($1, $2, 'active')",
            _SLUG,
            _UUID,
        )
        await conn.execute(
            "INSERT INTO delegation_events "
            "(correlation_id, tenant_id, task_type, delegated_to, timestamp) "
            "VALUES ($1, $2, 'code-review', 'glm-5.2', now())",
            f"omn17288-{uuid4().hex[:8]}",
            _SLUG,
        )
        yield conn, schema
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


async def _policy_names(conn: asyncpg.Connection, schema: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT policyname FROM pg_policies "
        "WHERE schemaname = $1 AND tablename = 'delegation_events' "
        "ORDER BY policyname",
        schema,
    )
    return [row["policyname"] for row in rows]


async def _rls_flags(conn: asyncpg.Connection) -> asyncpg.Record:
    return await conn.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE oid = 'delegation_events'::regclass"
    )


class TestTheNoOpPathIsActuallyANoOp:
    """Defect (a). AC1."""

    async def test_red_0032_aborts_on_the_path_it_documents_as_a_no_op(self) -> None:
        """0032 announces "nothing to convert" and then fails to convert nothing.

        Kept as an executable record of what the supersession is FOR. If this
        ever stops raising, 0032's bytes moved -- and they are not allowed to.
        """
        async with _empty_schema() as (conn, _schema):
            with pytest.raises(asyncpg.PostgresError) as excinfo:
                await conn.execute(_sql(_SUPERSEDED_0032))

            assert 'relation "delegation_events" does not exist' in str(excinfo.value)

    async def test_0033_completes_silently_when_the_table_is_absent(self) -> None:
        async with _empty_schema() as (conn, _schema):
            # No raises-guard: an exception here IS the failure.
            await conn.execute(_sql(_CONVERSION))

            assert (
                await conn.fetchval("SELECT to_regclass('delegation_events')") is None
            )

    def test_0033_has_no_statements_after_the_do_block(self) -> None:
        """The structural reason the no-op holds, pinned so it cannot regress.

        A ``RETURN`` is only a no-op if nothing follows the block. This is the
        invariant; the replay above is the behaviour.
        """
        assert _statements_after_the_block_commits(_CONVERSION) == "", (
            "0033 grew a statement after END$$. Anything there runs even when "
            "the DO block RETURNed early, which is exactly the 0032 defect "
            "(OMN-17288): CREATE POLICY and GRANT then fire against a relation "
            "the block just established does not exist."
        )

    def test_0032_still_has_the_statements_that_caused_it(self) -> None:
        """Anchors the comparison. 0032 is retired, not edited."""
        trailing = _statements_after_the_block_commits(_SUPERSEDED_0032)
        assert "CREATE POLICY tenant_isolation" in trailing
        assert "GRANT SELECT ON delegation_events TO app_dashboard" in trailing


class TestThePolicyIsNeverCommittedAway:
    """Defect (b). AC2."""

    async def test_red_0032_commits_rls_on_with_no_policy(self) -> None:
        """The window, measured rather than argued.

        Executing 0032 up to and including ``END$$;`` is exactly what the
        runner has committed at that instant. The table is left RLS-enabled
        with zero policies -- which denies every application read.
        """
        async with _convertible_schema() as (conn, schema):
            assert await _policy_names(conn, schema) == ["tenant_isolation"]

            await conn.execute(_statements_before_the_block_commits(_SUPERSEDED_0032))

            flags = await _rls_flags(conn)
            assert flags["relrowsecurity"] is True
            assert await _policy_names(conn, schema) == [], (
                "0032's DO block was expected to commit with the policy "
                "dropped -- that gap is the defect this supersession closes"
            )

    async def test_0033_commits_the_policy_with_the_conversion(self) -> None:
        """Same instant, same measurement, on the replacement.

        0033 has nothing after ``END$$``, so the state below is not an
        intermediate one -- it is the final state, and it already has the
        policy.
        """
        async with _convertible_schema() as (conn, schema):
            await conn.execute(_statements_before_the_block_commits(_CONVERSION))

            flags = await _rls_flags(conn)
            assert flags["relrowsecurity"] is True
            assert flags["relforcerowsecurity"] is True
            assert await _policy_names(conn, schema) == ["tenant_isolation"]

            column_type = await conn.fetchval(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = 'delegation_events' "
                "AND column_name = 'tenant_id'",
                schema,
            )
            assert column_type == "uuid"

    async def test_0033_restates_the_policy_on_the_already_uuid_path(self) -> None:
        """The fall-through, proven directly.

        0032 ``RETURN``ed here and relied on the statements after ``END$$`` to
        restate the policy. Those are gone, so the already-converted branch has
        to reach the restatement itself. Dropping the policy and re-running is
        the only way to observe that it does.
        """
        async with _convertible_schema() as (conn, schema):
            await conn.execute(_sql(_CONVERSION))
            assert await _policy_names(conn, schema) == ["tenant_isolation"]

            await conn.execute("DROP POLICY tenant_isolation ON delegation_events")
            assert await _policy_names(conn, schema) == []

            # Second run takes the `v_current_type = 'uuid'` branch.
            await conn.execute(_sql(_CONVERSION))

            assert await _policy_names(conn, schema) == ["tenant_isolation"], (
                "the already-uuid branch must fall through to the policy "
                "restatement, not RETURN past it (OMN-17288)"
            )

    async def test_0033_grants_app_dashboard_in_the_same_file(self) -> None:
        """OMN-14894 ratchet, as behaviour rather than as a grep."""
        async with _convertible_schema() as (conn, schema):
            await conn.execute(_sql(_CONVERSION))

            granted = await conn.fetchval(
                "SELECT has_table_privilege('app_dashboard', $1, 'SELECT')",
                f"{schema}.delegation_events",
            )
            assert granted is True
