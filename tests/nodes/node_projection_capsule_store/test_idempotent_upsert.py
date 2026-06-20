# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD test 5 (OMN-12842): idempotent upsert / replay safety.

Replaying the same scored event twice yields one row (no duplicate identity
row, because the unique key is ``capsule_hash``), with ``hit_count``
incremented deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.nodes.node_projection_capsule_store.handlers.handler_capsule_store_projection import (
    HandlerCapsuleStoreProjection,
    ModelCapsuleScoredEvent,
)
from omnimarket.nodes.node_projection_capsule_store.models.model_capsule_identity import (
    EnumCapsuleSchemaVersion,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerCapsuleStoreProjection()
_EVENT_TS = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)


def _scored_event(*, source_commit: str = "abc123") -> ModelCapsuleScoredEvent:
    return ModelCapsuleScoredEvent(
        factor=EnumContextFactor.EXEMPLAR,
        content="exemplar body",
        source_artifact="exemplars/foo.py",
        source_commit=source_commit,
        schema_version=EnumCapsuleSchemaVersion.V1,
        validity_scope="repo:omnimarket",
        final_success_rate=0.8,
        first_pass_rate=0.6,
        cost_per_success_usd=0.42,
        event_timestamp=_EVENT_TS,
    )


class TestIdempotentUpsert:
    def test_replay_same_event_yields_one_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _scored_event()
        HANDLER.project(event, db)
        HANDLER.project(event, db)
        rows = db.query("capsule_store")
        assert len(rows) == 1

    def test_replay_increments_hit_count_deterministically(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _scored_event()
        HANDLER.project(event, db)
        first = db.query("capsule_store")[0]
        assert int(first["hit_count"]) == 1

        HANDLER.project(event, db)
        second = db.query("capsule_store")[0]
        assert int(second["hit_count"]) == 2

        HANDLER.project(event, db)
        third = db.query("capsule_store")[0]
        assert int(third["hit_count"]) == 3

    def test_changed_commit_adds_distinct_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(_scored_event(source_commit="abc123"), db)
        HANDLER.project(_scored_event(source_commit="def456"), db)
        rows = db.query("capsule_store")
        assert len(rows) == 2
        hashes = {str(r["capsule_hash"]) for r in rows}
        assert len(hashes) == 2

    def test_before_zero_after_one_row_delta(self) -> None:
        db = InmemoryDatabaseAdapter()
        assert len(db.query("capsule_store")) == 0
        HANDLER.project(_scored_event(), db)
        assert len(db.query("capsule_store")) == 1
