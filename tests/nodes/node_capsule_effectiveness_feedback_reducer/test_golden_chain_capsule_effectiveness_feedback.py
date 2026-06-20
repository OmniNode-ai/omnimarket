# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_capsule_effectiveness_feedback_reducer (OMN-12845).

Closes the M5 live closed loop:

    onex.evt.omnimarket.context-roi-runtime-row-scored.v1
      -> (controlled) effectiveness CLAIM folded onto capsule_store (M2)
      -> M3 context selection re-ranks on the live-updated score
    onex.evt.omnimarket.context-roi-runtime-row-scored.v1
      -> (observational) HYPOTHESIS only; capsule_store unchanged

Includes the OMN-13122 row-delta proof pattern (before=0, fold, after=1) and
proves the contract <-> topic-registry wiring on both the subscribe and the two
publish topics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.events.capsule_feedback import (
    EnumRowAttributionClass,
    ModelScoredRuntimeRow,
)
from omnimarket.events.topics import (
    CAPSULE_EFFECTIVENESS_HYPOTHESIS_TOPIC_V1,
    CONTEXT_ROI_RUNTIME_ROW_SCORED_TOPIC_V1,
    CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1,
)
from omnimarket.nodes.node_capsule_effectiveness_feedback_reducer.handlers.handler_capsule_effectiveness_feedback import (
    HandlerCapsuleEffectivenessFeedback,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerCapsuleEffectivenessFeedback()
CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_capsule_effectiveness_feedback_reducer"
    / "contract.yaml"
)
_EVENT_TS = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)

_ROW_PAYLOAD: dict[str, object] = {
    "factor": "exemplar",
    "content": "def handle(envelope): ...",
    "source_artifact": "exemplars/handler_pattern.py",
    "source_commit": "abc123def",
    "validity_scope": "repo:omnimarket",
    "final_success_rate": 0.85,
    "first_pass_rate": 0.7,
    "cost_per_success_usd": 0.31,
    "proof_class": EnumProofClass.RUNTIME_OBSERVED_ONLY.value,
    "attribution_class": EnumRowAttributionClass.CONTROLLED_INTERVENTION.value,
    "routing_source": "routing_tier:local-coder",
    "event_timestamp": _EVENT_TS.isoformat(),
}


class TestGoldenChainCapsuleEffectivenessFeedback:
    def test_contract_topics_match_registry(self) -> None:
        contract = yaml.safe_load(CONTRACT_PATH.read_text())
        event_bus = contract["event_bus"]
        assert CONTEXT_ROI_RUNTIME_ROW_SCORED_TOPIC_V1 in event_bus["subscribe_topics"]
        assert CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1 in event_bus["publish_topics"]
        assert CAPSULE_EFFECTIVENESS_HYPOTHESIS_TOPIC_V1 in event_bus["publish_topics"]
        # Handler resolves the same topics from the contract via the registry.
        assert HANDLER.subscribe_topic == CONTEXT_ROI_RUNTIME_ROW_SCORED_TOPIC_V1
        assert HANDLER.claim_topic == CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1
        assert HANDLER.hypothesis_topic == CAPSULE_EFFECTIVENESS_HYPOTHESIS_TOPIC_V1

    def test_controlled_row_delta_before_zero_after_one(self) -> None:
        """OMN-13122 row-delta proof for the controlled claim path."""
        db = InmemoryDatabaseAdapter()
        before = db.query("capsule_store")
        assert len(before) == 0

        row = ModelScoredRuntimeRow.model_validate(_ROW_PAYLOAD)
        result = HANDLER.project(row, db)
        assert result.effectiveness_claim_written is True
        assert result.rows_upserted == 1

        after = db.query("capsule_store")
        assert len(after) - len(before) == 1
        stored = after[0]
        assert float(stored["success_rate"]) == 0.85
        assert float(stored["first_pass_rate"]) == 0.7

    def test_observational_row_delta_stays_zero(self) -> None:
        """The observational hypothesis path leaves capsule_store untouched."""
        db = InmemoryDatabaseAdapter()
        observational = {
            **_ROW_PAYLOAD,
            "attribution_class": EnumRowAttributionClass.OBSERVATIONAL.value,
        }
        row = ModelScoredRuntimeRow.model_validate(observational)
        result = HANDLER.project(row, db)
        assert result.effectiveness_claim_written is False
        assert result.hypothesis_recorded is True
        assert db.query("capsule_store") == []

    def test_replay_is_idempotent_chain(self) -> None:
        db = InmemoryDatabaseAdapter()
        row = ModelScoredRuntimeRow.model_validate(_ROW_PAYLOAD)
        HANDLER.project(row, db)
        HANDLER.project(row, db)
        rows = db.query("capsule_store")
        assert len(rows) == 1
        assert int(rows[0]["hit_count"]) == 2

    def test_row_validates_factor_enum(self) -> None:
        row = ModelScoredRuntimeRow.model_validate(_ROW_PAYLOAD)
        assert row.factor == EnumContextFactor.EXEMPLAR
        assert row.is_controlled is True
