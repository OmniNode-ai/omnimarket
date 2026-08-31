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
carries its own evidence. Each is paired with the same assertion against the
OPERATIVE conversion, which must not reproduce it.

(c) OMN-17316 -- THE GUARD PASSED AND POSTGRES REFUSED ANYWAY. 0033 in turn
    guarded its role switch with ``pg_has_role(current_user, v_owner, 'USAGE')``
    and then performed the switch with ``set_config('role', v_owner::text,
    true)``, which is ``SET LOCAL ROLE``. Since PostgreSQL 16 ``INHERIT`` and
    ``SET`` are INDEPENDENT membership options, so a membership created
    ``WITH INHERIT TRUE, SET FALSE`` reads ``USAGE = true`` and passes, and the
    migration then aborts two statements later on a bare ``permission denied to
    set role "<owner>"``. That is the exact opaque refusal the guard exists to
    replace, produced from inside the guard's own blind spot. 0034 tests BOTH
    predicates before the switch; 0033's body is carried over verbatim.
    0033 is retired in place -- its bytes are frozen by the OMN-16705
    append-only ratchet, which keys on manifest DECLARATION rather than lane
    application, so an in-place repair was never available (verified by
    falsification against ``check_migration_append_only.py``, 2026-08-31).

Why a real database: (b) is a property of transaction boundaries, (a) is a
property of PL/pgSQL control flow, and (c) is a property of the PostgreSQL 16
catalog. None is visible to a mocked connection -- the standing proof is
OMN-16493, where 0031 shipped two fail-closed guards that were green in review
and RLS-blinded on the lane. SKIPS (never ERRORs) without a reachable Postgres.
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
# 0033 fixed defects (a) and (b) and then carried defect (c) of its own
# (OMN-17316). It is RETIRED, not edited: the assertions below that name it
# hold it to the shape it was superseded FOR, and the role-membership section
# at the bottom holds it to the defect it was superseded BY.
_SUPERSEDED_0033 = "0033_delegation_events_uuid_via_registry_single_transaction.sql"
_CONVERSION = "0034_delegation_events_uuid_via_registry_role_set_guard.sql"
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
    behind. For the operative conversion it is the whole file, which is the point.
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
        if path.name
        not in {_SUPERSEDED_0031, _SUPERSEDED_0032, _SUPERSEDED_0033, _CONVERSION}
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

    async def test_0034_completes_silently_when_the_table_is_absent(self) -> None:
        async with _empty_schema() as (conn, _schema):
            # No raises-guard: an exception here IS the failure.
            await conn.execute(_sql(_CONVERSION))

            assert (
                await conn.fetchval("SELECT to_regclass('delegation_events')") is None
            )

    def test_0034_has_no_statements_after_the_do_block(self) -> None:
        """The structural reason the no-op holds, pinned so it cannot regress.

        A ``RETURN`` is only a no-op if nothing follows the block. This is the
        invariant; the replay above is the behaviour.
        """
        assert _statements_after_the_block_commits(_CONVERSION) == "", (
            "0034 grew a statement after END$$. Anything there runs even when "
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

    async def test_0034_commits_the_policy_with_the_conversion(self) -> None:
        """Same instant, same measurement, on the replacement.

        0034 has nothing after ``END$$``, so the state below is not an
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

    async def test_0034_restates_the_policy_on_the_already_uuid_path(self) -> None:
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

    async def test_0034_grants_app_dashboard_in_the_same_file(self) -> None:
        """OMN-14894 ratchet, as behaviour rather than as a grep."""
        async with _convertible_schema() as (conn, schema):
            await conn.execute(_sql(_CONVERSION))

            granted = await conn.fetchval(
                "SELECT has_table_privilege('app_dashboard', $1, 'SELECT')",
                f"{schema}.delegation_events",
            )
            assert granted is True


# ---------------------------------------------------------------------------
# Defect (c) -- OMN-17316. The guard that passed and let Postgres refuse.
#
# This section needs something the two above do not: a NON-SUPERUSER LOGIN
# identity. `SET ROLE` is authorised against the SESSION user, so a superuser
# session that merely `SET ROLE`s to the migrator can still reach the owner --
# which would make the RED pass vacuously. The migration therefore has to be
# applied over its own connection, authenticated as the migrator.
#
# It also needs PostgreSQL 16+: `GRANT ... WITH INHERIT TRUE, SET FALSE` is a
# syntax error before 16, so an older server cannot express the defect OR the
# repair and must SKIP rather than read as a broken test. Every .201 lane and
# both CI services are 16.x.
# ---------------------------------------------------------------------------

_MIN_SERVER_VERSION_NUM = 160000

# PostgreSQL's own wording for the opaque refusal. This string arriving instead
# of a named OMN- exception IS the defect.
_OPAQUE_SET_ROLE = "permission denied to set role"
# PostgreSQL refusing the switch itself, versus the migration refusing first.
# WHICH of these arrives is the whole finding, so it is asserted on the code
# rather than on the message text -- see _apply_as_migrator.
_SQLSTATE_INSUFFICIENT_PRIVILEGE = "42501"
_SQLSTATE_RAISE_EXCEPTION = "P0001"
# The repaired guard's distinctive phrase. Deliberately not the bare ticket id:
# a path or a docstring could carry that, and a vacuous match is worse than no
# assertion.
_REPAIRED_MARKER = "pg_has_role SET is false"


def _quote_literal(value: str) -> str:
    """Single-quote a value for SQL, doubling any embedded quote."""
    return "'" + value.replace("'", "''") + "'"


def _reject_non_loopback_or_skip(host: str) -> None:
    """OMN-16412. This section creates and drops CLUSTER-WIDE roles.

    A schema is contained; a role is not. A persistent dev-shell env var has
    pointed a sibling suite at the live .201 stability-test Postgres before and
    left 35 throwaway databases behind, so a non-loopback target is refused
    unless opted into explicitly.
    """
    if host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/"):
        return
    if os.environ.get("OMN17316_ALLOW_REMOTE_PG") == "1":
        return
    pytest.skip(
        f"refusing to create cluster roles on non-loopback host {host!r} "
        "(OMN-16412); set OMN17316_ALLOW_REMOTE_PG=1 to opt in"
    )


@asynccontextmanager
async def _split_membership_lane() -> AsyncIterator[
    tuple[asyncpg.Connection, str, str, str]
]:
    """A scratch schema whose delegation_events is owned by a role the migrator is NOT.

    Yields ``(superuser_conn, schema, owner_role, migrator_role)``. ``tenant_id`` is
    created already ``uuid`` on purpose: that is the already-converted branch,
    which reaches the ownership guard with the least fixture between the runner
    and the thing under test. The guard sits BEFORE the ``IF v_convert`` split,
    so this path exercises it identically to the conversion path.
    """
    _reject_non_loopback_or_skip(
        os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    )
    conn = await _connect_or_skip()
    tag = uuid4().hex[:12]
    schema = f"omn17316_{tag}"
    owner = f"omn17316_owner_{tag}"
    migrator = f"omn17316_migrator_{tag}"
    secret = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    try:
        version_num = await conn.fetchval("SHOW server_version_num")
        if int(version_num) < _MIN_SERVER_VERSION_NUM:
            pytest.skip(
                f"server is PostgreSQL {version_num} but the INHERIT/SET "
                "membership split this section proves arrived in 16"
            )
        await conn.execute(_APP_DASHBOARD_ROLE_SQL)
        await conn.execute(f"CREATE ROLE {owner} NOLOGIN NOSUPERUSER NOBYPASSRLS")
        await conn.execute(
            f"CREATE ROLE {migrator} LOGIN NOSUPERUSER NOBYPASSRLS "
            f"PASSWORD {_quote_literal(secret)}"
        )
        await conn.execute(f"CREATE SCHEMA {schema} AUTHORIZATION {owner}")
        await conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO {migrator}")
        await conn.execute(f"SET ROLE {owner}")
        await conn.execute(f"SET search_path TO {schema}")
        await conn.execute(
            "CREATE TABLE delegation_events ("
            "    correlation_id TEXT,"
            "    tenant_id UUID NOT NULL"
            "        DEFAULT '820272f9-4aaf-5add-a2df-0af942852ab2')"
        )
        await conn.execute("ALTER TABLE delegation_events ENABLE ROW LEVEL SECURITY")
        await conn.execute(
            "CREATE POLICY tenant_isolation ON delegation_events FOR ALL "
            "USING (tenant_id = current_setting('app.tenant_id', true)::uuid) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
        )
        await conn.execute("RESET ROLE")
        await conn.execute("SET search_path TO public")
        yield conn, schema, owner, migrator
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("RESET ROLE")
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await conn.execute(f"DROP ROLE IF EXISTS {migrator}")
            await conn.execute(f"DROP ROLE IF EXISTS {owner}")
        await conn.close()


async def _apply_as_migrator(
    migrator: str, schema: str, sql: str
) -> asyncpg.PostgresError | None:
    """Run SQL over a connection authenticated AS the migrator; return the error.

    Returns ``None`` when the statement succeeds. The EXCEPTION is returned
    rather than its text because ``sqlstate`` is what separates the two
    outcomes this section measures, and no substring can: 0034's own message
    QUOTES the phrase ``permission denied to set role`` while explaining what
    it refuses to walk into, so a text search finds it on BOTH paths. ``42501``
    is PostgreSQL refusing the switch; ``P0001`` is the migration's own
    ``RAISE EXCEPTION`` refusing first.

    A fresh connection is
    mandatory: ``SET ROLE`` is authorised against the SESSION user, so reusing
    the superuser connection would let the role switch succeed no matter what
    the membership says -- and the RED would pass vacuously.

    ``search_path`` is set explicitly because this connection is new: without
    it ``to_regclass('delegation_events')`` misses the scratch table, the
    migration takes its table-absent ``RETURN``, and every assertion below
    would be measuring a silent no-op.
    """
    secret = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(migrator)}:{quote_plus(secret)}@{host}:{port}/{db}"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"SET search_path TO {schema}")
        assert (
            await conn.fetchval("SELECT to_regclass('delegation_events')") is not None
        )
        await conn.execute(sql)
    except asyncpg.PostgresError as exc:
        return exc
    finally:
        await conn.close()
    return None


class TestTheRoleSwitchGuardTestsWhatItExercises:
    """Defect (c). OMN-17316."""

    async def test_usage_and_set_are_independent_predicates(self) -> None:
        """The platform fact the whole finding rests on -- measured, not cited.

        ``MEMBER`` is asserted too because it is the obvious "just use another
        privilege string" fix, and it does not work: it is true under
        ``SET FALSE`` as well, so it is no more a proxy for ``SET ROLE`` than
        ``USAGE`` is. If a future PostgreSQL collapses these back into one
        predicate, this is where that is discovered.
        """
        async with _split_membership_lane() as (conn, _schema, owner, migrator):
            await conn.execute(
                f"GRANT {owner} TO {migrator} WITH INHERIT TRUE, SET FALSE"
            )

            row = await conn.fetchrow(
                "SELECT pg_has_role($1, $2, 'USAGE') AS usage, "
                "pg_has_role($1, $2, 'SET') AS can_set, "
                "pg_has_role($1, $2, 'MEMBER') AS member",
                migrator,
                owner,
            )

            assert (row["usage"], row["can_set"], row["member"]) == (True, False, True)

    async def test_red_0033_passes_its_own_guard_then_aborts_opaquely(self) -> None:
        """RED. 0033 admits an identity that cannot do what comes next.

        Two things are asserted and the second is the point: the migration
        fails, and it fails with PostgreSQL's bare ``permission denied to set
        role`` rather than with 0033's own named refusal. A guard whose failure
        message never appears is not a guard.

        Kept as an executable record of what the supersession is FOR. If this
        ever stops raising, 0033's bytes moved -- and they are frozen.
        """
        async with _split_membership_lane() as (conn, schema, owner, migrator):
            await conn.execute(
                f"GRANT {owner} TO {migrator} WITH INHERIT TRUE, SET FALSE"
            )

            error = await _apply_as_migrator(migrator, schema, _sql(_SUPERSEDED_0033))

            assert error is not None, (
                "0033 unexpectedly completed under a SET FALSE membership -- it "
                "cannot have performed the role switch"
            )
            assert error.sqlstate == _SQLSTATE_INSUFFICIENT_PRIVILEGE, (
                "the abort must be PostgreSQL's own refusal at set_config, not "
                f"a RAISE from the guard; got {error.sqlstate} / {error}"
            )
            assert _OPAQUE_SET_ROLE in str(error), (
                f"expected the opaque PostgreSQL refusal OMN-17316 reports; got {error}"
            )
            assert _REPAIRED_MARKER not in str(error), (
                "0033 must not already carry the repaired guard -- if it does, "
                "this RED is measuring the wrong file"
            )

    async def test_0034_refuses_early_and_names_the_failing_predicate(self) -> None:
        """GREEN. The same membership now produces the named refusal instead.

        The message has to be actionable on its own -- an operator reading it
        in a deploy log has no access to this test -- so it is asserted to name
        the ticket, the predicate that failed, the membership option at fault,
        and the remediation.
        """
        async with _split_membership_lane() as (conn, schema, owner, migrator):
            await conn.execute(
                f"GRANT {owner} TO {migrator} WITH INHERIT TRUE, SET FALSE"
            )

            error = await _apply_as_migrator(migrator, schema, _sql(_CONVERSION))

            assert error is not None, "0034 must still refuse a SET FALSE membership"
            # THE ordering assertion. P0001 is the migration's own RAISE; 42501
            # would mean it reached set_config first and the guard guarded
            # nothing. Text cannot decide this -- 0034's message quotes the
            # 42501 wording verbatim while explaining what it avoids.
            assert error.sqlstate == _SQLSTATE_RAISE_EXCEPTION, (
                "0034 reached set_config before refusing -- the guard must come "
                f"FIRST; got {error.sqlstate} / {error}"
            )
            assert "OMN-17316: the migrate identity" in str(error)
            assert _REPAIRED_MARKER in str(error)
            assert "SET FALSE" in str(error)
            assert "WITH SET TRUE" in str(error)

    async def test_0034_still_refuses_a_non_member_on_the_usage_arm(self) -> None:
        """The USAGE half is not lost to the repair.

        Adding the SET predicate would be a regression if it replaced the USAGE
        one: USAGE carries the RLS-blindness rationale (OMN-16493), SET carries
        the role switch. With no membership at all, the USAGE arm must fire.
        """
        async with _split_membership_lane() as (_conn, schema, _owner, migrator):
            error = await _apply_as_migrator(migrator, schema, _sql(_CONVERSION))

            assert error is not None, "a non-member must be refused"
            assert error.sqlstate == _SQLSTATE_RAISE_EXCEPTION
            assert "OMN-16930" in str(error)
            assert "USAGE is" in str(error)

    async def test_0034_completes_under_a_default_membership(self) -> None:
        """The guard must not over-refuse.

        The PostgreSQL 16 default ``GRANT`` confers BOTH options, which is the
        path every real lane is on today -- exactly why the defect was latent
        and CI stayed green.
        """
        async with _split_membership_lane() as (conn, schema, owner, migrator):
            await conn.execute(f"GRANT {owner} TO {migrator}")

            error = await _apply_as_migrator(migrator, schema, _sql(_CONVERSION))

            assert error is None, (
                f"0034 refused a default (INHERIT+SET) membership: {error}"
            )

    def test_0033_is_left_untouched_and_still_carries_the_defect(self) -> None:
        """0034 SUPERSEDES 0033; it does not edit it.

        ``check_migration_append_only.py`` freezes 0033's bytes -- it is
        declared in the infra ledger's ``application-migrations.tsv`` -- and a
        supersession row is the only escape it accepts. If someone "helpfully"
        repairs 0033 in place later, every lane that recorded its checksum
        breaks, so the defect staying put is the correct end state and is
        asserted rather than left to trust.
        """
        body = _sql(_SUPERSEDED_0033)
        executable = body[body.index("DO $$") :]

        assert "pg_has_role(current_user, v_owner, 'SET')" not in executable, (
            "0033 was edited in place. Its bytes are frozen: the repair belongs "
            "in 0034 (OMN-16705 append-only ratchet, OMN-17316)."
        )

    def test_0034_checks_both_predicates_before_the_role_switch(self) -> None:
        """Ordering is the whole finding: a guard after the switch guards nothing."""
        body = _sql(_CONVERSION)
        executable = body[body.index("DO $$") :]

        usage = executable.index("pg_has_role(current_user, v_owner, 'USAGE')")
        can_set = executable.index("pg_has_role(current_user, v_owner, 'SET')")
        switch = executable.index("set_config('role', v_owner::text, true)")

        assert usage < switch
        assert can_set < switch
