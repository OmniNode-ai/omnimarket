# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15472: the registration projection must supply ``service_url`` on CREATE.

``node_service_registry.service_url`` is ``NOT NULL``. ``project_heartbeat`` and
``project_state_change`` only ever carried the column through an ALREADY-EXISTING
row (``**base`` / the UPSERT's implicit "don't touch columns I didn't name"), so
against an EMPTY table the INSERT omitted the column entirely. On ``onex-dev``,
where the live column had drifted to ``NOT NULL`` with **no** ``DEFAULT`` (the
shipped migration declares ``DEFAULT ''``), that omission raised
``NotNullViolation`` on every single heartbeat and routed it to
``onex.dlq.omnimarket.projection-registration-malformed.v1`` — ~700/hour, flat,
with the registry stuck at zero rows. Self-locking: no row exists, so the
heartbeat cannot create one, so no row exists.

Test posture (ticket AC6): the RED-before must be demonstrated against the REAL
handler and the REAL drifted column shape. ``InmemoryDatabaseAdapter`` enforces
no constraints and the shipped migration carries ``DEFAULT ''`` — either one
alone would mask the defect. So the create-path cases run against a SQLite table
pre-created in the LIVE onex-dev shape (``service_url TEXT NOT NULL``, no
default) through the real ``SqliteDatabaseAdapter``, which is a genuine
constraint-enforcing store. ``test_drifted_fixture_rejects_insert_omitting_service_url``
proves the fixture is fail-closed, so these tests cannot silently go vacuous if
someone later relaxes the fixture DDL.

Ticket AC mapping:
  AC3 -> TestHeartbeatCreatePath (empty-table create + explicit-key seam + no-clobber)
  AC4 -> TestStateChangeCreatePath (same three cases for the state-change path)
  AC6 -> TestDriftedShapeFixture (fixture is fail-closed; migration parity)
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.primitives.model_semver import ModelSemVer

# Drive the PRODUCER's canonical heartbeat shape (omnibase_infra) through the
# consumer, exactly as the runtime does — not a consumer-local restatement.
from omnibase_infra.models.registration.model_node_heartbeat_event import (
    ModelNodeHeartbeatEvent,
)

from omnimarket.nodes.node_projection_registration.handlers.handler_projection_registration import (
    TABLE,
    HandlerProjectionRegistration,
    ModelNodeStateChangeEvent,
)
from omnimarket.projection.protocol_database import (
    DatabaseAdapter,
    InmemoryDatabaseAdapter,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

HANDLER = HandlerProjectionRegistration()

# The LIVE onex-dev shape read from information_schema.columns on 2026-07-30 and
# re-confirmed 2026-07-31: service_url is NOT NULL with NO default, while the
# shipped migration declares DEFAULT ''. Reproducing the DRIFTED shape (not the
# shipped one) is what makes the create-path omission observable at all.
_DRIFTED_REGISTRY_DDL = """
CREATE TABLE node_service_registry (
    service_name    TEXT NOT NULL UNIQUE,
    service_url     TEXT NOT NULL,
    health_status   TEXT NOT NULL DEFAULT 'unknown',
    uptime_seconds  BIGINT NOT NULL DEFAULT 0,
    tenant_id       TEXT NOT NULL DEFAULT 'omninode'
)
"""

_MIGRATION_0000 = Path(
    "src/omnimarket/nodes/node_projection_registration/migrations/"
    "0000_create_node_service_registry.sql"
)


def _drifted_registry_db(tmp_path: Path) -> SqliteDatabaseAdapter:
    """Create an EMPTY node_service_registry in the live (drifted) column shape.

    Empty is load-bearing: seeding a row first makes ``**base`` carry
    ``service_url`` through and the defect disappears (ticket AC3).
    """
    db_path = tmp_path / "registry.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_DRIFTED_REGISTRY_DDL)
        conn.commit()
    finally:
        conn.close()
    return SqliteDatabaseAdapter(db_path)


def _heartbeat(
    node_id: UUID, *, uptime_seconds: float = 0.0
) -> ModelNodeHeartbeatEvent:
    """Build the canonical heartbeat wire event the runtime actually publishes."""
    return ModelNodeHeartbeatEvent(
        node_id=node_id,
        node_type=EnumNodeKind.EFFECT,
        node_version=ModelSemVer(major=1, minor=0, patch=0),
        uptime_seconds=uptime_seconds,
        timestamp=datetime.now(tz=UTC),
    )


def _seed_existing_row(db: InmemoryDatabaseAdapter, *, node_id: str, url: str) -> None:
    """Seed a registry row as ``project_introspection`` would have written it."""
    db.tables[TABLE] = [
        {
            "service_name": "already-registered-node",
            "service_url": url,
            "service_type": "effect",
            "health_status": "RUNNING",
            "metadata": {"node_id": node_id},
            "is_active": True,
        }
    ]


def _written_row(db: DatabaseAdapter) -> dict[str, object]:
    rows = db.query(TABLE)
    assert len(rows) == 1, f"expected exactly one registry row, got {len(rows)}"
    return rows[0]


class TestHeartbeatCreatePath:
    """AC3: project_heartbeat must supply service_url on the create path."""

    def test_heartbeat_creates_row_on_empty_drifted_table(self, tmp_path: Path) -> None:
        """RED-before: NOT NULL constraint failed: node_service_registry.service_url.

        The real handler, the real adapter, the real drifted column shape, and an
        EMPTY table — i.e. the exact onex-dev situation in which every one of the
        ~8700 retained heartbeats died.
        """
        db = _drifted_registry_db(tmp_path)
        node_id = uuid4()

        result = HANDLER.project_heartbeat(_heartbeat(node_id), db)

        assert result.rows_upserted == 1
        row = _written_row(db)
        assert row["service_name"] == str(node_id)
        # Empty string is acceptable per the ticket; NULL/omission is not.
        assert row["service_url"] == ""
        assert row["health_status"] == "healthy"

    def test_heartbeat_create_row_names_service_url_column(self) -> None:
        """AC3 seam: the column must be NAMED in the write, not left to a default.

        A store-level assertion alone cannot distinguish "the handler supplied
        ''" from "the store defaulted it to ''". This drives the create path
        against the constraint-free in-memory adapter, which records the row dict
        verbatim, and asserts the KEY is present — the INSERT column list is the
        actual seam the NotNullViolation was raised on.
        """
        db = InmemoryDatabaseAdapter()

        HANDLER.project_heartbeat(_heartbeat(uuid4()), db)

        row = _written_row(db)
        assert "service_url" in row, (
            "project_heartbeat omitted service_url from the create-path row dict; "
            "the INSERT will omit the column and violate NOT NULL"
        )
        assert row["service_url"] == ""

    def test_heartbeat_does_not_clobber_existing_service_url(self) -> None:
        """The create-path default must never overwrite a registered URL."""
        db = InmemoryDatabaseAdapter()
        node_id = uuid4()
        _seed_existing_row(db, node_id=str(node_id), url="http://rich-svc:8080")

        HANDLER.project_heartbeat(_heartbeat(node_id, uptime_seconds=12.5), db)

        row = _written_row(db)
        assert row["service_url"] == "http://rich-svc:8080"
        assert row["service_name"] == "already-registered-node"
        assert row["uptime_seconds"] == 12


class TestStateChangeCreatePath:
    """AC4: project_state_change carried the identical omission and is reachable.

    ``onex.evt.platform.node-state-change.v1`` is a declared subscribe topic of
    this node's contract, and this path does no existing-row lookup at all, so a
    state change arriving against an empty registry INSERTs without service_url
    exactly as the heartbeat path did. It is fixed, not waived.
    """

    def test_state_change_creates_row_on_empty_drifted_table(
        self, tmp_path: Path
    ) -> None:
        db = _drifted_registry_db(tmp_path)

        result = HANDLER.project_state_change(
            ModelNodeStateChangeEvent(node_id="runtime-effect-001", new_state="active"),
            db,
        )

        assert result.rows_upserted == 1
        row = _written_row(db)
        assert row["service_name"] == "runtime-effect-001"
        assert row["service_url"] == ""
        assert row["health_status"] == "active"

    def test_state_change_create_row_names_service_url_column(self) -> None:
        db = InmemoryDatabaseAdapter()

        HANDLER.project_state_change(
            ModelNodeStateChangeEvent(node_id="runtime-effect-002", new_state="active"),
            db,
        )

        row = _written_row(db)
        assert "service_url" in row, (
            "project_state_change omitted service_url from the create-path row "
            "dict; the INSERT will omit the column and violate NOT NULL"
        )
        assert row["service_url"] == ""

    def test_state_change_does_not_clobber_existing_service_url(self) -> None:
        """A state change must not blank a URL introspection already registered."""
        db = InmemoryDatabaseAdapter()
        db.tables[TABLE] = [
            {
                "service_name": "registered-svc",
                "service_url": "http://registered-svc:9000",
                "health_status": "RUNNING",
                "is_active": True,
            }
        ]

        HANDLER.project_state_change(
            ModelNodeStateChangeEvent(service_name="registered-svc", new_state="idle"),
            db,
        )

        row = _written_row(db)
        assert row["service_url"] == "http://registered-svc:9000"
        assert row["health_status"] == "idle"
        assert row["is_active"] is False


class TestDriftedShapeFixture:
    """AC6: prove the RED harness is fail-closed and name the schema parity."""

    def test_drifted_fixture_rejects_insert_omitting_service_url(
        self, tmp_path: Path
    ) -> None:
        """The fixture must actually enforce NOT NULL, or every case above is vacuous.

        This is the anti-vacuous guard: if someone later relaxes the fixture DDL
        to carry the shipped ``DEFAULT ''``, the create-path tests would pass
        without the handler fix and this test fails instead.
        """
        db = _drifted_registry_db(tmp_path)

        with pytest.raises(sqlite3.IntegrityError, match="service_url"):
            db.upsert(TABLE, "service_name", {"service_name": "no-url-node"})

    def test_shipped_migration_declares_not_null_default_empty(self) -> None:
        """The shipped DDL keeps ``DEFAULT ''`` — the second half of the defect.

        Two independent faults produced the outage: this omission in the code AND
        the live column having lost the shipped default. Restoring the live column
        is the deploy-side leg; this asserts the code side never drops it.
        """
        ddl = _MIGRATION_0000.read_text()

        assert "service_url TEXT NOT NULL DEFAULT ''" in ddl
