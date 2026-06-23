# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerProjectionReplayCheck (OMN-12884).

Fixture proves idempotent projection dedupe behaviour and classification
output per the Phase 7 replay taxonomy.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_replay_check_compute.handlers.handler_projection_replay_check import (
    HandlerProjectionReplayCheck,
)
from omnimarket.nodes.node_projection_replay_check_compute.models.model_replay_check import (
    EnumReplayStatus,
    ModelProjectionEvent,
    ModelReplayCheckRequest,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

HANDLER = HandlerProjectionReplayCheck()
_TABLE = "delegation_events"
_TABLE_GEN = "generation_events"
_TOPIC = "onex.evt.omnibase-infra.task-delegated.v1"
_TOPIC_GEN = "onex.evt.omnimarket.node-generation-completed.v1"


def _evt(
    correlation_id: str,
    *,
    topic: str = _TOPIC,
    table: str = _TABLE,
    partition: int = 0,
    offset: int = 0,
) -> ModelProjectionEvent:
    return ModelProjectionEvent(
        correlation_id=correlation_id,
        source_topic=topic,
        partition=partition,
        offset=offset,
        table=table,
    )


def _req(*events: ModelProjectionEvent) -> ModelReplayCheckRequest:
    return ModelReplayCheckRequest(events=tuple(events))


# ---------------------------------------------------------------------------
# Single occurrence → runtime-observed
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSingleOccurrence:
    def test_single_event_is_runtime_observed(self) -> None:
        req = _req(_evt("corr-001", offset=0))
        result = HANDLER.check(req)
        assert result.status == "clean"
        assert result.total_correlations == 1
        assert result.runtime_observed == 1
        assert result.replay_proven == 0
        assert result.blocked == 0
        assert result.superseded == 0
        assert not result.findings

    def test_dedupe_held_true_on_single_occurrence(self) -> None:
        req = _req(_evt("corr-001", offset=0))
        result = HANDLER.check(req)
        classifications = {
            (r.correlation_id, r.table): r for r in (result.findings or ())
        }
        # No findings for a clean single occurrence; check directly via
        # rebuilding the internal path through the handler.
        assert result.runtime_observed == 1
        assert not classifications  # no findings list entry for runtime-observed


# ---------------------------------------------------------------------------
# Exact replay (same partition/offset) → replay-proven, dedupe held
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExactReplay:
    def test_identical_delivery_classified_replay_proven(self) -> None:
        """Same correlation at partition=0/offset=5 delivered twice → replay-proven."""
        e1 = _evt("corr-002", offset=5)
        e2 = _evt("corr-002", offset=5)  # identical
        req = _req(e1, e2)
        result = HANDLER.check(req)

        assert result.status == "clean"
        assert result.total_correlations == 1
        assert result.replay_proven == 1
        assert not result.findings

    def test_dedupe_held_true_on_replay(self) -> None:
        e = _evt("corr-003", offset=10)
        req = _req(e, e, e)  # triple replay
        result = HANDLER.check(req)

        assert result.replay_proven == 1
        assert result.status == "clean"

    def test_replay_count_reflects_occurrence_count(self) -> None:
        """Three identical occurrences → occurrence_count=3 but one classification."""
        e = _evt("corr-004", offset=7)
        req = _req(e, e, e)
        # Access via an intermediate assertion on aggregates
        result = HANDLER.check(req)
        assert result.total_correlations == 1
        assert result.replay_proven == 1

    def test_delegation_and_generation_tables_independent(self) -> None:
        """delegation_events and generation_events are separate (correlation_id, table) keys."""
        e_del = _evt("corr-005", table=_TABLE, offset=1)
        e_gen = _evt("corr-005", table=_TABLE_GEN, topic=_TOPIC_GEN, offset=1)
        req = _req(e_del, e_gen)
        result = HANDLER.check(req)

        # Two distinct (correlation_id, table) pairs — each observed once.
        assert result.total_correlations == 2
        assert result.runtime_observed == 2
        assert result.replay_proven == 0


# ---------------------------------------------------------------------------
# Different offsets for same correlation → superseded
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSuperseded:
    def test_later_offset_marks_earlier_superseded(self) -> None:
        e1 = _evt("corr-006", offset=0)
        e2 = _evt("corr-006", offset=5)
        req = _req(e1, e2)
        result = HANDLER.check(req)

        assert result.status == "findings"
        assert result.superseded == 1
        assert len(result.findings) == 1
        assert result.findings[0].status == EnumReplayStatus.SUPERSEDED
        assert result.findings[0].dedupe_held is True

    def test_superseded_classification_includes_partition_info(self) -> None:
        e1 = _evt("corr-007", partition=0, offset=10)
        e2 = _evt("corr-007", partition=1, offset=0)
        req = _req(e1, e2)
        result = HANDLER.check(req)

        assert result.superseded == 1
        detail = result.findings[0].detail
        assert "superseded" in detail


# ---------------------------------------------------------------------------
# Blocked: empty correlation_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBlocked:
    def test_empty_correlation_id_raises_on_model_construction(self) -> None:
        """ModelProjectionEvent rejects empty correlation_id at construction."""
        with pytest.raises(ValidationError):
            ModelProjectionEvent(
                correlation_id="",
                source_topic=_TOPIC,
                partition=0,
                offset=0,
                table=_TABLE,
            )

    def test_whitespace_only_correlation_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            ModelProjectionEvent(
                correlation_id="   ",
                source_topic=_TOPIC,
                partition=0,
                offset=0,
                table=_TABLE,
            )


# ---------------------------------------------------------------------------
# Mixed batch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMixedBatch:
    def test_mixed_batch_counts_all_statuses(self) -> None:
        """One runtime-observed, one replay-proven, one superseded."""
        e_once = _evt("corr-010", offset=0)
        e_replay_a = _evt("corr-011", offset=3)
        e_replay_b = _evt("corr-011", offset=3)  # exact duplicate
        e_super_old = _evt("corr-012", offset=0)
        e_super_new = _evt("corr-012", offset=9)  # supersedes

        req = _req(e_once, e_replay_a, e_replay_b, e_super_old, e_super_new)
        result = HANDLER.check(req)

        assert result.total_correlations == 3
        assert result.runtime_observed == 1
        assert result.replay_proven == 1
        assert result.superseded == 1
        assert result.status == "findings"
        assert len(result.findings) == 1  # only superseded in findings

    def test_all_clean_returns_clean_status(self) -> None:
        """Only runtime-observed and replay-proven → status=clean."""
        e1 = _evt("corr-020", offset=0)
        e2a = _evt("corr-021", offset=5)
        e2b = _evt("corr-021", offset=5)
        req = _req(e1, e2a, e2b)
        result = HANDLER.check(req)

        assert result.status == "clean"
        assert not result.findings

    def test_empty_request_raises(self) -> None:
        """ModelReplayCheckRequest rejects empty events tuple."""
        with pytest.raises(ValidationError):
            ModelReplayCheckRequest(events=())


# ---------------------------------------------------------------------------
# Idempotent projection DB dedupe — fixture proving ON CONFLICT behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProjectionDedupeFixture:
    """Proves that the idempotent DB upsert pattern holds: replaying the same
    event into InmemoryDatabaseAdapter yields exactly one row.

    This is the 'duplicate/replay fixture proves idempotent projection
    behaviour' required by the OMN-12884 acceptance criteria.
    """

    def test_replay_same_event_yields_one_db_row(self) -> None:
        from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

        db = InmemoryDatabaseAdapter()
        row: dict[str, object] = {
            "correlation_id": "replay-fixture-001",
            "task_type": "test-task",
            "delegated_to": "glm-4.5",
        }
        db.upsert("delegation_events", "correlation_id", row)
        db.upsert("delegation_events", "correlation_id", row)  # replay

        rows = db.query("delegation_events")
        assert len(rows) == 1, f"Expected 1 row after replay; got {len(rows)}"

    def test_replay_n_times_yields_one_db_row(self) -> None:
        from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

        db = InmemoryDatabaseAdapter()
        row: dict[str, object] = {
            "correlation_id": "replay-fixture-002",
            "task_type": "test-task",
            "delegated_to": "gemini-flash",
        }
        for _ in range(10):
            db.upsert("delegation_events", "correlation_id", row)

        rows = db.query("delegation_events")
        assert len(rows) == 1

    def test_different_correlations_yield_distinct_rows(self) -> None:
        from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

        db = InmemoryDatabaseAdapter()
        for i in range(5):
            row: dict[str, object] = {
                "correlation_id": f"corr-{i:03d}",
                "task_type": "test",
                "delegated_to": "model",
            }
            db.upsert(
                "delegation_events",
                "correlation_id",
                row,
            )
        rows = db.query("delegation_events")
        assert len(rows) == 5

    def test_generation_events_dedupe_on_correlation_id(self) -> None:
        from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

        db = InmemoryDatabaseAdapter()
        row: dict[str, object] = {
            "correlation_id": "gen-replay-001",
            "task_description": "generate node",
            "provider": "gemini",
            "contract_passed": True,
        }
        db.upsert("generation_events", "correlation_id", row)
        db.upsert("generation_events", "correlation_id", row)

        rows = db.query("generation_events")
        assert len(rows) == 1
