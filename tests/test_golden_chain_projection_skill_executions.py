# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain tests for node_projection_skill_executions (OMN-13839).

Exercises the full live path the deployed runtime entrypoint runs:
    skill-lifecycle event -> SkillExecutionsProjectionRunner.project_event
      -> skill_execution_snapshots upsert (per-(skill,repo,window,minute) row).

Completes the skill-measurement pipeline (emit OMN-13830 -> skill_executions
table -> [this snapshot topic] -> skill-adoption widget OMN-13832).

The runner writes through the raw asyncpg ``self.db.execute`` path (the
node_projection_cost_by_repo writer pattern), so this chain drives a
``_FakeUpsertDB`` that interprets the INSERT ... ON CONFLICT DO UPDATE the runner
issues and accumulates the lifecycle counters additively — proving the same
arithmetic the Postgres unique key + generated ``receipt_coverage`` column apply
in production.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from omnimarket.nodes.node_projection_skill_executions.handlers.handler_skill_executions import (
    TABLE,
    SkillExecutionsProjectionRunner,
)
from omnimarket.nodes.node_projection_skill_executions.handlers.row_skill_executions import (
    compute_receipt_coverage,
)
from omnimarket.projection.runner import MessageMeta

TOPIC_STARTED = "onex.evt.omniclaude.skill-started.v1"
TOPIC_COMPLETED = "onex.evt.omniclaude.skill-completed.v1"

_COUNTER_COLUMNS = (
    "started_count",
    "completed_count",
    "success_count",
    "failed_count",
    "partial_count",
)


class _FakeUpsertDB:
    """Minimal asyncpg-shaped stand-in that applies the runner's upsert.

    Keyed on (skill_name, repo_id, window, snapshot_timestamp_minute), it mirrors
    the additive DO UPDATE and computes receipt_coverage exactly like the DB
    generated column.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[Any, ...], dict[str, Any]] = {}

    async def execute(self, sql: str, *params: Any) -> None:
        assert f"INSERT INTO {TABLE}" in sql
        assert "ON CONFLICT" in sql
        assert "DO UPDATE SET" in sql
        # Each additive counter must be present in the DO UPDATE clause.
        for col in _COUNTER_COLUMNS:
            assert re.search(rf"{col}\s*=\s*{TABLE}\.{col}\s*\+\s*EXCLUDED\.{col}", sql)

        (
            skill_name,
            repo_id,
            window,
            minute,
            started,
            completed,
            success,
            failed,
            partial,
        ) = params
        key = (skill_name, repo_id, window, minute)
        row = self.rows.get(key)
        if row is None:
            self.rows[key] = {
                "skill_name": skill_name,
                "repo_id": repo_id,
                "window": window,
                "snapshot_timestamp_minute": minute,
                "started_count": started,
                "completed_count": completed,
                "success_count": success,
                "failed_count": failed,
                "partial_count": partial,
            }
        else:
            row["started_count"] += started
            row["completed_count"] += completed
            row["success_count"] += success
            row["failed_count"] += failed
            row["partial_count"] += partial

    def receipt_coverage(self, key: tuple[Any, ...]) -> float:
        row = self.rows[key]
        return compute_receipt_coverage(row["started_count"], row["completed_count"])


def _runner_with_fake_db() -> tuple[SkillExecutionsProjectionRunner, _FakeUpsertDB]:
    runner = SkillExecutionsProjectionRunner()
    db = _FakeUpsertDB()
    runner._db = db  # type: ignore[assignment]
    return runner, db


def _project(
    runner: SkillExecutionsProjectionRunner, topic: str, data: dict[str, Any]
) -> None:
    meta = MessageMeta(partition=0, offset=0, fallback_id=str(data.get("event_id", "")))
    ok = asyncio.run(runner.project_event(topic, data, meta))
    assert ok is True


class TestSkillExecutionsGoldenChain:
    def test_started_then_completed_pair_accumulates(self) -> None:
        runner, db = _runner_with_fake_db()
        base = {
            "run_id": "r-1",
            "skill_name": "pr-review",
            "repo_id": "omniclaude",
            "correlation_id": "c-1",
            "emitted_at": "2026-06-30T12:00:10+00:00",
        }
        _project(runner, TOPIC_STARTED, {**base, "event_id": "e-start"})
        _project(
            runner,
            TOPIC_COMPLETED,
            {**base, "event_id": "e-done", "status": "success"},
        )

        assert len(db.rows) == 1
        key = ("pr-review", "omniclaude", "latest", next(iter(db.rows))[3])
        row = db.rows[key]
        assert row["started_count"] == 1
        assert row["completed_count"] == 1
        assert row["success_count"] == 1
        # Full receipt coverage: every started skill produced a completed receipt.
        assert db.receipt_coverage(key) == 1.0

    def test_status_breakdown_accumulates_across_events(self) -> None:
        runner, db = _runner_with_fake_db()
        minute = "2026-06-30T08:30:00+00:00"
        for status in ("success", "success", "failed", "partial"):
            _project(
                runner,
                TOPIC_COMPLETED,
                {
                    "run_id": "r",
                    "skill_name": "merge-sweep",
                    "repo_id": "omnimarket",
                    "correlation_id": "c",
                    "status": status,
                    "emitted_at": minute,
                },
            )
        (row,) = list(db.rows.values())
        assert row["completed_count"] == 4
        assert row["success_count"] == 2
        assert row["failed_count"] == 1
        assert row["partial_count"] == 1

    def test_partial_receipt_coverage_when_started_exceeds_completed(self) -> None:
        runner, db = _runner_with_fake_db()
        minute = "2026-06-30T09:00:00+00:00"
        base = {
            "run_id": "r",
            "skill_name": "ci-watch",
            "repo_id": "omnimarket",
            "correlation_id": "c",
            "emitted_at": minute,
        }
        # 4 starts, only 2 completions => coverage 0.5.
        for _ in range(4):
            _project(runner, TOPIC_STARTED, dict(base))
        for _ in range(2):
            _project(runner, TOPIC_COMPLETED, {**base, "status": "success"})

        key = next(iter(db.rows))
        assert db.rows[key]["started_count"] == 4
        assert db.rows[key]["completed_count"] == 2
        assert db.receipt_coverage(key) == 0.5

    def test_distinct_skills_and_repos_do_not_collide(self) -> None:
        runner, db = _runner_with_fake_db()
        minute = "2026-06-30T10:00:00+00:00"
        _project(
            runner,
            TOPIC_STARTED,
            {"skill_name": "a", "repo_id": "r1", "emitted_at": minute},
        )
        _project(
            runner,
            TOPIC_STARTED,
            {"skill_name": "a", "repo_id": "r2", "emitted_at": minute},
        )
        _project(
            runner,
            TOPIC_STARTED,
            {"skill_name": "b", "repo_id": "r1", "emitted_at": minute},
        )
        assert len(db.rows) == 3

    def test_event_bus_wiring(self) -> None:
        runner, _ = _runner_with_fake_db()
        assert runner.topics == [TOPIC_STARTED, TOPIC_COMPLETED]
