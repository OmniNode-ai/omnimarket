# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18774 AC3: the SYNC generation projection can write on a kernel pod.

THE DEFECT. ``generation_events`` is declared ``schema: omninode_internal`` by
``node_projection_delegation/contract.yaml``, so a runtime-kernel pod hands the
handler an ``InternalProjectionTableOperation`` -- which rejects a canonical
``tenant_id`` key outright (``_reject_canonical_tenant_field``) and issues no
``set_config('app.tenant_id', ...)`` at all. OMN-16831 item 4 nevertheless made
the SYNC handler stamp ``house_tenant_write_stamp(table=GENERATION_TABLE)``
into the row. Those two landed changes are mutually exclusive: the sync
generation projection could not write the relation on a kernel pod at ALL, and
raised ``ValueError: omninode_internal operation rejects canonical tenant_id``
before any SQL was issued.

It went unobserved because the .201 dev lane runs the ASYNC twin
(``omnimarket.projection.runner``, ``Binding: legacy-settings``), whose raw
INSERT named ``tenant_id`` as ``$24`` and bound the GUC for the statement
(OMN-15919). Two twins, diverged, only one of them able to run.

WHY THE KERNEL SEAM AND NOT A DOUBLE. The guard under test lives in the
operation class the CONTRACT selects. An in-memory adapter or a hand-built
target picks no operation class at all, which is exactly the gap that let this
ship. These tests build the real ``ProjectionDatabaseOperations`` from the
node's own shipped declaration (``tests/helpers/autowired_projection.py``) and
run it against a real Postgres carrying the real migration chain.

SKIPS (never ERRORs) without a reachable Postgres, matching
``test_omn17288_migration_policy_atomicity``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

psycopg2 = pytest.importorskip("psycopg2")

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)

#: Every migration in this node's tree that names ``generation_events``, in the
#: order the forward runner applies them. Selected by content, not by hand:
#: `grep -l generation_events` over the tree returns exactly these.
CHAIN = (
    "0008_generation_events.sql",
    "0012_generation_output_columns.sql",
    "0013_generation_proof_fields.sql",
    "0014_generation_semantic_pass.sql",
    "0015_generation_corpus_acceptance.sql",
    "0027_generation_events_tenant_rls.sql",
    "0043_generation_events_drop_tenant_posture.sql",
)

#: The chain as it stood BEFORE this ticket -- the RED control.
CHAIN_BEFORE = CHAIN[:-1]

WRITER_ROLE = "omn18774_generation_writer"


def _credential() -> str:
    value = os.environ.get("INTEGRATION_POSTGRES_PASSWORD") or os.environ.get(
        "POSTGRES_PASSWORD"
    )
    if not value:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD/POSTGRES_PASSWORD unset -- skipping "
            "the generation_events internal-domain proof"
        )
    return value


def _host() -> tuple[str, str, str]:
    return (
        os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost"),
        os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"),
        os.environ.get("INTEGRATION_POSTGRES_DB", "omnidash_analytics"),
    )


def _admin_dsn() -> str:
    host, port, database = _host()
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    return f"postgresql://{user}:{quote_plus(_credential())}@{host}:{port}/{database}"


def _writer_dsn() -> str:
    host, port, database = _host()
    return (
        f"postgresql://{WRITER_ROLE}:{quote_plus(_credential())}"
        f"@{host}:{port}/{database}"
    )


@pytest.fixture
def admin() -> Iterator[Any]:
    try:
        connection = psycopg2.connect(_admin_dsn(), connect_timeout=5)
    except Exception as exc:  # pragma: no cover - environment dependent
        # Raised explicitly rather than via `pytest.skip(...)`: that helper's
        # signature is not NoReturn, so a static analyser reads the `except`
        # arm as falling through to a `connection` that was never bound.
        raise pytest.skip.Exception(f"Postgres unreachable: {exc}") from exc
    connection.autocommit = True
    try:
        yield connection
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS generation_events CASCADE")
        connection.close()


def _apply(admin: Any, chain: tuple[str, ...]) -> None:
    with admin.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS generation_events CASCADE")
        cursor.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
            "'app_dashboard') THEN CREATE ROLE app_dashboard NOLOGIN; END IF; "
            "END $$;"
        )
        cursor.execute("SELECT quote_literal(%s::text)", (_credential(),))
        literal = cursor.fetchone()[0]
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (WRITER_ROLE,))
        if cursor.fetchone() is None:
            cursor.execute(
                f"CREATE ROLE {WRITER_ROLE} LOGIN NOBYPASSRLS PASSWORD {literal}"
            )
        else:
            cursor.execute(f"ALTER ROLE {WRITER_ROLE} PASSWORD {literal}")
        for name in chain:
            cursor.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        cursor.execute(
            f"GRANT USAGE ON SCHEMA public TO {WRITER_ROLE};"
            f"GRANT SELECT, INSERT, UPDATE ON generation_events TO {WRITER_ROLE}"
        )


def _kernel_operations() -> Any:
    from tests.helpers.autowired_projection import autowired_operations

    return autowired_operations(
        node="node_projection_delegation",
        relation="generation_events",
        dsn=_writer_dsn(),
        principal=WRITER_ROLE,
        physical_database=_host()[2],
    )


def _terminal_event(correlation_id: str) -> Any:
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        ModelProjectionGenerationCompletedEvent,
    )

    return ModelProjectionGenerationCompletedEvent(
        correlation_id=correlation_id,
        task_description="omn18774 kernel-path probe",
        provider="zhipu",
        model_id="glm-4",
        endpoint_class="cloud",
        attempt_count=1,
        total_latency_e2e_ms=12,
        contract_passed=True,
        timestamp=datetime.now(tz=UTC).isoformat(),
        routing_source="contract",
        resolved_endpoint="http://omn18774:8000/v1/chat/completions",
    )


def _project(correlation_id: str) -> Any:
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        HandlerProjectionDelegation,
    )

    return HandlerProjectionDelegation().project_generation_completed(
        _terminal_event(correlation_id), _kernel_operations()
    )


class TestTheDefect:
    """The RED control: the row the pre-fix writer composed is still refused.

    Pinned as behaviour rather than described, so it keeps failing if anyone
    re-adds an unconditional stamp. The refusal is raised by the operation
    class before any SQL, so this half needs no table.
    """

    def test_generation_events_internal_domain_rejects_the_house_tenant_stamp(
        self, admin: Any
    ) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            GENERATION_TABLE,
        )
        from omnimarket.projection.tenant_isolation import house_tenant_write_stamp

        _apply(admin, CHAIN)
        operations = _kernel_operations()
        pre_fix_row = {
            "correlation_id": str(uuid4()),
            # The exact key OMN-16831 item 4 added to this handler's row dict.
            **house_tenant_write_stamp(table=GENERATION_TABLE),
        }

        with pytest.raises(ValueError, match="rejects canonical tenant_id"):
            operations.upsert(GENERATION_TABLE, "correlation_id", pre_fix_row)

    def test_the_declared_domain_is_the_one_that_selects_that_guard(self) -> None:
        """The refusal is contract-driven, not relation-name-driven."""
        from omnibase_core.enums.enum_database_schema_domain import (
            EnumDatabaseSchemaDomain,
        )

        from omnimarket.projection.relation_domains import declared_relation_domain

        assert (
            declared_relation_domain("generation_events")
            is EnumDatabaseSchemaDomain.OMNINODE_INTERNAL
        )


class TestTheKernelPathNowWrites:
    def test_generation_events_internal_domain_write_lands_a_row(
        self, admin: Any
    ) -> None:
        _apply(admin, CHAIN)
        correlation_id = str(uuid4())

        result = _project(correlation_id)

        assert result.rows_upserted == 1
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT provider, model_id FROM generation_events WHERE "
                "correlation_id = %s",
                (correlation_id,),
            )
            row = cursor.fetchone()
        assert row is not None, (
            "the sync handler must be able to write the relation its own "
            "contract declares -- this is the assertion that was false"
        )
        assert row == ("zhipu", "glm-4")

    def test_generation_events_internal_domain_write_was_refused_before_0043(
        self, admin: Any
    ) -> None:
        """The same call, against the pre-0043 relation, cannot land.

        Two independent refusals stand between this writer and the relation
        before this ticket, and the test asserts whichever fires first: the
        column still exists, so the handler's stamp (had it survived) is
        refused in Python; without the stamp the GUC-predicated policy refuses
        it in Postgres, because the internal operation binds no GUC.
        """
        _apply(admin, CHAIN_BEFORE)
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_policies WHERE tablename = "
                "'generation_events' AND policyname = 'tenant_isolation'"
            )
            assert cursor.fetchone()[0] == 1

        # The refusal can come from Postgres (policy) or from the operation
        # class (rejected key), and WHICH one is the thing under test, so
        # the type is deliberately not narrowed here; the assertion below
        # pins the message.
        with pytest.raises(Exception, match=r"(?s).") as caught:
            _project(str(uuid4()))
        assert "row-level security policy" in str(caught.value) or (
            "rejects canonical tenant_id" in str(caught.value)
        ), caught.value


class TestThePostureIsGone:
    def test_generation_events_internal_domain_carries_no_tenant_posture(
        self, admin: Any
    ) -> None:
        _apply(admin, CHAIN)
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'generation_events'"
            )
            assert cursor.fetchone() == (False, False)
            cursor.execute(
                "SELECT count(*) FROM pg_policies WHERE tablename = 'generation_events'"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns WHERE "
                "table_name = 'generation_events' AND column_name = 'tenant_id'"
            )
            assert cursor.fetchone()[0] == 0
