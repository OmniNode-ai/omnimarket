# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13839 — skill-executions projection: fold + contract + projection_api.

Completes the skill-measurement pipeline:
    emit (OMN-13830) -> skill_executions table -> [this snapshot topic]
      -> omnidash skill-adoption widget (OMN-13832).

Before this node the projection API returned 404 unknown_topic for
onex.snapshot.projection.skill-executions.v1. These tests pin:

  1. the contract subscribes to the LIVE skill-lifecycle topics
     (onex.evt.omniclaude.skill-started.v1 / skill-completed.v1)
  2. the contract declares skill_execution_snapshots as a db_io write model and
     exposes it through projection_api on the widget's snapshot topic
  3. the row builder folds a started event into started_count and a completed
     event into completed_count + the status-breakdown counter, keyed on the
     canonical (skill_name, repo_id, window, snapshot_timestamp_minute) dimension
  4. absent skill/repo are keyed on the honest 'unknown' sentinel (never faked)
  5. the runner UPSERTs a snapshot row via self.db.execute (the live writer path)
     and accumulates counters on conflict
  6. receipt_coverage mirrors the DB generated column (completed/started, clamped)
  7. the node-owned migration creates the backing table with the widget columns
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from omnimarket.nodes.node_projection_skill_executions.handlers.row_skill_executions import (
    SKILL_EXECUTION_CONFLICT_COLUMNS,
    SKILL_EXECUTION_SNAPSHOTS_COLUMNS,
    UNKNOWN_REPO,
    UNKNOWN_SKILL,
    build_skill_executions_row,
    compute_receipt_coverage,
)
from omnimarket.projection.runner import MessageMeta

NODE_DIR = Path("src/omnimarket/nodes/node_projection_skill_executions")
CONTRACT_PATH = Path(__file__).parent.parent / NODE_DIR / "contract.yaml"

TOPIC_STARTED = "onex.evt.omniclaude.skill-started.v1"
TOPIC_COMPLETED = "onex.evt.omniclaude.skill-completed.v1"
PROJECTION_TOPIC = (
    "onex.snapshot.projection.skill-executions.v1"  # onex-topic-allow: snapshot prefix
)
TABLE = "skill_execution_snapshots"


def _contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT_PATH.read_text())


# ---------------------------------------------------------------------------
# 1. Contract subscribes to the LIVE skill-lifecycle topics
# ---------------------------------------------------------------------------


def test_contract_subscribes_live_skill_lifecycle_topics() -> None:
    subscribe = _contract()["event_bus"]["subscribe_topics"]
    assert subscribe == [TOPIC_STARTED, TOPIC_COMPLETED]


def test_contract_declares_write_model() -> None:
    tables = _contract()["db_io"]["db_tables"]
    write_tables = [t["name"] for t in tables if t.get("access") == "write"]
    assert write_tables == [TABLE]


# ---------------------------------------------------------------------------
# 2. projection_api exposes the widget's snapshot topic
# ---------------------------------------------------------------------------


def test_projection_api_block_declares_widget_topic() -> None:
    projection_api = _contract()["projection_api"]
    assert isinstance(projection_api, dict)
    assert projection_api["expose"] is True
    assert projection_api["topic"] == PROJECTION_TOPIC
    assert projection_api["table"] == TABLE
    assert projection_api["schema"] == "public"


def test_projection_api_columns_cover_widget_required_fields() -> None:
    columns = _contract()["projection_api"]["columns"]
    for required in (
        "skill_name",
        "repo_id",
        "started_count",
        "completed_count",
        "success_count",
        "failed_count",
        "partial_count",
        "receipt_coverage",
    ):
        assert required in columns, f"projection_api missing widget column {required}"


def test_projection_api_ordering_and_freshness_bound() -> None:
    projection_api = _contract()["projection_api"]
    assert projection_api["order_by"]
    assert projection_api["freshness_column"] in projection_api["columns"]
    assert projection_api["limit"] >= 1


# ---------------------------------------------------------------------------
# 3 + 4. Row builder folds lifecycle events into counter rows
# ---------------------------------------------------------------------------


def test_row_builder_folds_started_event() -> None:
    row = build_skill_executions_row(
        {
            "event_id": "e-1",
            "run_id": "r-1",
            "skill_name": "pr-review",
            "repo_id": "omniclaude",
            "correlation_id": "c-1",
            "emitted_at": "2026-06-30T12:34:56+00:00",
        },
        TOPIC_STARTED,
    )
    assert set(row) == set(SKILL_EXECUTION_SNAPSHOTS_COLUMNS)
    assert row["skill_name"] == "pr-review"
    assert row["repo_id"] == "omniclaude"
    assert row["window"] == "latest"
    assert row["started_count"] == 1
    assert row["completed_count"] == 0
    assert row["success_count"] == 0
    # snapshot_timestamp_minute is truncated to the minute.
    assert row["snapshot_timestamp_minute"].second == 0
    assert row["snapshot_timestamp_minute"].microsecond == 0


def test_row_builder_folds_completed_success_event() -> None:
    row = build_skill_executions_row(
        {
            "event_id": "e-2",
            "run_id": "r-1",
            "skill_name": "pr-review",
            "repo_id": "omniclaude",
            "correlation_id": "c-1",
            "status": "success",
            "duration_ms": 1200,
            "emitted_at": "2026-06-30T12:34:59+00:00",
        },
        TOPIC_COMPLETED,
    )
    assert row["started_count"] == 0
    assert row["completed_count"] == 1
    assert row["success_count"] == 1
    assert row["failed_count"] == 0
    assert row["partial_count"] == 0


def test_row_builder_folds_completed_failed_and_partial() -> None:
    failed = build_skill_executions_row(
        {"skill_name": "s", "repo_id": "r", "status": "failed"}, TOPIC_COMPLETED
    )
    assert failed["completed_count"] == 1
    assert failed["failed_count"] == 1
    assert failed["success_count"] == 0

    partial = build_skill_executions_row(
        {"skill_name": "s", "repo_id": "r", "status": "partial"}, TOPIC_COMPLETED
    )
    assert partial["completed_count"] == 1
    assert partial["partial_count"] == 1


def test_row_builder_classifies_by_explicit_event_type_over_topic() -> None:
    """event_type discriminator wins even when the topic is ambiguous/empty."""
    row = build_skill_executions_row(
        {"skill_name": "s", "repo_id": "r", "event_type": "started"}, topic=""
    )
    assert row["started_count"] == 1
    assert row["completed_count"] == 0


def test_row_builder_keys_unknown_when_skill_or_repo_absent() -> None:
    """Absent skill/repo -> honest 'unknown' sentinel; never fabricated."""
    row = build_skill_executions_row({"status": "success"}, TOPIC_COMPLETED)
    assert row["skill_name"] == UNKNOWN_SKILL
    assert row["repo_id"] == UNKNOWN_REPO


def test_conflict_columns_match_migration_unique_constraint() -> None:
    assert SKILL_EXECUTION_CONFLICT_COLUMNS == (
        "skill_name",
        "repo_id",
        "window",
        "snapshot_timestamp_minute",
    )


# ---------------------------------------------------------------------------
# 5. Runner UPSERTs and accumulates counters
# ---------------------------------------------------------------------------


def test_runner_upserts_snapshot_row_from_started_event() -> None:
    from omnimarket.nodes.node_projection_skill_executions.handlers.handler_skill_executions import (
        SkillExecutionsProjectionRunner,
    )

    captured: list[tuple[object, ...]] = []

    class _RecordingDB:
        async def execute(self, *args: object, **kwargs: object) -> None:
            captured.append(args)

    runner = SkillExecutionsProjectionRunner()
    runner._db = _RecordingDB()  # type: ignore[assignment]

    data: dict[str, object] = {
        "event_id": "e-1",
        "run_id": "r-1",
        "skill_name": "merge-sweep",
        "repo_id": "omnimarket",
        "correlation_id": "c-1",
        "emitted_at": "2026-06-30T09:15:30+00:00",
    }
    meta = MessageMeta(partition=0, offset=0, fallback_id="e-1")

    ok = asyncio.run(runner.project_event(TOPIC_STARTED, data, meta))
    assert ok is True
    assert len(captured) == 1

    sql = str(captured[0][0])
    assert f"INSERT INTO {TABLE}" in sql
    assert "ON CONFLICT" in sql
    # Accumulation on conflict is additive per counter.
    assert "started_count = skill_execution_snapshots.started_count" in sql
    assert "completed_count = skill_execution_snapshots.completed_count" in sql

    params = captured[0][1:]
    assert "merge-sweep" in params
    assert "omnimarket" in params
    # started_count param is 1, completed_count param is 0.
    assert params[4] == 1
    assert params[5] == 0


def test_runner_subscribe_topics_resolve_from_contract() -> None:
    from omnimarket.nodes.node_projection_skill_executions.handlers.handler_skill_executions import (
        SkillExecutionsProjectionRunner,
    )

    runner = SkillExecutionsProjectionRunner()
    assert runner.topics == [TOPIC_STARTED, TOPIC_COMPLETED]


# ---------------------------------------------------------------------------
# 6. receipt_coverage mirrors the DB generated column
# ---------------------------------------------------------------------------


def test_receipt_coverage_matches_db_formula() -> None:
    assert compute_receipt_coverage(0, 0) == 0.0
    assert compute_receipt_coverage(0, 3) == 0.0  # no evidence, not div-by-zero
    assert compute_receipt_coverage(4, 2) == 0.5
    assert compute_receipt_coverage(4, 4) == 1.0
    # Orphan completed events never push coverage above 1.0.
    assert compute_receipt_coverage(2, 5) == 1.0


# ---------------------------------------------------------------------------
# 7. Node-owned migration creates the backing table
# ---------------------------------------------------------------------------


def test_node_owned_migration_creates_backing_table() -> None:
    migration = (
        Path(__file__).parent.parent
        / NODE_DIR
        / "migrations"
        / "0001_create_skill_execution_snapshots.sql"
    )
    sql = migration.read_text()
    assert f"CREATE TABLE IF NOT EXISTS {TABLE}" in sql
    assert "skill_name" in sql
    assert "repo_id" in sql
    assert "started_count" in sql
    assert "completed_count" in sql
    assert "receipt_coverage" in sql
    assert "GENERATED ALWAYS AS" in sql


def test_event_topic_publish_terminal_declared() -> None:
    event_bus = _contract()["event_bus"]
    assert event_bus["publish_topics"] == [
        "onex.evt.omnimarket.skill-executions-snapshot.v1"
    ]
