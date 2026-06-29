# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD test 3 (OMN-12845 / M5): feedback edge writes effectiveness onto capsule.

A scored runtime ROI row (controlled-intervention) fed into the feedback-edge
reducer:

* writes the effectiveness score onto the stored M2 capsule keyed by
  ``capsule_hash`` (success_rate / first_pass_rate / last_scored populated); and
* causes M3 context selection to re-rank — a capsule whose live score is raised
  above another candidate is selected ahead of it.

The feedback edge folds through the M2 capsule_store projection (the durable
write surface, OMN-12842) — it never owns a bespoke write path. The M3 selection
node then ranks by the live-updated score.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_selection_reason import EnumSelectionReason

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.events.capsule_feedback import (
    EnumRowAttributionClass,
    ModelScoredRuntimeRow,
)
from omnimarket.nodes.node_capsule_effectiveness_feedback_reducer.handlers.handler_capsule_effectiveness_feedback import (
    HandlerCapsuleEffectivenessFeedback,
)
from omnimarket.nodes.node_context_selection_policy_compute.handlers.handler_context_selection_policy import (
    HandlerContextSelectionPolicy,
)
from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_request import (
    ModelContextCandidate,
    ModelContextSelectionRequest,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_EVENT_TS = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _controlled_row(
    *,
    content: str = "exemplar body",
    source_commit: str = "abc123",
    final_success_rate: float = 0.9,
    first_pass_rate: float = 0.8,
) -> ModelScoredRuntimeRow:
    return ModelScoredRuntimeRow(
        factor=EnumContextFactor.EXEMPLAR,
        content=content,
        source_artifact="exemplars/foo.py",
        source_commit=source_commit,
        validity_scope="repo:omnimarket",
        final_success_rate=final_success_rate,
        first_pass_rate=first_pass_rate,
        cost_per_success_usd=0.30,
        proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
        attribution_class=EnumRowAttributionClass.CONTROLLED_INTERVENTION,
        routing_source="routing_tier:local-coder",
        event_timestamp=_EVENT_TS,
    )


class TestFeedbackEdgeWritesEffectiveness:
    def test_effectiveness_written_onto_capsule_by_hash(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerCapsuleEffectivenessFeedback()

        result = handler.project(_controlled_row(), db)
        assert result.effectiveness_claim_written is True
        assert result.capsule_hash, "expected the capsule_hash of the written row"

        rows = db.query("capsule_store", {"capsule_hash": result.capsule_hash})
        assert len(rows) == 1
        row = rows[0]
        assert float(row["success_rate"]) == 0.9
        assert float(row["first_pass_rate"]) == 0.8
        assert row["last_scored"] == _EVENT_TS.isoformat()

    def test_selection_reranks_on_updated_score(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerCapsuleEffectivenessFeedback()

        # Capsule A starts low; capsule B is higher.
        low = handler.project(
            _controlled_row(content="A body", source_commit="aaa", first_pass_rate=0.2),
            db,
        )
        high = handler.project(
            _controlled_row(content="B body", source_commit="bbb", first_pass_rate=0.7),
            db,
        )

        def _score(capsule_hash: str) -> float:
            stored = db.query("capsule_store", {"capsule_hash": capsule_hash})[0]
            return float(stored["first_pass_rate"])

        selection = HandlerContextSelectionPolicy()
        before = selection.handle(
            ModelContextSelectionRequest(
                candidates=(
                    ModelContextCandidate(
                        factor=EnumContextFactor.EXEMPLAR,
                        source=low.capsule_hash,
                        effectiveness_score=_score(low.capsule_hash),
                        is_required=False,
                    ),
                    ModelContextCandidate(
                        factor=EnumContextFactor.EXEMPLAR,
                        source=high.capsule_hash,
                        effectiveness_score=_score(high.capsule_hash),
                        is_required=False,
                    ),
                ),
            )
        )
        # B (higher live score) ranks first.
        assert before.selections[0].source == high.capsule_hash
        assert before.selections[0].selection_reason == (
            EnumSelectionReason.POLICY_EFFECTIVENESS
        )

        # Feed a new controlled row that lifts A above B, then re-rank.
        handler.project(
            _controlled_row(
                content="A body", source_commit="aaa", first_pass_rate=0.95
            ),
            db,
        )
        after = selection.handle(
            ModelContextSelectionRequest(
                candidates=(
                    ModelContextCandidate(
                        factor=EnumContextFactor.EXEMPLAR,
                        source=low.capsule_hash,
                        effectiveness_score=_score(low.capsule_hash),
                        is_required=False,
                    ),
                    ModelContextCandidate(
                        factor=EnumContextFactor.EXEMPLAR,
                        source=high.capsule_hash,
                        effectiveness_score=_score(high.capsule_hash),
                        is_required=False,
                    ),
                ),
            )
        )
        # A now ranks first — selection re-ranked on the live-updated score.
        assert after.selections[0].source == low.capsule_hash
