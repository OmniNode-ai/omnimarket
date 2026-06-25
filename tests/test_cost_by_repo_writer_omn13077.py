# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13077 — cost.by_repo projection WRITER (Wave-5: node had no writer at all).

The Wave-5 diagnosis found node_projection_cost_by_repo declared a
projection_api over cost_by_repo_snapshots but had NO materialization path —
the handler returned a pure dict and nothing persisted, so the table stayed
empty (HWM=0 on the dead onex.evt.omniintelligence.llm-call-completed.v1 topic).

These tests are written BEFORE the implementation (TDD). They pin:

  1. the contract re-subscribes to the LIVE metered-cost topic
     onex.evt.omnibase-infra.delegation-completed.v1 (was the dead
     llm-call-completed.v1)
  2. the contract declares cost_by_repo_snapshots as a db_io write model
  3. the row builder maps a delegation-completed event to a snapshot row
     keyed on the canonical (repo_name, window, snapshot_timestamp_minute)
     dimension, reading the metered cost (cost_usd) and tokens
  4. when the upstream event carries no repo, the row is keyed on the
     honest "unknown" sentinel (NOT a fabricated repo) — the documented
     Wave-5 upstream gap
  5. the runner UPSERTs a snapshot row into cost_by_repo_snapshots from a
     delegation-completed event via self.db.execute (the live writer path)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from omnimarket.nodes.node_projection_cost_by_repo.handlers.row_cost_by_repo import (
    COST_BY_REPO_CONFLICT_COLUMNS,
    COST_BY_REPO_SNAPSHOTS_COLUMNS,
    UNKNOWN_REPO,
    build_cost_by_repo_row,
)
from omnimarket.projection.runner import MessageMeta

CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_cost_by_repo/contract.yaml"
)
LIVE_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
DEAD_TOPIC = "onex.evt.omniintelligence.llm-call-completed.v1"
TABLE = "cost_by_repo_snapshots"


def _contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT_PATH.read_text())


# ---------------------------------------------------------------------------
# 1. Contract re-subscribes to the LIVE metered-cost topic
# ---------------------------------------------------------------------------


def test_contract_subscribes_live_delegation_completed_topic() -> None:
    subscribe = _contract()["event_bus"]["subscribe_topics"]
    assert LIVE_TOPIC in subscribe, (
        f"contract must subscribe to the LIVE metered-cost topic {LIVE_TOPIC!r}; "
        "the previous llm-call-completed.v1 source was dead (HWM=0)"
    )


def test_contract_no_longer_subscribes_dead_topic() -> None:
    subscribe = _contract()["event_bus"]["subscribe_topics"]
    assert DEAD_TOPIC not in subscribe, (
        f"contract must NOT subscribe to the dead topic {DEAD_TOPIC!r} (HWM=0)"
    )


# ---------------------------------------------------------------------------
# 2. Contract declares the write model
# ---------------------------------------------------------------------------


def test_contract_declares_cost_by_repo_snapshots_write_model() -> None:
    tables = _contract()["db_io"]["db_tables"]
    write_tables = [t["name"] for t in tables if t.get("access") == "write"]
    assert write_tables == [TABLE]


# ---------------------------------------------------------------------------
# 3 + 4. Row builder maps a delegation-completed event to a snapshot row
# ---------------------------------------------------------------------------


def test_row_builder_maps_metered_cost_and_tokens() -> None:
    row = build_cost_by_repo_row(
        {
            "correlation_id": "c-1",
            "repo": "omnimarket",
            "cost_usd": 0.042,
            "tokens_input": 1000,
            "tokens_output": 500,
            "window": "latest",
            "timestamp": "2026-06-25T12:34:56+00:00",
        }
    )
    assert set(row) == set(COST_BY_REPO_SNAPSHOTS_COLUMNS)
    assert row["repo_name"] == "omnimarket"
    assert float(row["total_cost_usd"]) == 0.042
    assert row["total_tokens"] == 1500
    assert row["window"] == "latest"
    # snapshot_timestamp_minute is truncated to the minute.
    assert row["snapshot_timestamp_minute"].second == 0
    assert row["snapshot_timestamp_minute"].microsecond == 0


def test_row_builder_reads_canonical_terminal_cost_field() -> None:
    """Canonical delegation terminal nests cost under final_attempt_cost / usage."""
    row = build_cost_by_repo_row(
        {
            "correlation_id": "c-2",
            "repo": "omnibase_core",
            "final_attempt_cost": 0.10,
            "usage": {"prompt_tokens": 200, "completion_tokens": 50},
            "window": "latest",
        }
    )
    assert row["repo_name"] == "omnibase_core"
    assert float(row["total_cost_usd"]) == 0.10
    assert row["total_tokens"] == 250


def test_row_builder_keys_unknown_when_repo_absent() -> None:
    """Wave-5: delegation-completed carries no repo today -> honest 'unknown'.

    We must NOT fabricate a repo value. The aggregation is keyed on the
    canonical dimension that IS available; repo defaults to the documented
    'unknown' sentinel until the upstream emitter populates it.
    """
    row = build_cost_by_repo_row(
        {
            "correlation_id": "c-3",
            "cost_usd": 0.01,
            "tokens_input": 10,
            "tokens_output": 5,
        }
    )
    assert row["repo_name"] == UNKNOWN_REPO


def test_conflict_columns_match_migration_unique_constraint() -> None:
    assert COST_BY_REPO_CONFLICT_COLUMNS == (
        "repo_name",
        "window",
        "snapshot_timestamp_minute",
    )


# ---------------------------------------------------------------------------
# 5. Runner UPSERTs a snapshot row from a delegation-completed event
# ---------------------------------------------------------------------------


def test_runner_upserts_snapshot_row_from_delegation_completed_event() -> None:
    from omnimarket.nodes.node_projection_cost_by_repo.handlers.handler_cost_by_repo import (
        CostByRepoProjectionRunner,
    )

    captured: list[tuple[object, ...]] = []

    class _RecordingDB:
        async def execute(self, *args: object, **kwargs: object) -> None:
            captured.append(args)

    runner = CostByRepoProjectionRunner()
    runner._db = _RecordingDB()  # type: ignore[assignment]

    data: dict[str, object] = {
        "correlation_id": "del-001",
        "repo": "omnimarket",
        "task_type": "code_review",
        "delegated_to": "glm-4.6",
        "cost_usd": 0.0123,
        "tokens_input": 800,
        "tokens_output": 200,
        "window": "latest",
        "timestamp": "2026-06-25T09:15:30+00:00",
    }
    meta = MessageMeta(partition=0, offset=0, fallback_id="del-001")

    ok = asyncio.run(runner.project_event(LIVE_TOPIC, data, meta))
    assert ok is True, "project_event must return True after a successful upsert"
    assert len(captured) == 1, "exactly one upsert must be issued"

    sql = str(captured[0][0])
    assert f"INSERT INTO {TABLE}" in sql
    assert "ON CONFLICT" in sql
    # The bound params must carry the repo dimension + metered cost.
    params = captured[0][1:]
    assert "omnimarket" in params
    assert any(float(p) == 0.0123 for p in params if _is_number(p))
    assert 1000 in params  # total tokens (800 + 200)


def test_runner_subscribe_topics_resolve_from_contract() -> None:
    from omnimarket.nodes.node_projection_cost_by_repo.handlers.handler_cost_by_repo import (
        CostByRepoProjectionRunner,
    )

    runner = CostByRepoProjectionRunner()
    assert runner.topics == [LIVE_TOPIC]


def _is_number(value: object) -> bool:
    try:
        float(value)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False
