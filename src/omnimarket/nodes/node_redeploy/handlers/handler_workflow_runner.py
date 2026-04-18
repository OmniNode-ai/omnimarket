# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRedeployWorkflowRunner — wires the redeploy FSM to Kafka rebuild.

Drives the FSM through all phases:
  IDLE -> SYNC_CLONES -> UPDATE_PINS -> REBUILD -> SEED_INFISICAL ->
  VERIFY_HEALTH -> DONE

The REBUILD phase invokes HandlerRedeployKafka to publish a rebuild command
to the deploy agent and poll for completion. All other phases advance the FSM
with success=True (infrastructure work delegated to the deploy agent).

Dry-run mode: skips Kafka publish and returns a simulated success result.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_redeploy.handlers.handler_redeploy import HandlerRedeploy
from omnimarket.nodes.node_redeploy.handlers.handler_redeploy_kafka import (
    HandlerRedeployKafka,
)
from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
    EnumRedeployStatus,
    ModelRedeployResult,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    ModelRedeployCommand,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    TERMINAL_PHASES,
    EnumRedeployPhase,
    ModelRedeployState,
)


class ModelRedeployWorkflowInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    scope: str = Field(default="full")
    git_ref: str = Field(default="origin/main")
    services: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)
    skip_sync: bool = Field(default=False)
    verify_only: bool = Field(default=False)
    dry_run: bool = Field(default=False)
    requested_by: str = Field(default="node_redeploy")


class ModelRedeployWorkflowResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    final_phase: EnumRedeployPhase = Field(...)
    phases_completed: int = Field(default=0)
    success: bool = Field(...)
    rebuild_result: ModelRedeployResult | None = Field(default=None)
    error_message: str | None = Field(default=None)


# Phases that the deploy agent handles — FSM advances with success=True for these
# because the deploy agent reports failure via rebuild_result, not FSM circuit breaker.
_DEPLOY_AGENT_PHASES: frozenset[EnumRedeployPhase] = frozenset(
    {
        EnumRedeployPhase.SYNC_CLONES,
        EnumRedeployPhase.UPDATE_PINS,
        EnumRedeployPhase.SEED_INFISICAL,
        EnumRedeployPhase.VERIFY_HEALTH,
    }
)


async def run_redeploy_workflow(
    input_data: ModelRedeployWorkflowInput,
    event_bus: object | None = None,
) -> ModelRedeployWorkflowResult:
    """Run the redeploy workflow end-to-end.

    Args:
        input_data: Workflow inputs parsed from the start command.
        event_bus: Event bus for Kafka publish-monitor. Required unless dry_run=True.

    Returns:
        ModelRedeployWorkflowResult with final phase and rebuild outcome.
    """
    fsm = HandlerRedeploy()
    command = ModelRedeployCommand(
        correlation_id=input_data.correlation_id,
        versions=input_data.versions,
        skip_sync=input_data.skip_sync,
        verify_only=input_data.verify_only,
        dry_run=input_data.dry_run,
        requested_at=datetime.now(tz=UTC),
    )

    state: ModelRedeployState = fsm.start(command)
    rebuild_result: ModelRedeployResult | None = None

    while state.current_phase not in TERMINAL_PHASES:
        current = state.current_phase

        if current == EnumRedeployPhase.REBUILD:
            if input_data.dry_run:
                rebuild_result = ModelRedeployResult(
                    correlation_id=str(input_data.correlation_id),
                    success=True,
                    status=EnumRedeployStatus.SUCCESS,
                    duration_seconds=0.0,
                    git_sha="dry-run",
                    services_restarted=[],
                    phase_results={},
                    errors=[],
                    timed_out=False,
                )
                state, _ = fsm.advance(state, phase_success=True)
            else:
                if event_bus is None:
                    state, _ = fsm.advance(
                        state,
                        phase_success=False,
                        error_message="event_bus required for REBUILD phase (not in dry_run mode)",
                    )
                    break
                kafka_handler = HandlerRedeployKafka(event_bus=event_bus)
                try:
                    rebuild_result = await kafka_handler.execute(
                        scope=input_data.scope,
                        git_ref=input_data.git_ref,
                        services=input_data.services or None,
                        requested_by=input_data.requested_by,
                        correlation_id=str(input_data.correlation_id),
                    )
                    state, _ = fsm.advance(
                        state,
                        phase_success=rebuild_result.success,
                        error_message=rebuild_result.errors[0]
                        if rebuild_result.errors
                        else None,
                    )
                except Exception as exc:
                    state, _ = fsm.advance(
                        state,
                        phase_success=False,
                        error_message=str(exc),
                    )
                    break
        elif current in _DEPLOY_AGENT_PHASES:
            # These phases are handled by the deploy agent during REBUILD.
            # Advance with success — actual work is reported via rebuild_result.
            state, _ = fsm.advance(state, phase_success=True)
        else:
            state, _ = fsm.advance(state, phase_success=True)

    return ModelRedeployWorkflowResult(
        correlation_id=input_data.correlation_id,
        final_phase=state.current_phase,
        phases_completed=state.phases_completed,
        success=state.current_phase == EnumRedeployPhase.DONE,
        rebuild_result=rebuild_result,
        error_message=state.error_message,
    )


class HandlerRedeployWorkflowRunner:
    """RuntimeLocal handler protocol wrapper for redeploy workflow runner."""

    def __init__(self, event_bus: object | None = None) -> None:
        self._event_bus = event_bus

    def set_event_bus(self, event_bus: object) -> None:
        self._event_bus = event_bus

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Parses input_data into ModelRedeployWorkflowInput and runs the workflow.
        Requires an event_bus unless dry_run=True.
        """
        parsed = ModelRedeployWorkflowInput(**input_data)
        result = asyncio.run(run_redeploy_workflow(parsed, event_bus=self._event_bus))
        return result.model_dump(mode="json")


__all__: list[str] = [
    "HandlerRedeployWorkflowRunner",
    "ModelRedeployWorkflowInput",
    "ModelRedeployWorkflowResult",
    "run_redeploy_workflow",
]
