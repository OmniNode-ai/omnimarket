# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_epic_team_orchestrator [OMN-12206].

ORCHESTRATOR node. Consumes ModelEpicTeamRequest, orchestrates a multi-repo
sprint: decompose epic → build DAG → dispatch waves of ticket-pipeline agents
→ monitor stalls → collect results → run DoD compliance gate.

Bounded production slice: dry-run can return an injected deterministic plan and
live execution requires an injected adapter.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_epic_team_orchestrator.models.model_epic_team_request import (
    ModelEpicTeamRequest,
)
from omnimarket.nodes.node_epic_team_orchestrator.models.model_epic_team_result import (
    EnumDodGateStatus,
    EnumEpicTeamRunStatus,
    ModelEpicTeamResult,
    ModelWaveResult,
)


class ProtocolEpicTeamPlanner(Protocol):
    """Adapter boundary for deterministic epic-team wave planning."""

    def plan_waves(
        self, request: ModelEpicTeamRequest
    ) -> tuple[ModelWaveResult, ...]: ...


class ProtocolEpicTeamExecutor(Protocol):
    """Adapter boundary for live epic-team orchestration side effects."""

    def execute(self, request: ModelEpicTeamRequest) -> ModelEpicTeamResult: ...


class HandlerEpicTeamOrchestrator:
    """ORCHESTRATOR — epic team dry-run planner and adapter-gated executor."""

    def __init__(
        self,
        planner: ProtocolEpicTeamPlanner | None = None,
        executor: ProtocolEpicTeamExecutor | None = None,
    ) -> None:
        self._planner = planner
        self._executor = executor

    def handle(self, request: ModelEpicTeamRequest) -> ModelEpicTeamResult:
        if request.dry_run:
            wave_results = (
                self._planner.plan_waves(request) if self._planner is not None else ()
            )
            return ModelEpicTeamResult(
                epic_id=request.epic_id,
                run_status=EnumEpicTeamRunStatus.DRY_RUN,
                wave_results=wave_results,
                completed_tickets=(),
                failed_tickets=(),
                stall_events=(),
                dod_gate_status=EnumDodGateStatus.SKIPPED,
                total_tickets=sum(wave.dispatched_count for wave in wave_results),
                dry_run=True,
            )

        if self._executor is None:
            raise RuntimeError(
                "epic team executor adapter required when dry_run is false"
            )
        return self._executor.execute(request)


__all__ = [
    "HandlerEpicTeamOrchestrator",
    "ProtocolEpicTeamExecutor",
    "ProtocolEpicTeamPlanner",
]
