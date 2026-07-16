# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain / data-flow tests for node_merge_state_projection (OMN-14648 / WS6).

Proves the full event -> projection -> measurement path with synthetic
onex.evt.omnimarket.merge-state-transition.v1 events (no live publisher, no live
broker):

  synthetic ModelMergeStateTransitionEvent(s)
    -> HandlerMergeStateProjection.handle/project
      -> UPSERT into merge_state_transitions (deduped by deterministic event_id)
        -> compute_merge_flow_metrics over the folded window
          -> evidence-volume ratio, per-state duration, same-head reruns by reason

and verifies the contract declares the read surface the merge-flow telemetry
consumer depends on: subscribe topic, projection_api exposure on
onex.evt.omnimarket.merge-state-transition.v1 with a monotonic cursor_column,
and a node-local migration that creates the table.

REPORT-ONLY: this exercises the measurement layer end to end; no enforcement /
WIP-cap gate is wired off the projection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.events.merge_state import (
    EnumMergeRerunReason,
    ModelMergeStateTransitionEvent,
)
from omnimarket.nodes.merge_state_metrics_native import (
    EVIDENCE_VOLUME_RATIO_TARGET,
    compute_merge_flow_metrics,
)
from omnimarket.nodes.node_merge_state_projection.handlers.handler_merge_state_projection import (
    TABLE,
    HandlerMergeStateProjection,
)
from omnimarket.nodes.pr_ledger_native import EnumPrLifecyclePhase
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

NODE_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_merge_state_projection"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
MIGRATION_PATH = NODE_DIR / "migrations" / "0001_create_merge_state_transitions.sql"

EXPECTED_SUBSCRIBE_TOPIC = "onex.evt.omnimarket.merge-state-transition.v1"

HANDLER = HandlerMergeStateProjection()
_T0 = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _event(
    *,
    pr_number: int = 100,
    head_sha: str = "deadbeef",
    from_state: EnumPrLifecyclePhase,
    to_state: EnumPrLifecyclePhase,
    at: datetime,
    is_occ_evidence: bool = False,
    reason_code: EnumMergeRerunReason | None = None,
) -> ModelMergeStateTransitionEvent:
    return ModelMergeStateTransitionEvent(
        repo="omnimarket",
        pr_number=pr_number,
        head_sha=head_sha,
        from_state=from_state,
        to_state=to_state,
        occurred_at=at,
        is_occ_evidence=is_occ_evidence,
        reason_code=reason_code,
    )


@pytest.mark.unit
class TestMergeStateProjectionDataFlow:
    """event -> projection materialization with synthetic transition events."""

    def test_project_materializes_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        evt = _event(
            from_state=EnumPrLifecyclePhase.TRIAGE,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            at=_T0,
        )
        result = HANDLER.project(evt, db)
        assert result.rows_upserted == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_id"] == evt.event_id
        assert row["from_state"] == "triage"
        assert row["to_state"] == "merge_group"
        # projection_cursor is DB-assigned (BIGSERIAL) — the handler must NOT
        # write it, or the cursor would not be monotonic across inserts.
        assert "projection_cursor" not in row

    def test_dedup_by_event_id_is_idempotent(self) -> None:
        db = InmemoryDatabaseAdapter()
        evt = _event(
            from_state=EnumPrLifecyclePhase.TRIAGE,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            at=_T0,
        )
        HANDLER.project(evt, db)
        HANDLER.project(evt, db)
        assert len(db.query(TABLE)) == 1

    def test_full_chain_event_log_to_metrics(self) -> None:
        """Project a window of transitions, then fold the rows' source events
        into merge-flow metrics: the evidence-volume ratio is materialized from
        the same event log the projection wrote."""
        db = InmemoryDatabaseAdapter()
        # 2 product merges + 2 OCC-evidence merges => ratio 1.0 (<= target 1.1).
        events = [
            _event(
                pr_number=1,
                head_sha="p1",
                from_state=EnumPrLifecyclePhase.MERGE_GROUP,
                to_state=EnumPrLifecyclePhase.TERMINAL,
                at=_T0,
            ),
            _event(
                pr_number=2,
                head_sha="p2",
                from_state=EnumPrLifecyclePhase.MERGE_GROUP,
                to_state=EnumPrLifecyclePhase.TERMINAL,
                at=_T0 + timedelta(minutes=1),
            ),
            _event(
                pr_number=3,
                head_sha="e1",
                from_state=EnumPrLifecyclePhase.MERGE_GROUP,
                to_state=EnumPrLifecyclePhase.TERMINAL,
                at=_T0 + timedelta(minutes=2),
                is_occ_evidence=True,
            ),
            _event(
                pr_number=4,
                head_sha="e2",
                from_state=EnumPrLifecyclePhase.MERGE_GROUP,
                to_state=EnumPrLifecyclePhase.TERMINAL,
                at=_T0 + timedelta(minutes=3),
                is_occ_evidence=True,
            ),
        ]
        for evt in events:
            HANDLER.project(evt, db)
        rows = db.query(TABLE)
        assert len(rows) == 4

        projected_events = [
            ModelMergeStateTransitionEvent.model_validate(
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"event_id", "projection_cursor"}
                }
            )
            for row in rows
        ]
        metrics = compute_merge_flow_metrics(projected_events)
        assert metrics.product_merges == 2
        assert metrics.occ_evidence_merges == 2
        assert metrics.evidence_volume_ratio == pytest.approx(1.0)
        assert metrics.evidence_volume_ratio_target == EVIDENCE_VOLUME_RATIO_TARGET
        assert metrics.evidence_volume_meets_target is True


@pytest.mark.unit
class TestMergeStateProjectionContract:
    """Contract declares the read surface the merge-flow telemetry depends on."""

    def _load_contract(self) -> dict[str, object]:
        return yaml.safe_load(CONTRACT_PATH.read_text())  # type: ignore[return-value]

    def test_contract_and_migration_exist(self) -> None:
        assert CONTRACT_PATH.exists(), f"Missing contract at {CONTRACT_PATH}"
        assert MIGRATION_PATH.exists(), f"Missing migration at {MIGRATION_PATH}"

    def test_contract_subscribes_to_the_topic(self) -> None:
        contract = self._load_contract()
        event_bus = contract["event_bus"]
        assert isinstance(event_bus, dict)
        assert EXPECTED_SUBSCRIBE_TOPIC in event_bus["subscribe_topics"]
        assert event_bus["publish_topics"] == []

    def test_contract_exposes_monotonic_cursor_projection(self) -> None:
        contract = self._load_contract()
        projection_api = contract["projection_api"]
        assert isinstance(projection_api, dict)
        assert projection_api["expose"] is True
        exposure = projection_api["exposures"][0]
        assert exposure["topic"] == EXPECTED_SUBSCRIBE_TOPIC
        assert exposure["table"] == "merge_state_transitions"
        assert exposure["cursor_column"] == "projection_cursor"
