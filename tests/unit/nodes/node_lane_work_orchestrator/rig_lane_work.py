# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rig that drives the lane work orchestrator one input at a time (OMN-26721)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from omnibase_core.models.lane_work import ModelLaneWorkChange, ModelLaneWorkEvent

from omnimarket.models.overlay_documents.model_lane_work_orchestrator_overlay import (
    load_orchestrator_overlay,
)
from omnimarket.nodes.node_lane_work_orchestrator.journal import (
    InMemoryLaneWorkJournal,
    ProtocolLaneWorkJournal,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_fold_result import (
    ModelLaneWorkFoldResult,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_ingress import (
    ModelLaneWorkIngress,
)
from omnimarket.nodes.node_lane_work_orchestrator.orchestration.lane_work_orchestrator import (
    LaneWorkOrchestrator,
)

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"
HEAD = "a" * 40
T0 = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
CHANGE = ModelLaneWorkChange(
    owner="OmniNode-ai", repo="omnimarket", pr_number=7, ticket="OMN-26721"
)
# The completion each answer kind settles, by the node action it answers.
ANSWERS = {
    "claim_confirmed": "ledger_query_claims",
    "claim_missing": "ledger_query_claims",
    "proof_passed": "lab_proof",
    "proof_failed": "lab_proof",
    "proof_posted": "post_lab_comment",
    "proof_post_failed": "post_lab_comment",
    "handoff_written": "handoff_request",
    "handoff_failed": "handoff_request",
}


def make_orchestrator(
    journal: ProtocolLaneWorkJournal | None = None,
) -> LaneWorkOrchestrator:
    clock = [T0]

    def tick() -> datetime:
        clock[0] = clock[0] + timedelta(seconds=1)
        return clock[0]

    return LaneWorkOrchestrator(
        journal=journal if journal is not None else InMemoryLaneWorkJournal(),
        overlay=load_orchestrator_overlay(),
        clock=tick,
    )


class Rig:
    """Sends typed events for one change and reads back what the orchestrator published."""

    def __init__(
        self,
        orchestrator: LaneWorkOrchestrator | None = None,
        change: ModelLaneWorkChange = CHANGE,
    ) -> None:
        self.orchestrator = orchestrator or make_orchestrator()
        self.change = change
        self.workflow_id: str | None = None
        self.counter = 0
        self.results: list[ModelLaneWorkFoldResult] = []
        self.sent: list[ModelLaneWorkIngress] = []

    def outstanding(self, name: str) -> str | None:
        assert self.workflow_id is not None
        state = self.orchestrator.state_of(self.workflow_id)
        for action_id, action in state.outstanding_actions.items():
            if action == name:
                return action_id
        return None

    def ingress(self, kind: str, **fields: Any) -> ModelLaneWorkIngress:
        self.counter += 1
        if kind in ANSWERS and "action_id" not in fields:
            found = self.outstanding(ANSWERS[kind])
            if found is not None:
                fields["action_id"] = found
        fields.setdefault("input_id", f"in-{self.counter}")
        return ModelLaneWorkIngress(
            correlation_id=uuid4(),
            event=ModelLaneWorkEvent(
                kind=kind,
                workflow_id=None if kind == "start" else self.workflow_id,
                change=self.change,
                **fields,
            ),
        )

    def send(self, kind: str, **fields: Any) -> ModelLaneWorkFoldResult:
        request = self.ingress(kind, **fields)
        self.sent.append(request)
        result = self.orchestrator.handle(request)
        if kind == "start" and result.record is not None:
            self.workflow_id = result.record.state.workflow_id
        self.results.append(result)
        return result

    def check(
        self, kind: str, at: datetime, head: str = HEAD
    ) -> ModelLaneWorkFoldResult:
        return self.send(kind, head_sha=head, observed_at=at, ci_read_at=at)

    @property
    def name(self) -> str:
        assert self.workflow_id is not None
        return self.orchestrator.state_of(self.workflow_id).name

    def to_proving(self) -> None:
        self.send("start", worker_tier="claude-medium")
        self.send("worker_started", lane_name="lane-x", body_ticket_ids=("OMN-26721",))
        self.send("claim_confirmed")
        self.check("check_green", T0 + timedelta(minutes=5))

    def to_handed_off(self) -> None:
        self.to_proving()
        self.send("proof_passed", head_sha=HEAD)
        self.send("proof_posted")
        self.send(
            "handoff_written",
            head_sha=HEAD,
            request_id=self.outstanding("handoff_request"),
        )
        self.check("check_green", T0 + timedelta(hours=1))
