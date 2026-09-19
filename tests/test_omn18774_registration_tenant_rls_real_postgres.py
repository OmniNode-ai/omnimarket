# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18774 AC2: ``node_service_registry`` is writable by its OWN writer.

THE DEFECT. ``node_service_registry`` is declared ``schema: omninode_internal``
by ``node_projection_registration/contract.yaml``. Its live writer --
``RegistrationProjectionRunner._project_introspection``, the async runner that
actually runs on the .201 dev lane -- issues an INSERT naming nine columns, and
``tenant_id`` is not one of them, with no ``tenant=`` argument, so the statement
opens no tenant transaction either. Migration 0002 nevertheless put a
``tenant_isolation`` policy on the relation predicated on
``current_setting('app.tenant_id', true)``.

That predicate is therefore one NO DECLARED WRITER CAN EVER SATISFY. It has not
been observed because ``relforcerowsecurity`` is off and the lane's connection
owns the table, so PostgreSQL exempts the session entirely. The moment the
runtime connects as the non-bypassing login dev-lane security parity requires
(OMN-18256 AC1/AC2), every write to this relation is refused.

WHY A REAL POSTGRES, AND WHY A NON-OWNER ROLE. The whole defect lives in the
exemption. A mock records the statement the writer composed and is blind by
construction to a policy; the OWNER connection is blind to it too, which is
precisely why the defect survived a year of green lane deploys. Only a
NOBYPASSRLS non-owner role asks the question. SKIPS (never ERRORs) without a
reachable Postgres, matching ``test_omn17288_migration_policy_atomicity``.

THE END STATE THIS ASSERTS is the one the operator already ruled:
``docs/tracking/ROLLING_WORK_LEDGER.md:654`` (OPERATOR-CONSENT,
``approved_by=operator``, 2026-09-14T12:11:31Z) puts "relations classified
OMNINODE_INTERNAL or ambiguous" in its OUT OF SCOPE list. Internal relations
receive no tenant stamping and no row-level security. Migration 0007 applies
it; this file proves the applied result against the real writer.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

asyncpg = pytest.importorskip("asyncpg")

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_registration"
    / "migrations"
)

#: The migration chain this relation's posture is the product of, in the order
#: the forward runner applies it. Read as bytes off disk -- a hand-written
#: CREATE TABLE here would be a fixture asserting itself.
CHAIN = (
    "0000_create_node_service_registry.sql",
    "0001_add_heartbeat_columns.sql",
    "0002_node_service_registry_tenant_rls.sql",
    "0003_reconcile_heartbeat_observability.sql",
    "0004_node_service_registry_no_force_rls.sql",
    "0007_node_service_registry_drop_tenant_posture.sql",
)

#: The chain as it stood BEFORE this ticket -- the RED control.
CHAIN_BEFORE = CHAIN[:-1]

WRITER_ROLE = "omn18774_registry_writer"


def _credential() -> str:
    """The admin secret, reused for the test writer role.

    Deliberately NOT a literal in this file, and deliberately the same value
    as the admin connection already uses: this fixture must not introduce a
    new secret into the repository, and a test role is not an identity anyone
    provisions.
    """
    value = os.environ.get("INTEGRATION_POSTGRES_PASSWORD") or os.environ.get(
        "POSTGRES_PASSWORD"
    )
    if not value:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD/POSTGRES_PASSWORD unset -- skipping "
            "the node_service_registry RLS proof (it needs a real Postgres)"
        )
    return value


def _admin_dsn() -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnidash_analytics")
    return f"postgresql://{user}:{quote_plus(_credential())}@{host}:{port}/{database}"


def _writer_dsn() -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnidash_analytics")
    return (
        f"postgresql://{WRITER_ROLE}:{quote_plus(_credential())}"
        f"@{host}:{port}/{database}"
    )


async def _connect_or_skip() -> Any:
    dsn = _admin_dsn()
    try:
        connection = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # pragma: no cover - environment dependent
        # `pytest.skip` raises, but its signature is not NoReturn, so a bare
        # call here reads to a static analyser as a path that falls through
        # and returns None. Raising explicitly makes every path terminal.
        raise pytest.skip.Exception(f"Postgres unreachable: {exc}") from exc
    return connection


async def _apply(conn: Any, chain: tuple[str, ...]) -> None:
    await conn.execute("DROP TABLE IF EXISTS node_service_registry CASCADE")
    await conn.execute(
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard')
          THEN CREATE ROLE app_dashboard NOLOGIN; END IF;
        END $$;
        """
    )
    # Quoting is delegated to the server (`quote_literal`) rather than
    # hand-rolled here: a DO block takes no parameters, and a test that
    # concatenates a credential into DDL by hand is the wrong example to
    # leave in the tree.
    literal = await conn.fetchval("SELECT quote_literal($1::text)", _credential())
    if not await conn.fetchval(
        "SELECT 1 FROM pg_roles WHERE rolname = $1", WRITER_ROLE
    ):
        await conn.execute(
            f"CREATE ROLE {WRITER_ROLE} LOGIN NOBYPASSRLS PASSWORD {literal}"
        )
    else:
        await conn.execute(f"ALTER ROLE {WRITER_ROLE} PASSWORD {literal}")
    for name in chain:
        await conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
    # The writer is a NON-OWNER with the grants a projection writer holds and
    # nothing more. It is deliberately NOT the table owner: an owner is exempt
    # from a non-FORCEd policy, which is the exemption that hid this defect.
    await conn.execute(
        f"GRANT USAGE ON SCHEMA public TO {WRITER_ROLE};"
        f"GRANT SELECT, INSERT, UPDATE ON node_service_registry TO {WRITER_ROLE};"
    )


@pytest.fixture
async def admin() -> AsyncIterator[Any]:
    conn = await _connect_or_skip()
    try:
        yield conn
    finally:
        await conn.execute("DROP TABLE IF EXISTS node_service_registry CASCADE")
        await conn.close()


def _introspection_event() -> dict[str, Any]:
    return {
        "service_name": "omn18774-probe",
        "service_url": "http://omn18774-probe:8080",
        "service_type": "runtime",
        "health_status": "healthy",
        "node_name": "omn18774-probe",
    }


async def _drive_the_real_writer(dsn: str) -> None:
    """Run the event through ``RegistrationProjectionRunner``, not a double."""
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
    from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
        RegistrationProjectionRunner,
    )
    from omnimarket.projection.runner import MessageMeta

    runner = RegistrationProjectionRunner()
    adapter = AsyncpgAdapter(dsn=dsn, min_size=1, max_size=2)
    runner._db = adapter  # type: ignore[assignment]
    await adapter.connect()
    try:
        await runner._project_introspection(
            _introspection_event(),
            MessageMeta(
                partition=0, offset=1, fallback_id="omn18774", topic="omn18774"
            ),
        )
    finally:
        await adapter.close()


def _kernel_operations(dsn: str) -> Any:
    from tests.helpers.autowired_projection import autowired_operations

    return autowired_operations(
        node="node_projection_registration",
        relation="node_service_registry",
        dsn=dsn,
        principal=WRITER_ROLE,
        physical_database=os.environ.get(
            "INTEGRATION_POSTGRES_DB", "omnidash_analytics"
        ),
    )


def _drive_the_kernel_writer(dsn: str) -> None:
    """Run the SYNC twin through the REAL auto-wired internal operation."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from omnibase_core.enums.enum_node_kind import EnumNodeKind

    from omnimarket.nodes.node_projection_registration.handlers.handler_projection_registration import (
        HandlerProjectionRegistration,
        ModelNodeIntrospectionEvent,
    )

    event = ModelNodeIntrospectionEvent(
        node_id=uuid4(),
        node_name="omn18774-probe",
        node_type=EnumNodeKind.EFFECT,
        correlation_id=uuid4(),
        timestamp=datetime.now(tz=UTC),
        endpoints={"http": "http://omn18774-probe:8080"},
    )
    HandlerProjectionRegistration().project_introspection(
        event, _kernel_operations(dsn)
    )


class TestTheDefectOnTheKernelPath:
    """The RED control: the pre-0007 relation refuses the SYNC twin outright.

    This is the path a runtime-kernel pod dispatches, and it is the one the
    policy actually decides: ``InternalProjectionTableOperation`` passes no
    ``recorded_scope``, so ``_statement_tenant_scope`` returns ``None`` and the
    statement issues NO ``set_config('app.tenant_id', ...)`` at all. Against a
    NOBYPASSRLS non-owner role the ``WITH CHECK`` half compares the DDL-default
    ``'omninode'`` to an unset GUC -- NULL -- and refuses.
    """

    async def test_registration_tenant_rls_refuses_the_kernel_writer_before_0007(
        self, admin: Any
    ) -> None:
        await _apply(admin, CHAIN_BEFORE)

        policy = await admin.fetchval(
            "SELECT qual FROM pg_policies WHERE tablename = "
            "'node_service_registry' AND policyname = 'tenant_isolation'"
        )
        assert policy is not None, "the tenant_isolation policy must be present"
        assert "app.tenant_id" in policy, (
            "the RED control must run against the GUC-predicated policy the "
            "lane actually carries"
        )

        # The refusal can come from Postgres (policy) or from the operation
        # class (rejected key), and WHICH one is the thing under test, so
        # the type is deliberately not narrowed here; the assertion below
        # pins the message.
        with pytest.raises(Exception, match=r"(?s).") as caught:
            _drive_the_kernel_writer(_writer_dsn())
        assert "row-level security policy" in str(caught.value), caught.value

        assert (
            await admin.fetchval("SELECT count(*) FROM node_service_registry") == 0
        ), "a refused write must leave zero rows"


class TestWhyTheAsyncPathNEVERSHOWEDIT:
    """A CORRECTION to this ticket's own AC2 premise, proven rather than argued.

    AC2 predicted the ASYNC writer would be refused today as well, "with
    InsufficientPrivilege". It is not, and the reason matters more than the
    prediction: ``AsyncpgAdapter._set_tenant_context`` sets ``app.tenant_id``
    on EVERY statement, and when the caller passes no ``tenant=`` it
    synthesises one from ``resolve_read_tenant(None)`` -- the house SLUG. The
    relation's column DEFAULT is the same house slug. So the write passes
    because two INDEPENDENT inventions -- one in the adapter, one in the DDL --
    happen to agree, and neither is the writer.

    That is a worse state than a refusal, not a better one. A refusal is
    visible; a coincidence is not, and it breaks the moment either side moves
    (a lane whose column holds a UUID, a caller that threads a real tenant, or
    the kernel path above, which synthesises nothing). Asserting it here keeps
    the correction in the tree rather than only in a pull-request body.
    """

    async def test_the_async_write_passes_on_a_coincidence_before_0007(
        self, admin: Any
    ) -> None:
        await _apply(admin, CHAIN_BEFORE)

        await _drive_the_real_writer(_writer_dsn())

        stored = await admin.fetchval(
            "SELECT tenant_id FROM node_service_registry WHERE service_name = "
            "'omn18774-probe'"
        )
        default = await admin.fetchval(
            "SELECT column_default FROM information_schema.columns WHERE "
            "table_name = 'node_service_registry' AND column_name = 'tenant_id'"
        )
        assert stored == "omninode"
        assert default is not None, "the column DEFAULT must still be present"
        assert "omninode" in default, (
            "the stored attribution is the column DEFAULT, authored by the "
            "schema -- the INSERT names no tenant_id at all"
        )

    async def test_the_async_write_is_refused_once_the_two_inventions_disagree(
        self, admin: Any
    ) -> None:
        """Move ONE of the two, and the coincidence stops holding."""
        await _apply(admin, CHAIN_BEFORE)
        await admin.execute(
            "ALTER TABLE node_service_registry ALTER COLUMN tenant_id "
            "SET DEFAULT 'some-real-tenant'"
        )

        # The refusal can come from Postgres (policy) or from the operation
        # class (rejected key), and WHICH one is the thing under test, so
        # the type is deliberately not narrowed here; the assertion below
        # pins the message.
        with pytest.raises(Exception, match=r"(?s).") as caught:
            await _drive_the_real_writer(_writer_dsn())
        assert "row-level security policy" in str(caught.value), caught.value


class TestTheEndState:
    """The ruled end state, applied by 0007 and proven against the writer."""

    async def test_registration_tenant_rls_posture_is_gone_after_0007(
        self, admin: Any
    ) -> None:
        await _apply(admin, CHAIN)

        relrowsecurity, relforce = await admin.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'node_service_registry'"
        )
        assert relrowsecurity is False
        assert relforce is False
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM pg_policies WHERE tablename = "
                "'node_service_registry'"
            )
            == 0
        )
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM information_schema.columns WHERE "
                "table_name = 'node_service_registry' AND column_name = "
                "'tenant_id'"
            )
            == 0
        ), (
            "the column is dropped, not kept unstamped: NOT NULL with no "
            "DEFAULT refuses every write, and NULLABLE with no DEFAULT is an "
            "attribution surface nothing fills"
        )

    async def test_registration_tenant_rls_removal_lets_the_real_writer_land_a_row(
        self, admin: Any
    ) -> None:
        await _apply(admin, CHAIN)

        await _drive_the_real_writer(_writer_dsn())

        row = await admin.fetchrow(
            "SELECT service_name, service_type, is_active FROM "
            "node_service_registry WHERE service_name = 'omn18774-probe'"
        )
        assert row is not None, (
            "the real writer, as a NOBYPASSRLS non-owner, must be able to "
            "write the relation its own contract declares it owns"
        )
        assert row["service_type"] == "runtime"
        assert row["is_active"] is True

    async def test_registration_tenant_rls_removal_lets_the_kernel_writer_land_a_row(
        self, admin: Any
    ) -> None:
        """The path that was refused before 0007 is the path that must work.

        The async proof above is necessary but not sufficient: it passed
        before 0007 too, on the coincidence. Only this one changes verdict.
        """
        await _apply(admin, CHAIN)

        _drive_the_kernel_writer(_writer_dsn())

        row = await admin.fetchrow(
            "SELECT service_name, service_type, is_active FROM "
            "node_service_registry WHERE service_name = 'omn18774-probe'"
        )
        assert row is not None
        assert row["is_active"] is True
