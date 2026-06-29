# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD test 2 (OMN-12842): effectiveness populated FROM the ROI score event.

The projection write path is driven by ``context-roi-score-completed.v1``.
On each scored event the projection upserts ``success_rate`` (from
``final_success_rate``), ``first_pass_rate``, ``cost_per_success`` (from
``cost_per_success_usd``), increments ``hit_count``, and sets ``last_scored``
to the event timestamp.
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


def _scored_event(
    *,
    final_success_rate: float = 0.8,
    first_pass_rate: float = 0.6,
    cost_per_success_usd: float = 0.42,
    source_commit: str = "abc123",
) -> ModelCapsuleScoredEvent:
    return ModelCapsuleScoredEvent(
        factor=EnumContextFactor.EXEMPLAR,
        content="exemplar body",
        source_artifact="exemplars/foo.py",
        source_commit=source_commit,
        schema_version=EnumCapsuleSchemaVersion.V1,
        validity_scope="repo:omnimarket",
        final_success_rate=final_success_rate,
        first_pass_rate=first_pass_rate,
        cost_per_success_usd=cost_per_success_usd,
        event_timestamp=_EVENT_TS,
    )


class TestEffectivenessFromRoi:
    def test_row_populated_from_roi_metrics(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _scored_event()
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1

        rows = db.query("capsule_store")
        assert len(rows) == 1
        row = rows[0]
        assert float(row["success_rate"]) == 0.8
        assert float(row["first_pass_rate"]) == 0.6
        assert float(row["cost_per_success"]) == 0.42
        assert int(row["hit_count"]) == 1
        assert row["last_scored"] == _EVENT_TS.isoformat()
        assert row["factor"] == EnumContextFactor.EXEMPLAR.value
        assert row["source_commit"] == "abc123"

    def test_row_carries_capsule_hash_identity(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = HANDLER.project(_scored_event(), db)
        assert result.rows_upserted == 1
        row = db.query("capsule_store")[0]
        assert len(str(row["capsule_hash"])) == 64
        assert row["capsule_id"]

    def test_from_roi_result_view_maps_fields(self) -> None:
        """ModelCapsuleScoredEvent.model_validate accepts the wire payload shape."""
        payload: dict[str, object] = {
            "factor": "exemplar",
            "content": "exemplar body",
            "source_artifact": "exemplars/foo.py",
            "source_commit": "abc123",
            "schema_version": "v1",
            "validity_scope": "repo:omnimarket",
            "final_success_rate": 0.8,
            "first_pass_rate": 0.6,
            "cost_per_success_usd": 0.42,
            "event_timestamp": _EVENT_TS.isoformat(),
        }
        event = ModelCapsuleScoredEvent.model_validate(payload)
        assert event.final_success_rate == 0.8
        assert event.factor == EnumContextFactor.EXEMPLAR
