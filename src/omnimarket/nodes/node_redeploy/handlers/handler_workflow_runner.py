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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnimarket.nodes.node_redeploy.handlers.deployment_adapter import (
    DEFAULT_PREVIOUS_IMAGE,
    DeploymentAdapterKafka,
    ProtocolDeploymentAdapter,
)
from omnimarket.nodes.node_redeploy.handlers.handler_redeploy import HandlerRedeploy
from omnimarket.nodes.node_redeploy.handlers.handler_redeploy_kafka import (
    HandlerRedeployKafka,
)
from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
    EnumRedeployStatus,
    ModelRedeployResult,
)
from omnimarket.nodes.node_redeploy.models.model_lane_policy import (
    ModelStabilityReadiness,
    evaluate_prod_digest_gate,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
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
    # OMN-12577 lane / digest / rollback inputs.
    runtime_lane: EnumRuntimeLane = Field(
        default=EnumRuntimeLane.DEV,
        description="Target runtime lane for this deployment.",
    )
    image_digest: str | None = Field(
        default=None,
        description="Pinned image digest. Required for prod (stability-proven).",
    )
    promotion_batch_id: str | None = Field(
        default=None, description="Promotion batch shared with OCC evidence."
    )
    previous_image: str = Field(
        default=DEFAULT_PREVIOUS_IMAGE,
        description="Previous known-good image to restore on rollback.",
    )
    smoke_test: bool = Field(
        default=False,
        description=(
            "Run a post-deploy smoke probe. When the deploy reports success but "
            "no live runtime proof is available, the smoke probe fails closed and "
            "triggers rollback (OMN-9579)."
        ),
    )


class ModelRedeployWorkflowResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    final_phase: EnumRedeployPhase = Field(...)
    phases_completed: int = Field(default=0)
    success: bool = Field(...)
    rebuild_result: ModelRedeployResult | None = Field(default=None)
    error_message: str | None = Field(default=None)
    rolled_back: bool = Field(
        default=False, description="True if a rollback was triggered (OMN-9579)."
    )


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


def _rollback_reason(
    rebuild_result: ModelRedeployResult | None,
    smoke_test: bool,
) -> str | None:
    """Return a rollback reason for a successful-deploy that fails post-checks.

    A deploy that the agent reports as ``failed`` is NOT a rollback — the
    artifact never went live, so the FSM circuit breaker handles it. Rollback is
    only for a deploy that succeeded then failed post-deploy verification:

      - the publish-monitor timed out waiting for completion (``timed_out``);
      - the agent reported success but a ``/health`` check came back failing;
      - a smoke probe was requested and there is no live runtime proof, so it
        fails closed (OMN-9579).
    """
    if rebuild_result is None:
        return None
    if rebuild_result.timed_out:
        return "deploy agent timed out before completion; rolling back"
    if not rebuild_result.success:
        # Agent-reported build failure — handled by the FSM, not rollback.
        return None
    failing_health = [hc for hc in rebuild_result.health_checks if hc.status == "fail"]
    if failing_health:
        endpoints = ", ".join(hc.endpoint for hc in failing_health)
        return f"post-deploy health check failed ({endpoints}); rolling back"
    if smoke_test:
        return "post-deploy smoke test failed (no live runtime proof); rolling back"
    return None


async def run_redeploy_workflow(
    input_data: ModelRedeployWorkflowInput,
    event_bus: object | None = None,
    deployment_adapter: ProtocolDeploymentAdapter | None = None,
) -> ModelRedeployWorkflowResult:
    """Run the redeploy workflow end-to-end.

    Args:
        input_data: Workflow inputs parsed from the start command.
        event_bus: Event bus for Kafka publish-monitor. Required unless dry_run=True.
        deployment_adapter: Rollback boundary. Defaults to a Kafka-backed adapter
            built from ``event_bus`` (OMN-9579 / OMN-12577).

    Returns:
        ModelRedeployWorkflowResult with final phase and rebuild outcome.
    """
    # Lane gate: production may not enter REBUILD/deploy unless a stability-test
    # readiness event exists for the SAME digest. The gate runs BEFORE any deploy
    # effect so a bad prod request is rejected before the agent is invoked.
    if input_data.runtime_lane is EnumRuntimeLane.PROD:
        # No stability readiness is threaded through this entry point yet, so the
        # gate fails closed: a prod deploy here is blocked until the readiness
        # handoff (publish/consume readiness-gate events) supplies a matching
        # READY digest. This is the regression guard for prod-vs-stability drift.
        gate = evaluate_prod_digest_gate(
            requested_digest=input_data.image_digest,
            stability_readiness=_stability_readiness_for(input_data),
        )
        if not gate.allowed:
            return ModelRedeployWorkflowResult(
                correlation_id=input_data.correlation_id,
                final_phase=EnumRedeployPhase.BLOCKED,
                phases_completed=0,
                success=False,
                rebuild_result=None,
                error_message=gate.reason,
            )

    fsm = HandlerRedeploy()
    command = ModelRedeployCommand(
        correlation_id=input_data.correlation_id,
        versions=input_data.versions,
        skip_sync=input_data.skip_sync,
        verify_only=input_data.verify_only,
        dry_run=input_data.dry_run,
        requested_at=datetime.now(tz=UTC),
        runtime_lane=input_data.runtime_lane,
        image_digest=input_data.image_digest,
        promotion_batch_id=input_data.promotion_batch_id,
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

    # Post-deploy rollback: a deploy that succeeded but failed post-deploy health
    # (smoke/health/timeout) restores the previous image and emits the
    # rolled-back event over the deploy boundary (OMN-9579). Dry-run never rolls
    # back — there is nothing live to verify.
    if not input_data.dry_run and event_bus is not None:
        reason = _rollback_reason(rebuild_result, input_data.smoke_test)
        if reason is not None:
            adapter = deployment_adapter or DeploymentAdapterKafka(event_bus=event_bus)
            await adapter.rollback(
                correlation_id=input_data.correlation_id,
                runtime_lane=input_data.runtime_lane,
                restored_image=input_data.previous_image,
                failure_reason=reason,
                failed_phase=EnumRedeployPhase.VERIFY_HEALTH,
            )
            return ModelRedeployWorkflowResult(
                correlation_id=input_data.correlation_id,
                final_phase=EnumRedeployPhase.FAILED,
                phases_completed=state.phases_completed,
                success=False,
                rebuild_result=rebuild_result,
                error_message=reason,
                rolled_back=True,
            )

    return ModelRedeployWorkflowResult(
        correlation_id=input_data.correlation_id,
        final_phase=state.current_phase,
        phases_completed=state.phases_completed,
        success=state.current_phase == EnumRedeployPhase.DONE,
        rebuild_result=rebuild_result,
        error_message=state.error_message,
    )


def _stability_readiness_for(
    input_data: ModelRedeployWorkflowInput,
) -> ModelStabilityReadiness | None:
    """Resolve the stability readiness fact for a prod deploy.

    The readiness handoff (publish ``readiness-gate-start``, consume
    ``readiness-gate-{completed,blocked}``) is the source of this fact in the
    wired runtime. This entry point does not yet receive it, so it returns
    ``None`` and the prod gate fails closed — exactly the prod-vs-stability drift
    regression guard the design requires.
    """
    return None


class HandlerRedeployWorkflowRunner:
    """RuntimeLocal handler protocol wrapper for redeploy workflow runner."""

    def __init__(self, event_bus: object) -> None:
        self._event_bus = event_bus

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Validates ``input_data`` into ``ModelRedeployWorkflowInput`` at the
        handler boundary and runs the workflow. Requires an event_bus unless
        dry_run=True.

        The dispatched envelope arrives here as a raw dict. ``ModelRedeployWorkflowInput``
        is ``frozen=True, extra="forbid"``, so an unexpected key or a wrong-typed
        field raises ``ValidationError``. Before OMN-12478 that exception escaped
        ``handle`` with no terminal output, so the CLI hung for the full 660s
        ``timeout_ms`` waiting for a terminal event that never arrived. Now a
        validation failure is converted into a terminal ``ModelRedeployWorkflowResult``
        (``final_phase=FAILED``, ``success=False``) — the contracted output the
        runtime publishes as ``onex.evt.omnimarket.redeploy-completed.v1`` — so
        callers fail fast instead of hanging.
        """
        try:
            parsed = ModelRedeployWorkflowInput.model_validate(input_data)
        except ValidationError as exc:
            return self._validation_failure_result(input_data, exc).model_dump(
                mode="json"
            )
        result = asyncio.run(run_redeploy_workflow(parsed, event_bus=self._event_bus))
        return result.model_dump(mode="json")

    @staticmethod
    def _validation_failure_result(
        input_data: dict[str, object], exc: ValidationError
    ) -> ModelRedeployWorkflowResult:
        """Build the terminal failure result for a malformed dispatched envelope.

        The correlation_id is only used to label the terminal result so a caller
        can correlate the failure; the run itself is rejected. A missing or
        non-UUID value falls back to a freshly generated id rather than masking
        the validation failure.
        """
        raw_corr = input_data.get("correlation_id")
        try:
            correlation_id = UUID(str(raw_corr)) if raw_corr is not None else uuid4()
        except (ValueError, AttributeError):
            correlation_id = uuid4()
        return ModelRedeployWorkflowResult(
            correlation_id=correlation_id,
            final_phase=EnumRedeployPhase.FAILED,
            phases_completed=0,
            success=False,
            rebuild_result=None,
            error_message=f"input validation failed: {exc.error_count()} errors",
        )


__all__: list[str] = [
    "HandlerRedeployWorkflowRunner",
    "ModelRedeployWorkflowInput",
    "ModelRedeployWorkflowResult",
    "run_redeploy_workflow",
]
