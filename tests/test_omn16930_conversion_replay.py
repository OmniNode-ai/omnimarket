# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16930 AC4: scratch-Postgres replay of the live delegation_events census
against the registry-resolving conversion.

WHY A REPLAY AND NOT A UNIT TEST
    The thing under test is a ``DO $$ ... $$`` block whose entire behaviour is
    RLS interaction, dynamic DDL, and a fail-closed pre-guard. Every one of
    those is invisible to a mocked database. OMN-16493 is the standing proof:
    0031 shipped with two independent fail-closed guards and a bespoke RAISE
    written for exactly this failure, all of them green in review -- and on the
    live lane the DELETE matched zero rows, the pre-guard never fired, and the
    only surviving symptom was ``contains null values``, which sent a week of
    diagnosis at a NULL-data problem that did not exist. The guards were
    RLS-blinded. Nothing short of a real database with real RLS can falsify
    that class.

THE CENSUS
    Seeded from the live enumeration of ``omnidash_analytics.delegation_events``
    on the staging RDS (2026-08-29, read-only, as the RDS master -- the only
    identity that can see through FORCE RLS to enumerate it at all):

        beta-business-proof              117
        omninode                          43
        11111111-1111-1111-1111-...        3   SEED-A-1/2/3
        22222222-2222-2222-2222-...        3   SEED-B-1/2/3
        d5-e2e-0b5ae67c                    1
        delegation-spotcheck-1786977419    1
        t-external-fixture-omn17288        1   externally-owned tenant
        -----------------------------------------
                                         169

    The last slug is SYNTHETIC. The live census row was an external customer's
    real slug and registry UUID, and this file is in a PUBLIC repo, so
    OMN-17288 substituted a stand-in that is provably nobody's:
    uuid5(NAMESPACE_DNS, "t-external-fixture-omn17288.example.invalid"), over
    an RFC 2606 reserved domain. Only the identifier changed -- the row count,
    the arithmetic below, and the fact that this slug is outside 0031's CASE
    are all unchanged, and none of them ever depended on whose tenant it was.

    Six of the nine "unmapped" rows are the SEED-A/SEED-B fixtures, which have
    no registry identity at any point in the future -- neither literal appears
    in ``omninode_cloud.public.tenants``. Their disposition is the recorded
    operator ruling (option (c), delete by exact correlation_id). The other
    three DO have registry UUIDs and must convert, which is exactly what 0031's
    three-entry CASE could not do.

    So the arithmetic this file pins is: 169 in, 6 deleted as debris, 163
    converted, **0 lost silently**. A conversion that quietly dropped rows
    would satisfy a naive "no NULLs afterwards" assertion; row conservation is
    asserted explicitly.

RED / GREEN
    RED   -- empty mirror: the conversion aborts, names every unresolved slug,
             and says the PROJECTION has not caught up (not "unknown tenant").
    GREEN -- mirror populated from the registry: all 163 survivors convert, and
             a UUID-keyed RLS read finds them.

Harness (``_connect_or_skip`` / disposable schema / guarded roles / lexical
migration glob) is the one this repo already uses for the 0031 proof in
``tests/test_omn15683_tenant_uuid_migration_real_postgres.py``. SKIPS (never
ERRORs) without a reachable database.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, quote_plus
from uuid import UUID, uuid4

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

# 0031 and 0032 are both RETIRED and fenced. 0033 is the operative conversion:
# OMN-17288 superseded 0032 because its policy recreate and GRANT sat AFTER
# `END$$`, which broke the documented table-absent no-op and left a real
# RLS-enabled-with-no-policy window between transactions. The mechanism under
# test here is unchanged, so this replay simply follows the live file.
_SUPERSEDED_0031 = "0031_delegation_events_tenant_id_to_uuid.sql"
_SUPERSEDED_0032 = "0032_delegation_events_tenant_id_uuid_via_registry.sql"
_CONVERSION = "0033_delegation_events_uuid_via_registry_single_transaction.sql"
_MIRROR = "0000_create_tenant_registry_mirror.sql"

# The registry rows, pinned as literals from the live cross-check against
# omninode_cloud.public.tenants (OMN-16493 comment 4d7a41a1, finding 4) --
# except t-external-fixture-omn17288, which is the OMN-17288 synthetic
# stand-in for the one externally-owned customer in that census (see the
# module docstring).
# Deliberately NOT imported from omnimarket.projection.tenant_isolation: this
# file is the independent check on that map. Note that three of these six are
# absent from _LEGACY_TENANT_UUID_MAP and from 0031's CASE -- they are the
# reason 0031 cannot apply.
_REGISTRY: dict[str, UUID] = {
    "omninode": UUID("820272f9-4aaf-5add-a2df-0af942852ab2"),
    "beta-business-proof": UUID("91c74442-1233-4c97-b191-911a10346fdf"),
    "beta-gateway-canary-79afa7263852": UUID("79afa726-3852-464f-b7a4-d4b8b9c75ee7"),
    "d5-e2e-0b5ae67c": UUID("18ae3951-376b-4400-af83-c34a137bfda1"),
    "delegation-spotcheck-1786977419": UUID("7e7e6e72-1a02-4a66-a98a-dae317e72e00"),
    "t-external-fixture-omn17288": UUID("7527359e-3c87-53fd-a0ae-09fb9c2fe82d"),
}

# Slugs that 0031's three-entry CASE knew nothing about but which resolve
# cleanly through the mirror. The whole point of the mechanism.
_BEYOND_0031 = {
    "d5-e2e-0b5ae67c",
    "delegation-spotcheck-1786977419",
    "t-external-fixture-omn17288",
}

_SEED_DEBRIS_CORRELATION_IDS = [
    "SEED-A-1",
    "SEED-A-2",
    "SEED-A-3",
    "SEED-B-1",
    "SEED-B-2",
    "SEED-B-3",
]

# (tenant_id value as stored, row count) -- the live census, verbatim.
_CENSUS: list[tuple[str, int]] = [
    ("beta-business-proof", 117),
    ("omninode", 43),
    ("11111111-1111-1111-1111-111111111111", 3),
    ("22222222-2222-2222-2222-222222222222", 3),
    ("d5-e2e-0b5ae67c", 1),
    ("delegation-spotcheck-1786977419", 1),
    ("t-external-fixture-omn17288", 1),
]
_CENSUS_TOTAL = sum(count for _, count in _CENSUS)
_DEBRIS_ROWS = len(_SEED_DEBRIS_CORRELATION_IDS)
_EXPECTED_SURVIVORS = _CENSUS_TOTAL - _DEBRIS_ROWS

_READER_ROLE = "omn16930_rls_reader"
_READER_PASSWORD = "omn16930-test-only-throwaway"

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

_READER_ROLE_SQL = f"""
DO $$
BEGIN
  BEGIN
    CREATE ROLE {_READER_ROLE} WITH
      LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
      PASSWORD '{_READER_PASSWORD}';
  EXCEPTION
    WHEN duplicate_object THEN
      NULL;
  END;
END;
$$;
ALTER ROLE {_READER_ROLE}
  LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD '{_READER_PASSWORD}';
"""


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


def _reader_dsn(schema: str) -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    options = quote(f"-c search_path={schema},public")
    return (
        f"postgresql://{_READER_ROLE}:{_READER_PASSWORD}@{host}:{port}/{db}"
        f"?options={options}"
    )


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("POSTGRES_PASSWORD not set -- skipping OMN-16930 conversion replay")
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-16930 conversion replay: {exc}")


def _test_schema_safe_sql(raw_sql: str) -> str:
    """CONCURRENTLY refuses to run inside asyncpg's implicit multi-statement
    transaction. Same helper the 0031 proof uses."""
    return raw_sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")


def _pre_conversion_migrations() -> list[Path]:
    """Every delegation migration up to and including 0030.

    0031 and 0032 are excluded deliberately: both are FENCED and superseded,
    and this file proves the replacement. Applying either here would convert
    the column before 0033 ever ran, making the whole replay vacuous.
    """
    return [
        path
        for path in sorted(_DELEGATION_MIGRATIONS.glob("*.sql"), key=lambda f: f.name)
        if path.name not in {_SUPERSEDED_0031, _SUPERSEDED_0032, _CONVERSION}
    ]


@asynccontextmanager
async def _census_schema(
    *, create_mirror: bool
) -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    """A disposable schema holding the live 169-row census under the pre-0031
    (TEXT) shape, with the mirror relation optionally created but never
    populated."""
    admin_conn = await _connect_or_skip()
    schema = f"omn16930_{uuid4().hex[:16]}"
    db_name = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_APP_DASHBOARD_ROLE_SQL)
        await admin_conn.execute(_READER_ROLE_SQL)
        await admin_conn.execute(
            f'GRANT CONNECT ON DATABASE "{db_name}" TO {_READER_ROLE}'
        )

        for migration_path in _pre_conversion_migrations():
            await admin_conn.execute(
                _test_schema_safe_sql(migration_path.read_text(encoding="utf-8"))
            )
        if create_mirror:
            await admin_conn.execute(
                _test_schema_safe_sql(
                    (_REGISTRY_MIGRATIONS / _MIRROR).read_text(encoding="utf-8")
                )
            )

        await _seed_census(admin_conn)

        await admin_conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO {_READER_ROLE}")
        await admin_conn.execute(
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {_READER_ROLE}"
        )
        yield admin_conn, schema
    finally:
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


async def _seed_census(conn: asyncpg.Connection) -> None:
    """Insert the live census. The six debris rows carry their real
    correlation_ids, because the conversion disposes of them BY that value."""
    for tenant_value, count in _CENSUS:
        for index in range(count):
            if tenant_value.startswith("11111111"):
                correlation_id = f"SEED-A-{index + 1}"
            elif tenant_value.startswith("22222222"):
                correlation_id = f"SEED-B-{index + 1}"
            else:
                correlation_id = f"{tenant_value}-{index}-{uuid4().hex[:8]}"
            await conn.execute(
                "INSERT INTO delegation_events "
                "(correlation_id, tenant_id, task_type, delegated_to, timestamp) "
                "VALUES ($1, $2, $3, $4, now())",
                correlation_id,
                tenant_value,
                "code-review",
                "glm-5.2",
            )


async def _populate_mirror(conn: asyncpg.Connection) -> None:
    """What the projection does at runtime, expressed as its net effect.

    The writer itself is unit-tested in
    ``test_omn16930_tenant_registry_projection.py``; what this replay needs is
    the STATE that writer produces, so the conversion has something real to
    resolve against.
    """
    for slug, tenant_uuid in _REGISTRY.items():
        await conn.execute(
            "INSERT INTO tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, display_name, status, "
            " registry_created_at, source_event_id) "
            "VALUES ($1, $2, $3, 'active', now(), 'omn16930-replay') "
            "ON CONFLICT (tenant_slug) DO NOTHING",
            slug,
            str(tenant_uuid),
            slug,
        )


async def _apply_conversion(conn: asyncpg.Connection) -> None:
    await conn.execute(
        _test_schema_safe_sql(
            (_DELEGATION_MIGRATIONS / _CONVERSION).read_text(encoding="utf-8")
        )
    )


async def _column_type(conn: asyncpg.Connection, schema: str) -> str:
    return await conn.fetchval(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = 'delegation_events' "
        "AND column_name = 'tenant_id'",
        schema,
    )


class TestConversionReplay:
    # -- RED ---------------------------------------------------------------

    async def test_red_unresolved_slugs_abort_naming_the_projection(self) -> None:
        """AC4 RED. An empty mirror aborts, and the message is actionable.

        This is the state every lane is in before the projection is deployed
        and caught up, which is exactly why the migration is fenced. The three
        assertions on the message text are the AC5 contract: 0031's failure was
        undiagnosable for a week because it named the symptom
        (``contains null values``) instead of the cause.
        """
        async with _census_schema(create_mirror=True) as (conn, schema):
            assert await _column_type(conn, schema) == "text"

            with pytest.raises(asyncpg.PostgresError) as excinfo:
                await _apply_conversion(conn)

            message = str(excinfo.value)
            assert "HAS NOT CAUGHT UP" in message
            assert "node_projection_tenant_registry" in message
            assert "OMN-16930" in message
            # Every surviving distinct slug is named -- an abort that named
            # only the first would send the operator round the loop once per
            # slug, which is 0031's ``LIMIT 1`` pre-guard shape.
            for slug in ("beta-business-proof", "omninode", *_BEYOND_0031):
                assert f"'{slug}'" in message, f"{slug} missing from abort message"

    async def test_red_the_abort_rolls_back_completely(self) -> None:
        """Fail-closed means fail-WHOLLY.

        The conversion defeats FORCE RLS on delegation_events to see its own
        rows. If an abort left that toggle off, a fail-closed migration would
        have silently disabled tenant isolation on a live table -- strictly
        worse than the defect it refused to introduce. The single-DO-block
        shape exists for this; it is asserted, not assumed.
        """
        async with _census_schema(create_mirror=True) as (conn, schema):
            before = await conn.fetchrow(
                "SELECT relforcerowsecurity, relrowsecurity FROM pg_class "
                "WHERE oid = 'delegation_events'::regclass"
            )
            assert before["relforcerowsecurity"] is True

            with pytest.raises(asyncpg.PostgresError):
                await _apply_conversion(conn)

            after = await conn.fetchrow(
                "SELECT relforcerowsecurity, relrowsecurity FROM pg_class "
                "WHERE oid = 'delegation_events'::regclass"
            )
            assert after["relforcerowsecurity"] is True
            assert after["relrowsecurity"] is True
            assert await _column_type(conn, schema) == "text"
            assert (
                await conn.fetchval("SELECT count(*) FROM delegation_events")
                == _CENSUS_TOTAL
            ), "an aborted conversion must not have deleted the debris rows either"

    async def test_red_a_partially_caught_up_mirror_still_aborts(self) -> None:
        """The dangerous middle state: the projection is running but behind.

        A mirror holding the three slugs 0031 already knew, and none of the
        three it did not, is the shape a naive "is the mirror populated?" check
        would pass. It must still abort, and it must name only what is actually
        unresolved.
        """
        async with _census_schema(create_mirror=True) as (conn, _schema):
            for slug in ("omninode", "beta-business-proof"):
                await conn.execute(
                    "INSERT INTO tenant_registry_mirror "
                    "(tenant_slug, tenant_uuid, status) VALUES ($1, $2, 'active')",
                    slug,
                    str(_REGISTRY[slug]),
                )

            with pytest.raises(asyncpg.PostgresError) as excinfo:
                await _apply_conversion(conn)

            message = str(excinfo.value)
            for slug in _BEYOND_0031:
                assert f"'{slug}'" in message
            assert "'omninode'" not in message
            assert "'beta-business-proof'" not in message

    # -- GREEN -------------------------------------------------------------

    async def test_green_populated_mirror_converts_every_row(self) -> None:
        """AC4 GREEN. 169 in, 6 debris deleted, 163 converted, 0 lost.

        Row conservation is asserted explicitly. A conversion that silently
        dropped the rows it could not resolve would pass a bare "no NULLs
        remain" check while destroying a customer's history.
        """
        async with _census_schema(create_mirror=True) as (conn, schema):
            await _populate_mirror(conn)
            assert (
                await conn.fetchval("SELECT count(*) FROM delegation_events")
                == _CENSUS_TOTAL
            )

            await _apply_conversion(conn)

            assert await _column_type(conn, schema) == "uuid"
            assert (
                await conn.fetchval("SELECT count(*) FROM delegation_events")
                == _EXPECTED_SURVIVORS
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM delegation_events WHERE tenant_id IS NULL"
                )
                == 0
            )

            # Every surviving row landed on the registry's UUID for its slug --
            # including the three 0031's CASE could not express.
            for slug, expected_uuid in _REGISTRY.items():
                expected_rows = next(
                    (count for value, count in _CENSUS if value == slug), 0
                )
                actual = await conn.fetchval(
                    "SELECT count(*) FROM delegation_events WHERE tenant_id = $1",
                    expected_uuid,
                )
                assert actual == expected_rows, (
                    f"{slug} -> {expected_uuid}: expected {expected_rows} rows, "
                    f"got {actual}"
                )

            # The debris is gone by exact correlation_id, and only the debris.
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM delegation_events "
                    "WHERE correlation_id = ANY($1::text[])",
                    _SEED_DEBRIS_CORRELATION_IDS,
                )
                == 0
            )

    async def test_green_the_uuid_keyed_rls_read_now_finds_the_rows(self) -> None:
        """The defect OMN-15683 filed, closed end to end.

        Read as a genuinely non-superuser NOBYPASSRLS role, because a superuser
        bypasses RLS unconditionally and would return the true count no matter
        what the policy says -- making the whole assertion vacuous.
        """
        async with _census_schema(create_mirror=True) as (conn, schema):
            await _populate_mirror(conn)
            await _apply_conversion(conn)
            await conn.execute(
                f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {_READER_ROLE}"
            )

            reader = await asyncpg.connect(_reader_dsn(schema))
            try:
                await reader.execute(
                    "SELECT set_config('app.tenant_id', $1, false)",
                    str(_REGISTRY["beta-business-proof"]),
                )
                assert (
                    await reader.fetchval("SELECT count(*) FROM delegation_events")
                    == 117
                )
                # The slug key, which used to be the ONLY key that worked,
                # is now the one that returns nothing -- the conversion moved
                # the identity, it did not duplicate it.
                await reader.execute(
                    "SELECT set_config('app.tenant_id', $1, false)",
                    "beta-business-proof",
                )
                with pytest.raises(asyncpg.PostgresError):
                    await reader.fetchval("SELECT count(*) FROM delegation_events")
            finally:
                await reader.close()

    async def test_green_reapplying_the_conversion_is_a_no_op(self) -> None:
        """The .201 dev lane, where 0031 already converted the column.

        This file must be safe to apply there, and must still leave the policy
        and grant in their intended end state.
        """
        async with _census_schema(create_mirror=True) as (conn, schema):
            await _populate_mirror(conn)
            await _apply_conversion(conn)
            survivors = await conn.fetchval("SELECT count(*) FROM delegation_events")

            await _apply_conversion(conn)

            assert await _column_type(conn, schema) == "uuid"
            assert (
                await conn.fetchval("SELECT count(*) FROM delegation_events")
                == survivors
            )
            predicate = await conn.fetchval(
                "SELECT pg_get_expr(polqual, polrelid) FROM pg_policy "
                "WHERE polrelid = 'delegation_events'::regclass "
                "AND polname = 'tenant_isolation'"
            )
            assert "::uuid" in predicate

    # -- ORDERING ----------------------------------------------------------

    async def test_missing_mirror_with_rows_present_names_the_ordering_violation(
        self,
    ) -> None:
        """Node directories are applied in sort order and
        node_projection_delegation sorts BEFORE node_projection_tenant_registry.

        On a lane that already holds delegation rows, that ordering is a real
        hazard, and the abort must say so rather than failing on a missing
        relation with a bare Postgres error.
        """
        async with _census_schema(create_mirror=False) as (conn, _schema):
            with pytest.raises(asyncpg.PostgresError) as excinfo:
                await _apply_conversion(conn)
            message = str(excinfo.value)
            assert "ORDERING VIOLATED" in message
            assert "0000_create_tenant_registry_mirror.sql" in message

    # -- AC2 ---------------------------------------------------------------

    async def test_the_mirror_is_readable_with_no_tenant_guc_set(self) -> None:
        """AC2, and the single property the whole mechanism rests on.

        The migrate Job connects as role_omnidash: NOBYPASSRLS, not the owner,
        and with ``app.tenant_id`` unset. If the mirror were RLS-covered it
        would be invisible to that identity, the conversion would resolve every
        row to NULL, and the failure would be indistinguishable from the
        OMN-16493 one it exists to fix. Asserted directly rather than inferred
        from the absence of a policy statement in the migration.
        """
        async with _census_schema(create_mirror=True) as (conn, schema):
            await _populate_mirror(conn)
            await conn.execute(
                f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {_READER_ROLE}"
            )

            flags = await conn.fetchrow(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = 'tenant_registry_mirror'::regclass"
            )
            assert flags["relrowsecurity"] is False
            assert flags["relforcerowsecurity"] is False

            reader = await asyncpg.connect(_reader_dsn(schema))
            try:
                # No set_config at all -- the exact state the migrate Job runs in.
                assert await reader.fetchval(
                    "SELECT count(*) FROM tenant_registry_mirror"
                ) == len(_REGISTRY)
            finally:
                await reader.close()

    async def test_the_mirror_refuses_two_slugs_pointing_at_one_tenant(self) -> None:
        """Two slugs resolving to the same UUID would merge two tenants' rows
        during a conversion -- the cross-tenant reassignment OMN-15683 exists
        to prevent. The unique index is the mechanical guard."""
        async with _census_schema(create_mirror=True) as (conn, _schema):
            await _populate_mirror(conn)
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    "INSERT INTO tenant_registry_mirror "
                    "(tenant_slug, tenant_uuid, status) VALUES ($1, $2, 'active')",
                    "an-alias-slug",
                    str(_REGISTRY["t-external-fixture-omn17288"]),
                )
