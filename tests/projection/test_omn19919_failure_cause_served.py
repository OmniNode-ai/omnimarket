# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed run's typed cause survives the local write and dashboard read.

Failures to catch: the writer stores the cause but the decisions exposure does
not declare it, so the dashboard's Runs list sees no cause; the per-row
snapshot (which carries only declared columns) drops it the same way; one
cause is served for every failed run; a run with no cause reads a value
instead of null; and a fresh local store lacks the column, which makes the read
node refuse the whole exposure. The write goes through the real writer into the
real SQLite store and the read through the real dashboard app.
"""

from __future__ import annotations

import json
import sqlite3
from functools import cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_publisher import ModelSnapshotDeltaMessage
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

TOPIC = "onex.snapshot.projection.delegation.decisions.v1"
TENANT_SLUG = "failure-cause-tenant"
TENANT = "00000000-0000-4000-8000-000000019919"
FAILED_ID = "00000000-0000-4000-8000-000000000001"
CONTROL_ID = "00000000-0000-4000-8000-000000000002"


class RecordingPublisher:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        if message.topic == TOPIC:
            assert message.value is not None
            row = json.loads(message.value)["row"]
            self.rows[str(row["correlation_id"])] = row
        return True


def store(tmp_path: Path) -> SqliteDatabaseAdapter:
    path = tmp_path / "runs.sqlite"
    with sqlite3.connect(path) as conn:
        # The local-deployment registry relation, which the delegation adapter
        # reads but does not create (same shape as the existing serving tests).
        conn.execute(
            "CREATE TABLE tenant_registry_mirror (tenant_slug TEXT NOT NULL UNIQUE, "
            "tenant_uuid TEXT NOT NULL, display_name TEXT, status TEXT NOT NULL, "
            "registry_created_at TEXT, observed_at TEXT NOT NULL, source_event_id TEXT)"
        )
        conn.execute(
            "INSERT INTO tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, status, observed_at) "
            "VALUES (?, ?, 'active', '2026-10-21T10:00:00+00:00')",
            (TENANT_SLUG, TENANT),
        )
    return SqliteDatabaseAdapter(path)


def terminal(
    correlation_id: str, **changes: object
) -> ModelDelegateSkillTerminalProjection:
    payload: dict[str, object] = {
        "tenant_id": TENANT_SLUG,
        "status": "completed",
        "correlation_id": correlation_id,
        "task_type": "document",
        "provider": "local-coder",
        "model_name": "fixture-local-model",
        "response": "hello",
        "quality_gate_passed": True,
        "timestamp": "2026-10-21T10:00:00+00:00",
    }
    payload.update(changes)
    return ModelDelegateSkillTerminalProjection.from_payload(payload)


@cache
def topic_map() -> dict[str, ProjectionTableConfig]:
    # Discovery reads every node contract; its result does not depend on a
    # test's store, so it is built once per module.
    return build_projection_topic_map()


def served(tmp_path: Path) -> dict[str, dict[str, object]]:
    topics = topic_map()
    client = TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(
                topic_map=topics,
                row_source=SqliteTableRowSource(tmp_path / "runs.sqlite"),
            ),
            tenant=TENANT,
            topic_map=topics,
        )
    )
    response = client.get(f"/projection/{TOPIC}?tenant={TENANT}")
    assert response.status_code == 200, response.text
    return {str(row["correlation_id"]): row for row in response.json()["rows"]}


@pytest.mark.parametrize("cause", ["provider_error", "timeout"])
def test_failed_run_serves_its_typed_cause_and_a_passing_run_serves_null(
    tmp_path: Path, cause: str
) -> None:
    db = store(tmp_path)
    publisher = RecordingPublisher()
    handler = HandlerProjectionDelegation(publisher=publisher)
    handler.project_delegate_skill_terminal(
        terminal(
            FAILED_ID,
            status="failed",
            provider="",
            model_name="",
            response="",
            quality_gate_passed=False,
            terminal_failure_cause=cause,
            error_message="fixture refusal before any rung was dispatched",
        ),
        db,
    )
    handler.project_delegate_skill_terminal(terminal(CONTROL_ID), db)

    rows = served(tmp_path)
    assert set(rows) == {FAILED_ID, CONTROL_ID}
    failed, control = rows[FAILED_ID], rows[CONTROL_ID]
    # The served row: the page's Runs list and its cause filter read this.
    assert failed["terminal_ok"] in (False, 0)
    assert failed["terminal_failure_cause"] == cause
    assert control["terminal_ok"] in (True, 1)
    assert "terminal_failure_cause" in control
    assert control["terminal_failure_cause"] is None
    # The per-row snapshot carries only declared columns, so it must carry the
    # cause too, or a lane's projection API serves the row without it.
    assert publisher.rows[FAILED_ID]["terminal_failure_cause"] == cause
    assert publisher.rows[CONTROL_ID]["terminal_failure_cause"] is None


def test_a_fresh_store_serves_the_cause_column_before_any_failure(
    tmp_path: Path,
) -> None:
    # Only passing runs are written, so no row ever names the cause column;
    # the store must still have it, or the exposure is refused outright.
    db = store(tmp_path)
    HandlerProjectionDelegation(
        publisher=RecordingPublisher()
    ).project_delegate_skill_terminal(terminal(CONTROL_ID), db)
    row = served(tmp_path)[CONTROL_ID]
    assert "terminal_failure_cause" in row
    assert row["terminal_failure_cause"] is None
