# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17228: real-Postgres proof for the SYNC quality-gate write path on a lane
whose ``delegation_events`` DEFAULTs went missing.

WHY THIS FILE EXISTS ALONGSIDE
``tests/test_omn17228_real_postgres_drifted_default_write_path.py``. That module
proves the identical property for ``DelegationProjectionRunner`` -- the ASYNC
writer in ``handler_delegation.py``, driven through ``BaseProjectionRunner``.
The writer DEPLOYED as ``omnimarket-projection-delegation-writer`` on onex-dev is
the OTHER one: ``HandlerProjectionDelegation`` in
``handler_projection_delegation.py``, dispatched synchronously by the
omnibase_infra runtime's auto-wiring. The OMN-17228 fix went to the async twin
alone, both async modules went green, and the deployed path kept refusing.

Live traceback from the running pod, onex-dev (DEV-SYSTEM
``i-06169517a92b45f86``), 2026-09-15T16:43Z::

    File ".../handler_projection_delegation.py", line 1195, in
      project_quality_gate_result
    File ".../handler_projection_delegation.py", line 525, in
      _write_delegation_row
    psycopg2.errors.NotNullViolation: null value in column "task_type" of
      relation "delegation_events" violates not-null constraint

WHY A REAL DATABASE. Postgres evaluates NOT NULL against the PROPOSED insert row
BEFORE the conflict is resolved, so a targeted-column UPSERT that omits a
no-default NOT NULL column is refused outright even when it was only ever going
to take the DO UPDATE arm. No in-memory or mock adapter enforces that, which is
why every mock-DB test in this repo passed while the live writer wedged.

WHY THE HARNESS IS IMPORTED RATHER THAN COPIED.
``_SyncAsyncpgAdapter`` in ``test_omn16804_registry_resolved_write_tenant_real_postgres``
is the only sync ``ProtocolProjectionDatabaseSync`` over a real connection in
this repo, and it composes its statement through the shared
``build_upsert_plan`` so it cannot drift from the production adapters on arm
placement. A second copy here would be free to drift from it, and "two writers
nobody compared" is the exact defect this file is about. ``tests`` is a package
(``tests/__init__.py``), so the import is a real one, not a path hack.

THE DRIFT IS APPLIED DELIBERATELY. ``0007_delegation_events.sql`` declares
``task_type``/``delegated_to``/``timestamp`` with a DEFAULT, so a schema built by
applying the migrations to an empty database HAS them and the defect is
unreachable on it. onex-dev does not: its OMN-15376 reconciliation block
re-declares each as ``ADD COLUMN IF NOT EXISTS ... DEFAULT ''``, which no-ops on
a pre-existing column. Dropping the defaults is the only faithful reproduction,
and ``TestTheDriftedSyncFixtureIsFaithful`` asserts the fixture really is in that
state before any behaviour is asserted on it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
    ModelProjectionTaskDelegatedEvent,
)
from tests.test_omn16804_registry_resolved_write_tenant_real_postgres import (
    _APP_DASHBOARD_ROLE_SQL,
    _CONVERT_TENANT_ID_TO_UUID,
    _MIRROR_DDL,
    _dsn,
    _live_migration_files,
    _SyncAsyncpgAdapter,
    _test_schema_safe_sql,
)

#: The onex-dev drift, reproduced. Measured there as NOT NULL with
#: ``column_default = NULL`` while a fresh migrated schema gives each a default.
_COLUMNS_WITH_DROPPED_DEFAULT = ("task_type", "delegated_to", "timestamp")

#: The real-DSN environment this module connects through, named HERE rather
#: than left implicit in the imported ``_dsn`` helper. A reader opening this
#: file has to be able to see what to set to make it run, and the fixture
#: checks it explicitly below so an absent database SKIPS with a message that
#: names the variable instead of failing on a connection refusal.
_REAL_DB_PASSWORD_ENV = "INTEGRATION_POSTGRES_PASSWORD"

_TENANT_SLUG = "omninode"
_TENANT_UUID = "820272f9-4aaf-5add-a2df-0af942852ab2"
_EVENT_TIMESTAMP = datetime(2026, 9, 15, 16, 7, 1, tzinfo=UTC)


class _RecordingPublisher:
    """Captures deltas instead of reaching a broker."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def publish(self, message: Any) -> bool:
        self.messages.append(message)
        return True


def _handler() -> HandlerProjectionDelegation:
    return HandlerProjectionDelegation(publisher=_RecordingPublisher())


def _verdict(correlation_id: str) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=UUID(correlation_id),
        passed=True,
        quality_score=1.0,
        actual_score=1.0,
    )


def _terminal(correlation_id: str) -> ModelProjectionTaskDelegatedEvent:
    return ModelProjectionTaskDelegatedEvent(
        correlation_id=correlation_id,
        tenant_id=_TENANT_UUID,
        task_type="code-review",
        delegated_to="node_delegate_skill_orchestrator",
        model_name="qwen3.8",
    )


@pytest.fixture
def drifted_sync_adapter() -> Iterator[tuple[_SyncAsyncpgAdapter, Any]]:
    """The live migrated schema, the mirror, then the onex-dev drift on top."""
    if not os.environ.get(
        _REAL_DB_PASSWORD_ENV, os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip(
            f"{_REAL_DB_PASSWORD_ENV} not set -- skipping the OMN-17228 sync "
            "quality-gate write-path proof; a mock adapter cannot enforce "
            "NOT NULL and so cannot observe this defect at all"
        )
    dsn = _dsn()
    loop = asyncio.new_event_loop()
    conn: asyncpg.Connection | None = None
    try:
        try:
            conn = loop.run_until_complete(asyncpg.connect(dsn))
        except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
            pytest.skip(f"no reachable Postgres for the OMN-17228 sync proof: {exc}")
        assert conn is not None
        schema = f"omn17228s_{uuid4().hex[:12]}"
        try:
            loop.run_until_complete(conn.execute(f"CREATE SCHEMA {schema}"))
            loop.run_until_complete(
                conn.execute(f"SET search_path TO {schema}, public")
            )
            loop.run_until_complete(conn.execute(_APP_DASHBOARD_ROLE_SQL))
            for path in _live_migration_files():
                loop.run_until_complete(
                    conn.execute(
                        _test_schema_safe_sql(path.read_text(encoding="utf-8"))
                    )
                )
            loop.run_until_complete(conn.execute(_MIRROR_DDL))
            loop.run_until_complete(conn.execute(_CONVERT_TENANT_ID_TO_UUID))
            # The writer resolves its write tenant through the mirror and
            # refuses an identity nobody recorded (OMN-16804/OMN-16831), so the
            # fixture holds the row the deployed lane holds. Seeding it keeps
            # this module's subject the NOT NULL defect rather than tenant
            # resolution, which has its own suites.
            loop.run_until_complete(
                conn.execute(
                    "INSERT INTO tenant_registry_mirror "
                    "(tenant_slug, tenant_uuid, status) "
                    "VALUES ($1, $2::uuid, 'active') "
                    "ON CONFLICT (tenant_slug) DO NOTHING",
                    _TENANT_SLUG,
                    _TENANT_UUID,
                )
            )
            for column in _COLUMNS_WITH_DROPPED_DEFAULT:
                loop.run_until_complete(
                    conn.execute(
                        f"ALTER TABLE {TABLE} ALTER COLUMN {column} DROP DEFAULT"
                    )
                )
            yield _SyncAsyncpgAdapter(loop, conn), (loop, conn, schema)
        finally:
            loop.run_until_complete(
                conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            )
            loop.run_until_complete(conn.close())
    finally:
        loop.close()


@pytest.mark.integration
class TestTheDriftedSyncFixtureIsFaithful:
    """Positive control. Without this, every assertion below could pass for the
    wrong reason on a schema that still carries the defaults."""

    def test_the_defaults_really_are_gone(
        self, drifted_sync_adapter: tuple[_SyncAsyncpgAdapter, Any]
    ) -> None:
        _, (loop, conn, _schema) = drifted_sync_adapter
        rows = loop.run_until_complete(
            conn.fetch(
                "SELECT column_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = ANY($2::text[])",
                TABLE,
                list(_COLUMNS_WITH_DROPPED_DEFAULT),
            )
        )
        assert {r["column_name"] for r in rows} == set(_COLUMNS_WITH_DROPPED_DEFAULT)
        for row in rows:
            assert row["is_nullable"] == "NO"
            assert row["column_default"] is None

    def test_an_omitting_insert_is_refused_by_real_postgres(
        self, drifted_sync_adapter: tuple[_SyncAsyncpgAdapter, Any]
    ) -> None:
        """The constraint itself, proven against this connection.

        This is what the deployed writer hit. If this ever stops raising, the
        fixture is no longer the onex-dev lane and the module proves nothing.
        """
        _, (loop, conn, _schema) = drifted_sync_adapter
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            loop.run_until_complete(
                conn.execute(
                    f"INSERT INTO {TABLE} (correlation_id, tenant_id) "
                    "VALUES ($1, $2::uuid)",
                    str(uuid4()),
                    _TENANT_UUID,
                )
            )


@pytest.mark.integration
class TestTheDeployedVerdictLandsOnTheDriftedLane:
    def test_a_verdict_with_no_prior_row_writes(
        self, drifted_sync_adapter: tuple[_SyncAsyncpgAdapter, Any]
    ) -> None:
        adapter, (loop, conn, _schema) = drifted_sync_adapter
        correlation_id = str(uuid4())
        result = _handler().project_quality_gate_result(
            _verdict(correlation_id),
            adapter,
            tenant_identity=_TENANT_UUID,
            event_timestamp=_EVENT_TIMESTAMP,
        )
        assert result.rows_upserted == 1
        row = loop.run_until_complete(
            conn.fetchrow(
                f"SELECT task_type, delegated_to, timestamp, quality_gate_passed "
                f"FROM {TABLE} WHERE correlation_id = $1",
                correlation_id,
            )
        )
        assert row is not None
        assert row["task_type"] == ""
        assert row["delegated_to"] == ""
        assert row["timestamp"] == _EVENT_TIMESTAMP
        assert row["quality_gate_passed"] is True

    def test_a_verdict_after_its_terminal_never_erases_the_recorded_values(
        self, drifted_sync_adapter: tuple[_SyncAsyncpgAdapter, Any]
    ) -> None:
        """The other half of the fix: the placeholders are insert-only.

        Naming them makes the INSERT arm valid; keeping them out of DO UPDATE
        SET is what stops a late verdict from blanking a real task type. Both
        halves are needed and each can be broken without the other failing.
        """
        adapter, (loop, conn, _schema) = drifted_sync_adapter
        correlation_id = str(uuid4())
        _handler().project(_terminal(correlation_id), adapter)
        _handler().project_quality_gate_result(
            _verdict(correlation_id),
            adapter,
            tenant_identity=_TENANT_UUID,
            event_timestamp=_EVENT_TIMESTAMP,
        )
        row = loop.run_until_complete(
            conn.fetchrow(
                f"SELECT task_type, delegated_to, quality_gate_passed "
                f"FROM {TABLE} WHERE correlation_id = $1",
                correlation_id,
            )
        )
        assert row is not None
        assert row["task_type"] == "code-review"
        assert row["delegated_to"] == "node_delegate_skill_orchestrator"
        # Negative control on the insert-only set: the verdict's OWN fact must
        # still have been applied, or "insert-only" has simply widened to
        # everything and made every verdict after the first a no-op.
        assert row["quality_gate_passed"] is True

    def test_a_later_terminal_overwrites_the_placeholders(
        self, drifted_sync_adapter: tuple[_SyncAsyncpgAdapter, Any]
    ) -> None:
        """Verdict first, terminal second -- the ordering the wedge produced.

        The terminal event is the authority on both columns, so it must not be
        held insert-only anywhere.
        """
        adapter, (loop, conn, _schema) = drifted_sync_adapter
        correlation_id = str(uuid4())
        _handler().project_quality_gate_result(
            _verdict(correlation_id),
            adapter,
            tenant_identity=_TENANT_UUID,
            event_timestamp=_EVENT_TIMESTAMP,
        )
        _handler().project(_terminal(correlation_id), adapter)
        row = loop.run_until_complete(
            conn.fetchrow(
                f"SELECT task_type, delegated_to FROM {TABLE} "
                "WHERE correlation_id = $1",
                correlation_id,
            )
        )
        assert row is not None
        assert row["task_type"] == "code-review"
        assert row["delegated_to"] == "node_delegate_skill_orchestrator"
