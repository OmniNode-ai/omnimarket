# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_projection_capsule_store (OMN-12842).

Closes the golden chain:

    onex.evt.omnimarket.context-roi-score-completed.v1 -> capsule_store

Includes the OMN-13122 row-delta proof pattern (before=0, publish, after=1) and
proves the contract <-> topic-registry wiring, idempotent replay, and the
contract-declared decay applied at the read boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.events.topics import (
    CAPSULE_STORE_APPLIED_TOPIC_V1,
    CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1,
)
from omnimarket.nodes.node_projection_capsule_store.handlers.handler_capsule_store_projection import (
    HandlerCapsuleStoreProjection,
    ModelCapsuleScoredEvent,
)
from omnimarket.nodes.node_projection_capsule_store.models.model_capsule_identity import (
    EnumCapsuleSchemaVersion,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerCapsuleStoreProjection()
CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_capsule_store"
    / "contract.yaml"
)
_EVENT_TS = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)

# A realistic context-roi-score-completed.v1 payload, as the score event carries
# the per-capsule provenance plus the ROI effectiveness numbers.
_SCORE_PAYLOAD: dict[str, object] = {
    "factor": "exemplar",
    "content": "def handle(envelope): ...",
    "source_artifact": "exemplars/handler_pattern.py",
    "source_commit": "abc123def",
    "schema_version": "v1",
    "validity_scope": "repo:omnimarket",
    "final_success_rate": 0.85,
    "first_pass_rate": 0.7,
    "cost_per_success_usd": 0.31,
    "event_timestamp": _EVENT_TS.isoformat(),
}


class TestGoldenChainCapsuleStore:
    def test_contract_topics_match_registry(self) -> None:
        contract = yaml.safe_load(CONTRACT_PATH.read_text())
        event_bus = contract["event_bus"]
        assert CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1 in event_bus["subscribe_topics"]
        assert CAPSULE_STORE_APPLIED_TOPIC_V1 in event_bus["publish_topics"]
        # Handler resolves the same topics from the contract via the registry.
        assert HANDLER.subscribe_topic == CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1
        assert HANDLER.applied_topic == CAPSULE_STORE_APPLIED_TOPIC_V1

    def test_row_delta_before_zero_after_one(self) -> None:
        """OMN-13122 row-delta proof: before=0, publish score event, after=1."""
        db = InmemoryDatabaseAdapter()
        before = db.query("capsule_store")
        assert len(before) == 0

        event = ModelCapsuleScoredEvent.model_validate(_SCORE_PAYLOAD)
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1

        after = db.query("capsule_store")
        assert len(after) == 1
        assert len(after) - len(before) == 1
        row = after[0]
        assert float(row["success_rate"]) == 0.85
        assert float(row["first_pass_rate"]) == 0.7
        assert float(row["cost_per_success"]) == 0.31
        assert int(row["hit_count"]) == 1

    def test_replay_is_idempotent_chain(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelCapsuleScoredEvent.model_validate(_SCORE_PAYLOAD)
        HANDLER.project(event, db)
        HANDLER.project(event, db)
        rows = db.query("capsule_store")
        assert len(rows) == 1
        assert int(rows[0]["hit_count"]) == 2

    def test_changed_exemplar_creates_distinct_capsule(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(ModelCapsuleScoredEvent.model_validate(_SCORE_PAYLOAD), db)
        changed = dict(_SCORE_PAYLOAD)
        changed["source_commit"] = "zzz999"
        HANDLER.project(ModelCapsuleScoredEvent.model_validate(changed), db)
        rows = db.query("capsule_store")
        assert len(rows) == 2

    def test_decay_applied_at_read_boundary(self) -> None:
        """The read view ranks by effective (decayed) first_pass_rate."""
        now = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)
        fresh = ModelCapsuleScoredEvent.model_validate(
            {**_SCORE_PAYLOAD, "event_timestamp": now.isoformat()}
        )
        stale = ModelCapsuleScoredEvent.model_validate(
            {
                **_SCORE_PAYLOAD,
                "source_commit": "stale001",
                "event_timestamp": (now - timedelta(days=90)).isoformat(),
            }
        )
        raw = 0.7  # both share the same raw first_pass_rate
        effective_fresh = HANDLER.effective_score(
            raw, last_scored=fresh.event_timestamp, now=now
        )
        effective_stale = HANDLER.effective_score(
            raw, last_scored=stale.event_timestamp, now=now
        )
        assert effective_fresh > effective_stale
        assert fresh.factor == EnumContextFactor.EXEMPLAR
        assert fresh.schema_version == EnumCapsuleSchemaVersion.V1
