# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18565: one correlation's ``delegation_events`` row must land under the
submitting tenant whichever of its two subscriptions is scheduled first.

MEASURED CAUSE (staging ``onex-dev``, dev-system EC2 ``i-06169517a92b45f86``,
2026-09-17). ``delegation_events`` rows for one correlation are written by TWO
separate Kafka subscriptions inside the same deployment:

* ``onex.evt.omnibase-infra.quality-gate-result.v1`` -- the verdict. It carried
  no tenant, so the write authored the HOUSE tenant
  ``820272f9-4aaf-5add-a2df-0af942852ab2``.
* ``onex.evt.omnibase-infra.delegation-completed.v1`` -- the terminal. It
  carries the real submitting tenant in its own payload.

Both UPSERT on ``correlation_id``, so whichever lands first CREATES the row.
When the verdict wins, the terminal's ``INSERT ... ON CONFLICT DO UPDATE`` is
evaluated against the PRE-EXISTING house row by the ``USING`` half of the
``tenant_isolation`` policy, under ``FORCE ROW LEVEL SECURITY``, with the GUC
set to the real tenant -- and Postgres refuses::

    psycopg2.errors.InsufficientPrivilege: new row violates row-level security
    policy (USING expression) for table "delegation_events"

The ``(USING expression)`` suffix is the discriminator: a plain INSERT refusal
does not carry it, only the conflict-update path does. The terminal write never
lands, the tenant-scoped reader finds nothing in its own partition, and the
staging business proof fails on ``quality_gate``. When the terminal wins the
race instead, the row is created attributed and the proof passes. That race --
not any code change -- is the whole difference between a green run and a red
one: roughly three passes in sixteen proof runs over 24 hours.

WHY A REAL DATABASE, AND WHY A LEAST-PRIVILEGE ROLE. The defect is a property of
how PostgreSQL evaluates a row-level-security policy across the two arms of an
UPSERT. No in-memory or mock adapter has a policy at all, and a SUPERUSER
connection is exempt from one even when the relation carries
``FORCE ROW LEVEL SECURITY`` -- so every mock-DB and every superuser-fixture
test in this repo passes while the live writer refuses. The fixture below
provisions a ``NOSUPERUSER NOBYPASSRLS`` LOGIN role and drives the PRODUCTION
adapter (:class:`PostgresSyncProjectionAdapter`) through it, and
:class:`TestTheFixtureReallyBindsRowLevelSecurity` proves that before any
behaviour is asserted on it.

WHY ``HandlerProjectionDelegation`` AND NOT ``handler_delegation``. The writer
deployed as ``omnimarket-projection-delegation-writer`` is dispatched by the
omnibase_infra runtime kernel's auto-wiring, which calls THIS handler. Its live
traceback bottoms out at ``handler_projection_delegation.py`` ->
``_write_delegation_row`` -> ``upsert_returning``. The async twin in
``handler_delegation.py`` is a different path and its own suites cover it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
    ModelProjectionTaskDelegatedEvent,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter

psycopg2 = pytest.importorskip("psycopg2")

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)

#: 0031 is fenced out of the test apply for the same reason
#: ``test_omn16804_registry_resolved_write_tenant_real_postgres`` fences it: it
#: is superseded in-place by 0032-0034 and applying both produces a shape no
#: lane has ever carried.
_FENCED_MIGRATION = "0031_delegation_events_tenant_id_to_uuid.sql"

#: Applied as its OWN stage, after the onex-dev pre-state below, so the fixture
#: can prove the default was there to be removed. Applying it inside the bulk
#: loop would let the pre-state normalisation put the default back and make
#: every assertion about it vacuous.
_UNDER_TEST_MIGRATION = "0042_delegation_events_drop_house_tenant_default.sql"

#: The house tenant the OMN-16831 writer ruling stamps for an unattributed row,
#: and -- until this ticket's migration -- the ``delegation_events.tenant_id``
#: column DEFAULT.
_HOUSE_SLUG = "omninode"
_HOUSE_UUID = "820272f9-4aaf-5add-a2df-0af942852ab2"

#: The submitting tenant of the staging business proof, as measured.
_REAL_SLUG = "beta-business-proof"
_REAL_UUID = "91c74442-1233-4c97-b191-911a10346fdf"

_EVENT_TIMESTAMP = datetime(2026, 9, 17, 6, 57, 48, tzinfo=UTC)

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

#: The onex-dev PRE-STATE, reproduced: ``tenant_id`` is UUID, the relation
#: carries the ``tenant_isolation`` policy, and the column DEFAULT attributes an
#: unnamed row to the house tenant. That default is the mechanism this ticket
#: removes, so the fixture must actually install it -- otherwise the DDL
#: assertion below would pass against a schema nobody changed. Applied the way
#: omnibase_infra's own 0032 applies it, and unconditionally re-asserting the
#: DEFAULT so the stage is idempotent whichever guarded arm the bulk migrations
#: happened to take.
_ONEX_DEV_PRE_STATE = """
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

    IF v_data_type <> 'uuid' THEN
        DROP POLICY IF EXISTS tenant_isolation ON delegation_events;
        ALTER TABLE delegation_events ALTER COLUMN tenant_id DROP DEFAULT;
        ALTER TABLE delegation_events
            ALTER COLUMN tenant_id TYPE UUID USING (NULLIF(tenant_id, '')::uuid);
        CREATE POLICY tenant_isolation ON delegation_events
          FOR ALL
          USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
          WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
    END IF;

    ALTER TABLE delegation_events
        ALTER COLUMN tenant_id SET DEFAULT '820272f9-4aaf-5add-a2df-0af942852ab2'::uuid;
END$$;
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


def _dsn_for(user: str, secret: str, *, schema: str | None = None) -> str:
    _, _, host, port, db = _pg_settings()
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"
    if schema is not None:
        dsn += f"?options={quote_plus(f'-csearch_path={schema},public')}"
    return dsn


def _test_schema_safe_sql(raw_sql: str) -> str:
    """``CREATE INDEX CONCURRENTLY`` refuses to run inside a transaction block.

    It exists only to avoid locking a live table with real traffic, which is
    meaningless on a disposable schema, so a plain ``CREATE INDEX`` is
    schema-equivalent here. Same helper, same reasoning, as the other
    real-Postgres suites in this directory.
    """
    return raw_sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")


def _live_migration_files() -> list[Path]:
    return [
        path
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if path.name not in (_FENCED_MIGRATION, _UNDER_TEST_MIGRATION)
    ]


def _migration_under_test() -> Path:
    path = _MIGRATIONS_DIR / _UNDER_TEST_MIGRATION
    if not path.is_file():
        raise AssertionError(
            f"{_UNDER_TEST_MIGRATION} is absent: OMN-18565's forward migration "
            "is what removes the house-tenant column default, and without it "
            "this module would assert the property against a schema nobody "
            "changed"
        )
    return path


class _RecordingPublisher:
    """Captures snapshot deltas instead of reaching a broker."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def publish(self, message: Any) -> bool:
        self.messages.append(message)
        return True


def _handler() -> HandlerProjectionDelegation:
    return HandlerProjectionDelegation(publisher=_RecordingPublisher())


def _verdict(correlation_id: str) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=correlation_id,
        passed=True,
        quality_score=1.0,
        actual_score=1.0,
    )


def _terminal(correlation_id: str) -> ModelProjectionTaskDelegatedEvent:
    """The delegation-completed terminal, carrying the SUBMITTING tenant.

    Its ``model_name`` and ``tokens_output`` are the fields the staging business
    proof reads back, and the fields the house-tenant verdict row does not
    carry -- the measured difference between a green row and a red one.
    """
    return ModelProjectionTaskDelegatedEvent(
        correlation_id=correlation_id,
        tenant_id=_REAL_SLUG,
        task_type="summarization",
        delegated_to="node_delegate_skill_orchestrator",
        model_name="glm-5.3-flash",
        tokens_output=130,
    )


class _Lane:
    """One disposable schema, and the two connections that see it differently."""

    def __init__(
        self,
        adapter: PostgresSyncProjectionAdapter,
        admin: Any,
        writer_role: str,
        writer_dsn: str,
    ) -> None:
        self.adapter = adapter
        self.admin = admin
        self.writer_role = writer_role
        self.writer_dsn = writer_dsn


def _admin_connection_or_skip(user: str, secret: str) -> Any:
    """Return an admin connection, or SKIP.

    A separate function rather than an inline try/except so the connection is a
    RETURN value: ``pytest.skip`` raises, but a static analyser that does not
    know that reads the inline form as leaving the local unbound on the except
    path, and reports a use-before-initialisation on the teardown that follows.
    """
    try:
        return psycopg2.connect(_dsn_for(user, secret))
    except psycopg2.Error as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-18565 proof: {exc}")


def _provision(*, apply_migration_under_test: bool) -> Iterator[_Lane]:
    """Build the lane in three stages, in the order a real lane reached it.

    1. every migration except the fenced one and the one under test;
    2. the tenant-registry mirror and the onex-dev pre-state, which installs the
       house-tenant column DEFAULT this ticket removes;
    3. optionally, the migration under test.

    Staging it this way is what makes the DDL assertion non-vacuous: stage 2
    proves the default was present, stage 3 proves this ticket's migration is
    what removed it. Folding stage 3 into stage 1 would let stage 2 put the
    default back and the assertion would prove nothing.
    """
    admin_user, secret, _host, _port, database = _pg_settings()
    if not secret:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD / POSTGRES_PASSWORD not set -- "
            "skipping the OMN-18565 ordering-independence proof; a mock "
            "adapter has no row-level security policy and cannot observe the "
            "conflict-update refusal this module is about"
        )
    admin = _admin_connection_or_skip(admin_user, secret)
    admin.autocommit = True
    suffix = uuid4().hex[:12]
    schema = f"omn18565_{suffix}"
    writer_role = f"omn18565_w_{suffix}"
    writer_secret = uuid4().hex
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}, public")
            cur.execute(_APP_DASHBOARD_ROLE_SQL)
            for path in _live_migration_files():
                cur.execute(_test_schema_safe_sql(path.read_text(encoding="utf-8")))
            cur.execute(_MIRROR_DDL)
            cur.execute(_ONEX_DEV_PRE_STATE)
            if apply_migration_under_test:
                cur.execute(
                    _test_schema_safe_sql(
                        _migration_under_test().read_text(encoding="utf-8")
                    )
                )
            for slug, tenant_uuid in (
                (_HOUSE_SLUG, _HOUSE_UUID),
                (_REAL_SLUG, _REAL_UUID),
            ):
                cur.execute(
                    "INSERT INTO tenant_registry_mirror "
                    "(tenant_slug, tenant_uuid, status) "
                    "VALUES (%s, %s::uuid, 'active') "
                    "ON CONFLICT (tenant_slug) DO NOTHING",
                    (slug, tenant_uuid),
                )
            cur.execute(
                f"CREATE ROLE {writer_role} WITH LOGIN PASSWORD %s "
                "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION",
                (writer_secret,),
            )
            cur.execute(f'GRANT CONNECT ON DATABASE "{database}" TO {writer_role}')
            cur.execute(f"GRANT USAGE ON SCHEMA {schema} TO {writer_role}")
            cur.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                f"{schema} TO {writer_role}"
            )
            cur.execute(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} "
                f"TO {writer_role}"
            )
        writer_dsn = _dsn_for(writer_role, writer_secret, schema=schema)
        yield _Lane(
            PostgresSyncProjectionAdapter(writer_dsn), admin, writer_role, writer_dsn
        )
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute(f'REVOKE CONNECT ON DATABASE "{database}" FROM {writer_role}')
            cur.execute(f"DROP ROLE IF EXISTS {writer_role}")
        admin.close()


@pytest.fixture
def lane() -> Iterator[_Lane]:
    """The lane as this ticket leaves it: the migration under test applied."""
    yield from _provision(apply_migration_under_test=True)


@pytest.fixture
def lane_before_the_migration() -> Iterator[_Lane]:
    """The lane as onex-dev carries it today, for the DDL positive control."""
    yield from _provision(apply_migration_under_test=False)


def _fetch_row(admin: Any, correlation_id: str) -> dict[str, Any] | None:
    """Read the stored row as the ADMIN, so the assertion sees the table's real
    contents rather than one tenant's row-level-security-filtered view of it."""
    from psycopg2.extras import RealDictCursor

    with admin.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT tenant_id, model_name, tokens_output, task_type, "
            f"quality_gate_passed FROM {TABLE} WHERE correlation_id = %s",
            (correlation_id,),
        )
        record = cur.fetchone()
    return dict(record) if record is not None else None


def _row_count(admin: Any, correlation_id: str) -> int:
    with admin.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM {TABLE} WHERE correlation_id = %s",
            (correlation_id,),
        )
        return int(cur.fetchone()[0])


def _tenant_column_default(admin: Any) -> str | None:
    with admin.cursor() as cur:
        cur.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "AND column_name = 'tenant_id'",
            (TABLE,),
        )
        record = cur.fetchone()
    assert record is not None, f"{TABLE}.tenant_id is absent from the fixture schema"
    default = record[0]
    return None if default is None else str(default)


@pytest.mark.integration
class TestTheFixtureReallyBindsRowLevelSecurity:
    """Positive control. Without this every assertion below could pass for the
    wrong reason on a connection the policy never applied to."""

    def test_the_writer_role_cannot_bypass_rls(self, lane: _Lane) -> None:
        with lane.admin.cursor() as cur:
            cur.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
                (lane.writer_role,),
            )
            record = cur.fetchone()
        assert record is not None
        assert record[0] is False, "the writer role is a superuser: policy exempt"
        assert record[1] is False, "the writer role has BYPASSRLS: policy exempt"

    def test_the_relation_forces_row_level_security(self, lane: _Lane) -> None:
        with lane.admin.cursor() as cur:
            cur.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = %s::regclass",
                (TABLE,),
            )
            record = cur.fetchone()
        assert record is not None
        assert record[0] is True, "row-level security is not enabled on the relation"
        assert record[1] is True, "FORCE ROW LEVEL SECURITY is not set"


@pytest.mark.integration
class TestTheConflictUpdateRefusalIsPinned:
    """AC3. The refusal itself, reproduced against a real policy.

    This is characterisation rather than a guard on our own code: it pins the
    PostgreSQL behaviour the whole ticket rests on, so a later reader can tell a
    fixed writer from a weakened policy. It must keep raising.
    """

    def test_a_cross_tenant_conflict_update_is_refused_with_using_expression(
        self, lane: _Lane
    ) -> None:
        correlation_id = str(uuid4())
        with lane.admin.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE} (correlation_id, tenant_id, timestamp) "
                "VALUES (%s, %s::uuid, now())",
                (correlation_id, _HOUSE_UUID),
            )
        conn = psycopg2.connect(lane.writer_dsn)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (_REAL_UUID,)
                )
                with pytest.raises(psycopg2.errors.InsufficientPrivilege) as excinfo:
                    cur.execute(
                        f"INSERT INTO {TABLE} "
                        "(correlation_id, tenant_id, timestamp) "
                        "VALUES (%s, %s::uuid, now()) "
                        "ON CONFLICT (correlation_id) DO UPDATE "
                        "SET tenant_id = EXCLUDED.tenant_id",
                        (correlation_id, _REAL_UUID),
                    )
            conn.rollback()
        finally:
            conn.close()
        message = str(excinfo.value)
        assert "row-level security policy" in message
        # The discriminator. A plain INSERT refusal does not carry this suffix;
        # only the conflict-update path evaluates the USING half against the
        # pre-existing row.
        assert "(USING expression)" in message

    def test_the_same_write_succeeds_when_the_two_tenants_agree(
        self, lane: _Lane
    ) -> None:
        """Negative control on the refusal above.

        Without it, a fixture that refused every write for an unrelated reason
        (a missing grant, a wrong search_path) would read exactly like a proof
        that the policy is doing its job.
        """
        correlation_id = str(uuid4())
        with lane.admin.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE} (correlation_id, tenant_id, timestamp) "
                "VALUES (%s, %s::uuid, now())",
                (correlation_id, _REAL_UUID),
            )
        conn = psycopg2.connect(lane.writer_dsn)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (_REAL_UUID,)
                )
                cur.execute(
                    f"INSERT INTO {TABLE} (correlation_id, tenant_id, timestamp) "
                    "VALUES (%s, %s::uuid, now()) "
                    "ON CONFLICT (correlation_id) DO UPDATE "
                    "SET tenant_id = EXCLUDED.tenant_id",
                    (correlation_id, _REAL_UUID),
                )
            conn.commit()
        finally:
            conn.close()
        assert _row_count(lane.admin, correlation_id) == 1


@pytest.mark.integration
class TestOrderingIndependence:
    """AC4, the labelled criterion. The outcome must not depend on which of the
    two subscriptions is scheduled first.

    Both orderings must leave exactly ONE row, attributed to the submitting
    tenant, carrying the terminal's ``model_name`` and ``tokens_output``. Today
    the two orderings produce different rows and only one of them passes the
    business proof, which is what makes the staging gate a coin flip rather
    than a check.
    """

    def _assert_terminal_row(self, lane: _Lane, correlation_id: str) -> None:
        assert _row_count(lane.admin, correlation_id) == 1
        row = _fetch_row(lane.admin, correlation_id)
        assert row is not None
        assert str(row["tenant_id"]) == _REAL_UUID
        assert row["model_name"] == "glm-5.3-flash"
        assert row["tokens_output"] == 130
        assert row["task_type"] == "summarization"

    def test_terminal_then_unattributed_verdict(self, lane: _Lane) -> None:
        correlation_id = str(uuid4())
        _handler().project(_terminal(correlation_id), lane.adapter)
        _handler().project_quality_gate_result(
            _verdict(correlation_id),
            lane.adapter,
            tenant_identity=None,
            event_timestamp=_EVENT_TIMESTAMP,
        )
        self._assert_terminal_row(lane, correlation_id)

    def test_unattributed_verdict_then_terminal(self, lane: _Lane) -> None:
        """The ordering the staging gate fails on.

        The verdict must not create a row under an identity the terminal will
        disagree with: the terminal's conflict-update is then refused by the
        USING half of the policy and the row the reader needs never exists.
        """
        correlation_id = str(uuid4())
        _handler().project_quality_gate_result(
            _verdict(correlation_id),
            lane.adapter,
            tenant_identity=None,
            event_timestamp=_EVENT_TIMESTAMP,
        )
        _handler().project(_terminal(correlation_id), lane.adapter)
        self._assert_terminal_row(lane, correlation_id)

    @pytest.mark.parametrize("verdict_first", [True, False])
    def test_an_attributed_verdict_lands_in_either_order(
        self, lane: _Lane, verdict_first: bool
    ) -> None:
        """The same property once the producer stamp reaches the writer.

        Negative control on the refusal: a verdict that DOES carry the
        submitting tenant must still be applied, in both orderings, or "fail
        closed" has quietly become "drop every verdict".
        """
        correlation_id = str(uuid4())

        def _write_verdict() -> int:
            return (
                _handler()
                .project_quality_gate_result(
                    _verdict(correlation_id),
                    lane.adapter,
                    tenant_identity=_REAL_SLUG,
                    event_timestamp=_EVENT_TIMESTAMP,
                )
                .rows_upserted
            )

        def _write_terminal() -> int:
            return (
                _handler()
                .project(_terminal(correlation_id), lane.adapter)
                .rows_upserted
            )

        if verdict_first:
            verdict_rows = _write_verdict()
            _write_terminal()
        else:
            _write_terminal()
            verdict_rows = _write_verdict()

        # The verdict was APPLIED rather than refused, in both orderings. This
        # is the assertion that stops "fail closed" from widening into "drop
        # every verdict": the row's own columns cannot carry it, because the
        # terminal is the authority on quality_gate_passed and restates it on
        # whichever arm it takes.
        assert verdict_rows == 1
        self._assert_terminal_row(lane, correlation_id)

        if not verdict_first:
            # Terminal first, so the verdict is the LAST writer and its own
            # fact must be visible on the stored row.
            row = _fetch_row(lane.admin, correlation_id)
            assert row is not None
            assert row["quality_gate_passed"] is True


@pytest.mark.integration
class TestAnUnattributedVerdictIsRefused:
    """AC5. A verdict that cannot be attributed is refused with a named reason
    and authors no row, rather than silently taking the house tenant."""

    def test_no_row_is_created_and_the_reason_is_named(
        self, lane: _Lane, caplog: pytest.LogCaptureFixture
    ) -> None:
        correlation_id = str(uuid4())
        with caplog.at_level(logging.ERROR):
            result = _handler().project_quality_gate_result(
                _verdict(correlation_id),
                lane.adapter,
                tenant_identity=None,
                event_timestamp=_EVENT_TIMESTAMP,
            )
        assert result.rows_upserted == 0
        assert _row_count(lane.admin, correlation_id) == 0
        assert any("OMN-18565" in record.getMessage() for record in caplog.records), (
            "the refusal must name its reason; a silent drop is indistinguishable "
            "from a verdict nobody ever published"
        )


@pytest.mark.integration
class TestTheHouseTenantColumnDefaultIsRemoved:
    """AC1's DDL half. A column DEFAULT that attributes an unnamed row to the
    house tenant turns a write that says nothing about its tenant into a write
    that asserts one, which is the mechanism that made the race possible."""

    def test_the_default_was_there_to_remove(
        self, lane_before_the_migration: _Lane
    ) -> None:
        """Positive control. Without it the assertion below could pass on a
        lane whose default this ticket's migration never touched."""
        default = _tenant_column_default(lane_before_the_migration.admin)
        assert default is not None
        assert _HOUSE_UUID in default

    def test_the_migration_removes_it_and_keeps_the_column_not_null(
        self, lane: _Lane
    ) -> None:
        assert _tenant_column_default(lane.admin) is None
        with lane.admin.cursor() as cur:
            cur.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s "
                "AND column_name = 'tenant_id'",
                (TABLE,),
            )
            record = cur.fetchone()
        assert record is not None
        assert record[0] == "NO", (
            "dropping the DEFAULT must not relax NOT NULL: an unattributed "
            "write has to fail, not store a NULL tenant"
        )

    def test_an_unnamed_write_is_refused_rather_than_house_attributed(
        self, lane: _Lane
    ) -> None:
        """The behaviour the DDL change buys, stated as behaviour.

        With the default gone, a statement that names no tenant can no longer be
        silently attributed: PostgreSQL refuses it. WHICH refusal is Postgres'
        choice of the first constraint it reaches, and both are correct. The
        policy's WITH CHECK is evaluated against the proposed row, where
        ``tenant_id`` is now NULL, so the comparison is NULL and the write is
        denied as a policy violation before NOT NULL is ever consulted. On a
        connection outside the policy the same statement fails 23502 instead.
        Asserting the SQLSTATE rather than the refusal would pin an
        implementation detail of evaluation order, so both are accepted and the
        assertion that matters is that no row exists afterwards.
        """
        correlation_id = str(uuid4())
        conn = psycopg2.connect(lane.writer_dsn)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (_HOUSE_UUID,)
                )
                with pytest.raises(
                    (
                        psycopg2.errors.NotNullViolation,
                        psycopg2.errors.InsufficientPrivilege,
                    )
                ):
                    cur.execute(
                        f"INSERT INTO {TABLE} (correlation_id, timestamp) "
                        "VALUES (%s, now())",
                        (correlation_id,),
                    )
            conn.rollback()
        finally:
            conn.close()
        assert _row_count(lane.admin, correlation_id) == 0

    def test_before_the_migration_the_same_write_is_silently_house_attributed(
        self, lane_before_the_migration: _Lane
    ) -> None:
        """The falsifier for the test above, run against the pre-state.

        Without this the refusal could be caused by anything -- a missing grant,
        a wrong search_path -- rather than by the DEFAULT's removal. Here the
        identical statement SUCCEEDS and stores the house tenant, which is the
        defect in one line.
        """
        lane = lane_before_the_migration
        correlation_id = str(uuid4())
        conn = psycopg2.connect(lane.writer_dsn)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (_HOUSE_UUID,)
                )
                cur.execute(
                    f"INSERT INTO {TABLE} (correlation_id, timestamp) "
                    "VALUES (%s, now())",
                    (correlation_id,),
                )
            conn.commit()
        finally:
            conn.close()
        row = _fetch_row(lane.admin, correlation_id)
        assert row is not None
        assert str(row["tenant_id"]) == _HOUSE_UUID


@pytest.mark.integration
class TestTheInsertOnlyTenantArmIsNotAPolicyBypass:
    """The insert-only fallback does NOT let an unattributed terminal reach
    another tenant's row.

    Raised as a blocking finding by the adversarial reviewer on this PR:
    ``terminal_write_tenant`` holds ``tenant_id`` out of the ``DO UPDATE SET``
    clause when it resolved no tenant, so the concern is that the UPDATE arm
    then proceeds against a row belonging to somebody else. It does not, and
    the reason is that row-level security is not evaluated against the SET
    clause at all: the ``USING`` half is evaluated against the PRE-EXISTING
    row, and the session GUC is derived from the row's own ``tenant_id`` -- the
    house tenant on this arm -- so a pre-existing row under any other tenant
    makes the predicate false and PostgreSQL refuses the whole statement.

    Stated as a measurement rather than an argument, because the claim is a
    property of a real policy and only a real policy can settle it.

    What the insert-only arm is actually for, narrowly: a backing store with no
    row-level security -- the in-memory double, SQLite, a superuser lane -- has
    no policy to refuse the write, and there the SET clause is the only thing
    standing between a late unattributed terminal and a real attribution it
    would otherwise overwrite.
    """

    def test_an_unattributed_terminal_cannot_reach_another_tenants_row(
        self, lane: _Lane
    ) -> None:
        correlation_id = str(uuid4())
        _handler().project(_terminal(correlation_id), lane.adapter)

        unattributed = ModelProjectionTaskDelegatedEvent(
            correlation_id=correlation_id,
            tenant_id=None,
            task_type="clobbered",
            delegated_to="clobbered",
            model_name="clobbered",
            tokens_output=0,
        )
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _handler().project(unattributed, lane.adapter)

        # The stored row is untouched: same tenant, same terminal facts.
        row = _fetch_row(lane.admin, correlation_id)
        assert row is not None
        assert str(row["tenant_id"]) == _REAL_UUID
        assert row["model_name"] == "glm-5.3-flash"
        assert row["task_type"] == "summarization"
        assert row["tokens_output"] == 130

    def test_the_same_terminal_lands_when_it_carries_the_tenant(
        self, lane: _Lane
    ) -> None:
        """Negative control. The refusal above must be caused by the MISSING
        attribution, not by anything else about a second terminal write."""
        correlation_id = str(uuid4())
        _handler().project(_terminal(correlation_id), lane.adapter)

        attributed = ModelProjectionTaskDelegatedEvent(
            correlation_id=correlation_id,
            tenant_id=_REAL_SLUG,
            task_type="summarization",
            delegated_to="node_delegate_skill_orchestrator",
            model_name="glm-5.3-pro",
            tokens_output=131,
        )
        _handler().project(attributed, lane.adapter)

        row = _fetch_row(lane.admin, correlation_id)
        assert row is not None
        assert str(row["tenant_id"]) == _REAL_UUID
        assert row["model_name"] == "glm-5.3-pro"
