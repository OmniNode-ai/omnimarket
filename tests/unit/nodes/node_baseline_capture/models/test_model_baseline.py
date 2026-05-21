"""Focused model proof for baseline capture snapshots and deltas."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    BaselineProbeType,
    ModelBaselineDelta,
    ModelBaselineSnapshot,
    ModelDbRowCountDelta,
    ModelDbRowCountSnapshot,
    ModelGitBranchDelta,
    ModelGitBranchSnapshot,
    ModelGitHubPRDelta,
    ModelGitHubPRSnapshot,
    ModelKafkaTopicDelta,
    ModelKafkaTopicSnapshot,
    ModelLinearTicketDelta,
    ModelLinearTicketSnapshot,
    ModelServiceHealthDelta,
    ModelServiceHealthSnapshot,
)

_NOW = datetime(2026, 5, 21, 18, 0, tzinfo=UTC)


@pytest.mark.unit
def test_snapshot_probe_union_round_trips_all_probe_types() -> None:
    """Serialized snapshots recover the concrete probe item models."""
    snapshot = ModelBaselineSnapshot(
        baseline_id="baseline-20260521",
        captured_at=_NOW,
        label="proof",
        probes={
            BaselineProbeType.GITHUB_PRS: [
                ModelGitHubPRSnapshot(
                    pr_number=11427,
                    title="Add proof",
                    repo="OmniNode-ai/omnimarket",
                    state="open",
                    age_days=0.25,
                    ci_status="pending",
                )
            ],
            BaselineProbeType.LINEAR_TICKETS: [
                ModelLinearTicketSnapshot(
                    ticket_id="OMN-11427",
                    title="Repowise critical",
                    state="In Progress",
                    priority=2,
                    assignee="jonah",
                    updated_at=_NOW,
                )
            ],
            BaselineProbeType.SYSTEM_HEALTH: [
                ModelServiceHealthSnapshot(
                    service="runtime://201",
                    healthy=True,
                    latency_ms=12.5,
                )
            ],
            BaselineProbeType.KAFKA_TOPICS: [
                ModelKafkaTopicSnapshot(
                    topic="onex.evt.omnimarket.baseline-captured.v1",
                    partition_count=1,
                    latest_offset=42,
                )
            ],
            BaselineProbeType.GIT_BRANCHES: [
                ModelGitBranchSnapshot(
                    repo="omnimarket",
                    branch="jonah/omn-11427-proof",
                    worktree_path="/tmp/omni_worktrees/OMN-11427/omnimarket",
                    age_days=0.01,
                )
            ],
            BaselineProbeType.DB_ROW_COUNTS: [
                ModelDbRowCountSnapshot(table_name="projection_events", row_count=7)
            ],
        },
    )

    parsed = ModelBaselineSnapshot.model_validate(snapshot.model_dump(mode="json"))

    assert isinstance(
        parsed.probes[BaselineProbeType.GITHUB_PRS][0], ModelGitHubPRSnapshot
    )
    assert isinstance(
        parsed.probes[BaselineProbeType.LINEAR_TICKETS][0],
        ModelLinearTicketSnapshot,
    )
    assert isinstance(
        parsed.probes[BaselineProbeType.SYSTEM_HEALTH][0],
        ModelServiceHealthSnapshot,
    )
    assert isinstance(
        parsed.probes[BaselineProbeType.KAFKA_TOPICS][0], ModelKafkaTopicSnapshot
    )
    assert isinstance(
        parsed.probes[BaselineProbeType.GIT_BRANCHES][0], ModelGitBranchSnapshot
    )
    assert isinstance(
        parsed.probes[BaselineProbeType.DB_ROW_COUNTS][0], ModelDbRowCountSnapshot
    )


@pytest.mark.unit
def test_delta_probe_union_round_trips_all_delta_types() -> None:
    """Serialized deltas recover the concrete per-probe delta models."""
    delta = ModelBaselineDelta(
        baseline_id="baseline-20260521",
        baseline_captured_at=_NOW,
        compared_at=_NOW,
        per_probe_deltas={
            BaselineProbeType.GITHUB_PRS: ModelGitHubPRDelta(opened=[11427]),
            BaselineProbeType.LINEAR_TICKETS: ModelLinearTicketDelta(
                state_changes={"OMN-11427": "Backlog -> In Progress"}
            ),
            BaselineProbeType.SYSTEM_HEALTH: ModelServiceHealthDelta(
                recovered=["runtime://201"]
            ),
            BaselineProbeType.KAFKA_TOPICS: ModelKafkaTopicDelta(
                offset_advances={"onex.evt.omnimarket.baseline-captured.v1": 3}
            ),
            BaselineProbeType.GIT_BRANCHES: ModelGitBranchDelta(
                created=["jonah/omn-11427-proof"]
            ),
            BaselineProbeType.DB_ROW_COUNTS: ModelDbRowCountDelta(
                grown=["projection_events"],
                row_delta_by_table={"projection_events": 3},
            ),
        },
    )

    parsed = ModelBaselineDelta.model_validate(delta.model_dump(mode="json"))

    assert isinstance(
        parsed.per_probe_deltas[BaselineProbeType.GITHUB_PRS], ModelGitHubPRDelta
    )
    assert isinstance(
        parsed.per_probe_deltas[BaselineProbeType.LINEAR_TICKETS],
        ModelLinearTicketDelta,
    )
    assert isinstance(
        parsed.per_probe_deltas[BaselineProbeType.SYSTEM_HEALTH],
        ModelServiceHealthDelta,
    )
    assert isinstance(
        parsed.per_probe_deltas[BaselineProbeType.KAFKA_TOPICS], ModelKafkaTopicDelta
    )
    assert isinstance(
        parsed.per_probe_deltas[BaselineProbeType.GIT_BRANCHES], ModelGitBranchDelta
    )
    assert isinstance(
        parsed.per_probe_deltas[BaselineProbeType.DB_ROW_COUNTS], ModelDbRowCountDelta
    )


@pytest.mark.unit
def test_probe_models_forbid_extra_fields() -> None:
    """Probe payloads stay contract-shaped instead of accepting silent drift."""
    with pytest.raises(ValidationError):
        ModelGitHubPRSnapshot(
            pr_number=11427,
            title="Add proof",
            repo="OmniNode-ai/omnimarket",
            state="open",
            age_days=0.25,
            unexpected_authoritative_state="client",
        )
