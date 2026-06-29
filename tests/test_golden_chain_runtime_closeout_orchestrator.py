# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for node_runtime_closeout_orchestrator (OMN-13413).

The closeout ORCHESTRATOR dispatches commands over the bus; it has no in-process
FSM loop. Its transitions are the routing edges:

  closeout-start            -> closeout-preflight        (PREFLIGHT command)
  closeout-preflight-done   -> closeout-fitness-gate     (FITNESS_GATE command, ready)
  closeout-preflight-done   -> closeout-completed:BLOCKED (preflight not ready)
  closeout-fitness-gated    -> redeploy-start            (DEPLOY command, fit)
  closeout-fitness-gated    -> closeout-completed:BLOCKED (not fit)
  redeploy-completed:DONE    -> closeout-proof-matrix     (PROOF_MATRIX command)
  redeploy-completed:!DONE   -> closeout-completed:FAILED  (deploy failed)
  closeout-proof-matrix-done -> closeout-completed:COMPLETED (receipt)

# hand-authored: FSM schema not yet on orchestrators (verdict b)
Per the §6 golden-chain DoD, orchestrators have NO typed FSM field today, so
contract-derived auto-generation is not available; these chains are
HAND-AUTHORED full-transition coverage.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_closeout import (
    EnumCloseoutPhase,
    EnumCloseoutRecommendation,
    EnumProofClass,
    EnumProofSet,
    EnumProofVerdict,
    ModelCloseoutReceipt,
    ModelImageRow,
    ModelProofCellVerdict,
)
from omnimarket.events.runtime_deployment import (
    EnumRedeployPhase,
    EnumRuntimeLane,
    ModelRedeployCommand,
    ModelRedeployCompletedEvent,
)
from omnimarket.nodes.node_runtime_closeout_orchestrator.handlers.handler_runtime_closeout_orchestrator import (
    TOPIC_CLOSEOUT_COMPLETED,
    TOPIC_CLOSEOUT_FITNESS_GATE,
    TOPIC_CLOSEOUT_PREFLIGHT,
    TOPIC_CLOSEOUT_PROOF_MATRIX,
    TOPIC_REDEPLOY_START,
    HandlerRuntimeCloseoutOrchestrator,
)
from omnimarket.nodes.node_runtime_closeout_orchestrator.models.model_closeout_phase_messages import (
    ModelCloseoutFitnessGateCommand,
    ModelCloseoutFitnessGateFact,
    ModelCloseoutPreflightCommand,
    ModelCloseoutPreflightFact,
    ModelCloseoutProofMatrixCommand,
    ModelCloseoutProofMatrixFact,
)
from omnimarket.nodes.node_runtime_closeout_orchestrator.models.model_closeout_start_command import (
    ModelCloseoutStartCommand,
)

_DIGEST = "sha256:00c10de7"


@pytest.mark.unit
class TestRuntimeCloseoutOrchestratorGoldenChain:
    async def test_start_to_preflight(self) -> None:
        """Edge 1: closeout-start -> preflight command."""
        handler = HandlerRuntimeCloseoutOrchestrator()
        start = ModelCloseoutStartCommand(
            correlation_id=uuid4(), runtime_lane=EnumRuntimeLane.DEV
        )
        envelope = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.closeout-start.v1",
        )
        output = await handler.handle(envelope)

        assert output.node_kind == EnumNodeKind.ORCHESTRATOR
        assert [e.event_type for e in output.events] == [TOPIC_CLOSEOUT_PREFLIGHT]
        cmd = output.events[0].payload
        assert isinstance(cmd, ModelCloseoutPreflightCommand)
        assert cmd.runtime_lane is EnumRuntimeLane.DEV

    async def test_preflight_ready_to_fitness_gate(self) -> None:
        """Edge 2 (ready): preflight-done -> fitness-gate command."""
        handler = HandlerRuntimeCloseoutOrchestrator()
        corr = uuid4()
        start = ModelCloseoutStartCommand(
            correlation_id=corr, runtime_lane=EnumRuntimeLane.DEV
        )
        fact = ModelCloseoutPreflightFact(
            correlation_id=corr,
            runtime_lane=EnumRuntimeLane.DEV,
            ready=True,
            images=(ModelImageRow(service="runtime", git_sha="abc123"),),
            rollback_target="sha256:prev",
        )
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={
                "preflight": fact.model_dump(mode="json"),
                "start": start.model_dump(mode="json"),
            },
            correlation_id=corr,
            event_type="onex.evt.omnimarket.closeout-preflight-completed.v1",
        )
        output = await handler.handle(envelope)
        assert [e.event_type for e in output.events] == [TOPIC_CLOSEOUT_FITNESS_GATE]
        cmd = output.events[0].payload
        assert isinstance(cmd, ModelCloseoutFitnessGateCommand)
        assert cmd.images[0].git_sha == "abc123"

    async def test_preflight_not_ready_to_blocked(self) -> None:
        """Edge 2 (not ready): preflight-done -> closeout-completed:BLOCKED."""
        handler = HandlerRuntimeCloseoutOrchestrator()
        corr = uuid4()
        fact = ModelCloseoutPreflightFact(
            correlation_id=corr,
            runtime_lane=EnumRuntimeLane.DEV,
            ready=False,
            detail="broker unreachable",
        )
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={"preflight": fact.model_dump(mode="json")},
            correlation_id=corr,
            event_type="onex.evt.omnimarket.closeout-preflight-completed.v1",
        )
        output = await handler.handle(envelope)
        assert [e.event_type for e in output.events] == [TOPIC_CLOSEOUT_COMPLETED]
        receipt = output.events[0].payload
        assert isinstance(receipt, ModelCloseoutReceipt)
        assert receipt.final_phase is EnumCloseoutPhase.BLOCKED
        assert receipt.recommendation is EnumCloseoutRecommendation.HOLD
        assert "broker unreachable" in (receipt.error_message or "")

    async def test_fitness_fit_to_deploy(self) -> None:
        """Edge 3 (fit): fitness-gated -> redeploy-start (deploy) command.

        The deploy phase REUSES node_redeploy_orchestrator via the SHARED
        ModelRedeployCommand (omnimarket.events.runtime_deployment) — the closeout
        never imports the redeploy node's private start model across the boundary.
        """
        handler = HandlerRuntimeCloseoutOrchestrator()
        corr = uuid4()
        start = ModelCloseoutStartCommand(
            correlation_id=corr,
            runtime_lane=EnumRuntimeLane.DEV,
            image_digest=_DIGEST,
        )
        fact = ModelCloseoutFitnessGateFact(
            correlation_id=corr, fit=True, reason="no drift"
        )
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={
                "fitness": fact.model_dump(mode="json"),
                "start": start.model_dump(mode="json"),
            },
            correlation_id=corr,
            event_type="onex.evt.omnimarket.closeout-fitness-gated.v1",
        )
        output = await handler.handle(envelope)
        assert [e.event_type for e in output.events] == [TOPIC_REDEPLOY_START]
        cmd = output.events[0].payload
        assert isinstance(cmd, ModelRedeployCommand)
        assert cmd.runtime_lane is EnumRuntimeLane.DEV
        assert cmd.image_digest == _DIGEST

    async def test_fitness_not_fit_to_blocked(self) -> None:
        """Edge 3 (not fit): fitness-gated -> closeout-completed:BLOCKED."""
        handler = HandlerRuntimeCloseoutOrchestrator()
        corr = uuid4()
        fact = ModelCloseoutFitnessGateFact(
            correlation_id=corr, fit=False, reason="artifact drifted from dev HEAD"
        )
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={"fitness": fact.model_dump(mode="json")},
            correlation_id=corr,
            event_type="onex.evt.omnimarket.closeout-fitness-gated.v1",
        )
        output = await handler.handle(envelope)
        assert [e.event_type for e in output.events] == [TOPIC_CLOSEOUT_COMPLETED]
        receipt = output.events[0].payload
        assert isinstance(receipt, ModelCloseoutReceipt)
        assert receipt.final_phase is EnumCloseoutPhase.BLOCKED
        assert "drifted" in (receipt.error_message or "")

    async def test_deploy_done_to_proof_matrix(self) -> None:
        """Edge 4 (deploy done): redeploy-completed:DONE -> proof-matrix command."""
        handler = HandlerRuntimeCloseoutOrchestrator()
        corr = uuid4()
        start = ModelCloseoutStartCommand(
            correlation_id=corr,
            runtime_lane=EnumRuntimeLane.DEV,
            proof_set=EnumProofSet.REQUIRED,
        )
        deploy_done = ModelRedeployCompletedEvent(
            correlation_id=corr,
            final_phase=EnumRedeployPhase.DONE,
            phases_completed=6,
        )
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={
                "redeploy": deploy_done.model_dump(mode="json"),
                "start": start.model_dump(mode="json"),
            },
            correlation_id=corr,
            event_type="onex.evt.omnimarket.redeploy-completed.v1",
        )
        output = await handler.handle(envelope)
        assert [e.event_type for e in output.events] == [TOPIC_CLOSEOUT_PROOF_MATRIX]
        cmd = output.events[0].payload
        assert isinstance(cmd, ModelCloseoutProofMatrixCommand)
        assert cmd.proof_set is EnumProofSet.REQUIRED
        # required proof set -> only required cells are dispatched.
        assert set(cmd.cells) == {"delegation", "sea", "gate_zero"}

    async def test_deploy_failed_to_failed(self) -> None:
        """Edge 4 (deploy failed): redeploy-completed:FAILED -> closeout-completed:FAILED."""
        handler = HandlerRuntimeCloseoutOrchestrator()
        corr = uuid4()
        deploy_failed = ModelRedeployCompletedEvent(
            correlation_id=corr,
            final_phase=EnumRedeployPhase.FAILED,
            phases_completed=2,
            error_message="rebuild failed",
        )
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={"redeploy": deploy_failed.model_dump(mode="json")},
            correlation_id=corr,
            event_type="onex.evt.omnimarket.redeploy-completed.v1",
        )
        output = await handler.handle(envelope)
        assert [e.event_type for e in output.events] == [TOPIC_CLOSEOUT_COMPLETED]
        receipt = output.events[0].payload
        assert isinstance(receipt, ModelCloseoutReceipt)
        assert receipt.final_phase is EnumCloseoutPhase.FAILED
        assert "rebuild failed" in (receipt.error_message or "")

    async def test_deploy_blocked_to_blocked(self) -> None:
        """Edge 4 (deploy blocked): redeploy-completed:BLOCKED -> closeout:BLOCKED.

        A prod-promotion-gate denial surfaces as redeploy BLOCKED; the closeout
        propagates BLOCKED, never silently treating it as deployed.
        """
        handler = HandlerRuntimeCloseoutOrchestrator()
        corr = uuid4()
        deploy_blocked = ModelRedeployCompletedEvent(
            correlation_id=corr,
            final_phase=EnumRedeployPhase.BLOCKED,
            phases_completed=0,
            error_message="prod promotion gate denied",
        )
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={"redeploy": deploy_blocked.model_dump(mode="json")},
            correlation_id=corr,
            event_type="onex.evt.omnimarket.redeploy-completed.v1",
        )
        output = await handler.handle(envelope)
        receipt = output.events[0].payload
        assert isinstance(receipt, ModelCloseoutReceipt)
        assert receipt.final_phase is EnumCloseoutPhase.BLOCKED

    async def test_proof_matrix_done_to_completed_receipt(self) -> None:
        """Edge 5: proof-matrix-done -> closeout-completed receipt with recommendation."""
        handler = HandlerRuntimeCloseoutOrchestrator()
        corr = uuid4()
        start = ModelCloseoutStartCommand(
            correlation_id=corr, runtime_lane=EnumRuntimeLane.DEV
        )
        verdicts = (
            ModelProofCellVerdict(
                cell="delegation",
                proof_class=EnumProofClass.REQUIRED,
                verdict=EnumProofVerdict.PASS,
            ),
            ModelProofCellVerdict(
                cell="sea",
                proof_class=EnumProofClass.REQUIRED,
                verdict=EnumProofVerdict.PASS,
            ),
            ModelProofCellVerdict(
                cell="gate_zero",
                proof_class=EnumProofClass.REQUIRED,
                verdict=EnumProofVerdict.PASS,
            ),
        )
        fact = ModelCloseoutProofMatrixFact(correlation_id=corr, cell_verdicts=verdicts)
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={
                "proof_matrix": fact.model_dump(mode="json"),
                "start": start.model_dump(mode="json"),
            },
            correlation_id=corr,
            event_type="onex.evt.omnimarket.closeout-proof-matrix-completed.v1",
        )
        output = await handler.handle(envelope)
        assert [e.event_type for e in output.events] == [TOPIC_CLOSEOUT_COMPLETED]
        receipt = output.events[0].payload
        assert isinstance(receipt, ModelCloseoutReceipt)
        assert receipt.final_phase is EnumCloseoutPhase.COMPLETED
        # all required cells PASS and no failed/pending -> internal-integration
        # (full matrix not proven under the required proof set).
        assert receipt.recommendation is EnumCloseoutRecommendation.INTERNAL_INTEGRATION

    async def test_orchestrator_emits_no_projections_or_result(self) -> None:
        """ORCHESTRATOR output constraint: events/intents only."""
        handler = HandlerRuntimeCloseoutOrchestrator()
        start = ModelCloseoutStartCommand(correlation_id=uuid4())
        envelope = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.closeout-start.v1",
        )
        output = await handler.handle(envelope)
        assert output.projections == ()
        assert output.result is None


@pytest.mark.unit
class TestCloseoutReceiptRecommendation:
    def test_required_fail_holds(self) -> None:
        receipt = ModelCloseoutReceipt(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            final_phase=EnumCloseoutPhase.COMPLETED,
            cell_verdicts=(
                ModelProofCellVerdict(
                    cell="delegation",
                    proof_class=EnumProofClass.REQUIRED,
                    verdict=EnumProofVerdict.FAIL,
                ),
            ),
        )
        assert receipt.recompute_recommendation() is EnumCloseoutRecommendation.HOLD

    def test_full_pass_customer_beta(self) -> None:
        verdicts = tuple(
            ModelProofCellVerdict(
                cell=spec_cell,
                proof_class=spec_class,
                verdict=EnumProofVerdict.PASS,
            )
            for spec_cell, spec_class in (
                ("delegation", EnumProofClass.REQUIRED),
                ("sea", EnumProofClass.REQUIRED),
                ("gate_zero", EnumProofClass.REQUIRED),
                ("context", EnumProofClass.STRETCH),
                ("savings", EnumProofClass.STRETCH),
                ("cross_feature", EnumProofClass.RESEARCH),
            )
        )
        receipt = ModelCloseoutReceipt(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            final_phase=EnumCloseoutPhase.COMPLETED,
            cell_verdicts=verdicts,
        )
        assert (
            receipt.recompute_recommendation()
            is EnumCloseoutRecommendation.CUSTOMER_BETA
        )
