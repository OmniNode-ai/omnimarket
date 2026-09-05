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
    PROOF_MATRIX_CELLS,
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


def _proved_cells(
    proof_set: EnumProofSet = EnumProofSet.REQUIRED,
) -> tuple[ModelProofCellVerdict, ...]:
    return tuple(
        ModelProofCellVerdict(
            cell=spec.cell,
            proof_class=spec.proof_class,
            verdict=EnumProofVerdict.PASS,
            correlation_id=uuid4(),
            terminal_event=f"onex.evt.omnimarket.{spec.cell}-completed.v1",
            projection_readback=f"projection://runtime-proof/{spec.cell}",
        )
        for spec in PROOF_MATRIX_CELLS
        if proof_set is EnumProofSet.FULL or spec.proof_class is EnumProofClass.REQUIRED
    )


def _unproved_cells(
    case: str, proof_set: EnumProofSet
) -> tuple[ModelProofCellVerdict, ...]:
    cells = _proved_cells(proof_set)
    if case == "empty":
        return ()
    if case == "missing_required":
        return cells[1:]
    if case == "missing_stretch":
        return tuple(cell for cell in cells if cell.cell != "context")
    if case == "unknown":
        return (*cells, cells[0].model_copy(update={"cell": "unregistered"}))
    if case == "duplicate":
        return (*cells, cells[0])
    if case == "conflicting_duplicate":
        return (
            cells[0].model_copy(update={"verdict": EnumProofVerdict.FAIL}),
            *cells,
        )
    if case == "shared_cid":
        return (
            cells[0],
            cells[1].model_copy(update={"correlation_id": cells[0].correlation_id}),
            *cells[2:],
        )
    if case == "reclassified":
        return (
            cells[0].model_copy(update={"proof_class": EnumProofClass.RESEARCH}),
            *cells[1:],
        )
    if case == "reclassified_required_skip":
        return (
            cells[0].model_copy(
                update={
                    "proof_class": EnumProofClass.RESEARCH,
                    "verdict": EnumProofVerdict.SKIP,
                }
            ),
            *cells[1:],
        )
    if case.startswith("stretch_"):
        verdict = EnumProofVerdict(case.removeprefix("stretch_"))
        return tuple(
            cell.model_copy(update={"verdict": verdict})
            if cell.cell == "context"
            else cell
            for cell in cells
        )
    if case in ("fail", "pending", "skip"):
        return (
            cells[0].model_copy(update={"verdict": EnumProofVerdict(case)}),
            *cells[1:],
        )
    field, value = {
        "missing_cid": ("correlation_id", None),
        "missing_terminal": ("terminal_event", None),
        "blank_terminal": ("terminal_event", " \t"),
        "missing_readback": ("projection_readback", None),
        "blank_readback": ("projection_readback", " \t"),
    }[case]
    return (cells[0].model_copy(update={field: value}), *cells[1:])


_UNPROVED_CASES = (
    "empty",
    "missing_required",
    "fail",
    "pending",
    "skip",
    "unknown",
    "duplicate",
    "conflicting_duplicate",
    "shared_cid",
    "reclassified",
    "missing_cid",
    "missing_terminal",
    "blank_terminal",
    "missing_readback",
    "blank_readback",
)
_FULL_UNPROVED_CASES = (
    "missing_stretch",
    "stretch_fail",
    "stretch_pending",
    "stretch_skip",
    "reclassified_required_skip",
)


def test_runtime_closeout_declared_topic_literals_match_handler_constants() -> None:
    """Pin contract-declared publish topics to handler constants."""
    assert TOPIC_CLOSEOUT_PREFLIGHT == "onex.cmd.omnimarket.closeout-preflight.v1"
    assert TOPIC_CLOSEOUT_FITNESS_GATE == "onex.cmd.omnimarket.closeout-fitness-gate.v1"
    assert TOPIC_REDEPLOY_START == "onex.cmd.omnimarket.redeploy-start.v1"
    assert TOPIC_CLOSEOUT_PROOF_MATRIX == "onex.cmd.omnimarket.closeout-proof-matrix.v1"
    assert TOPIC_CLOSEOUT_COMPLETED == "onex.evt.omnimarket.closeout-completed.v1"


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
        verdicts = _proved_cells()
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

    @pytest.mark.parametrize("proof_set", tuple(EnumProofSet))
    @pytest.mark.parametrize("case", _UNPROVED_CASES)
    async def test_unproved_matrix_never_completes(
        self, case: str, proof_set: EnumProofSet
    ) -> None:
        await self._assert_matrix_holds(_unproved_cells(case, proof_set), proof_set)

    @pytest.mark.parametrize("case", _FULL_UNPROVED_CASES)
    async def test_full_matrix_requires_every_requested_cell(self, case: str) -> None:
        await self._assert_matrix_holds(
            _unproved_cells(case, EnumProofSet.FULL), EnumProofSet.FULL
        )

    async def _assert_matrix_holds(
        self,
        cells: tuple[ModelProofCellVerdict, ...],
        proof_set: EnumProofSet,
    ) -> None:
        corr = uuid4()
        start = ModelCloseoutStartCommand(
            correlation_id=corr,
            runtime_lane=EnumRuntimeLane.DEV,
            proof_set=proof_set,
        )
        fact = ModelCloseoutProofMatrixFact(correlation_id=corr, cell_verdicts=cells)
        envelope = ModelEventEnvelope(
            payload={"start": start, "proof_matrix": fact},
            correlation_id=corr,
            event_type="onex.evt.omnimarket.closeout-proof-matrix-completed.v1",
        )
        output = await HandlerRuntimeCloseoutOrchestrator().handle(envelope)
        assert [event.event_type for event in output.events] == [
            TOPIC_CLOSEOUT_COMPLETED
        ]
        receipt = output.events[0].payload
        assert isinstance(receipt, ModelCloseoutReceipt)
        assert receipt.final_phase in (
            EnumCloseoutPhase.BLOCKED,
            EnumCloseoutPhase.FAILED,
        )
        assert receipt.recommendation is EnumCloseoutRecommendation.HOLD
        assert receipt.recompute_recommendation() is EnumCloseoutRecommendation.HOLD
        assert receipt.error_message

    @pytest.mark.parametrize("proof_set", tuple(EnumProofSet))
    async def test_complete_matrix_preserves_context_and_replays(
        self, proof_set: EnumProofSet
    ) -> None:
        corr = uuid4()
        lane = EnumRuntimeLane.STABILITY_TEST
        start = ModelCloseoutStartCommand(
            correlation_id=corr, runtime_lane=lane, proof_set=proof_set
        )
        fact = ModelCloseoutProofMatrixFact(
            correlation_id=corr, cell_verdicts=_proved_cells(proof_set)
        )
        envelope = ModelEventEnvelope(
            payload={
                "start": start.model_dump(mode="json"),
                "proof_matrix": fact.model_dump(mode="json"),
            },
            correlation_id=corr,
            event_type="onex.evt.omnimarket.closeout-proof-matrix-completed.v1",
        )
        handler = HandlerRuntimeCloseoutOrchestrator()
        first = (await handler.handle(envelope)).events[0].payload
        replay = (await handler.handle(envelope)).events[0].payload
        assert isinstance(first, ModelCloseoutReceipt)
        assert first == replay
        assert first.correlation_id == corr
        assert first.runtime_lane is lane
        assert first.proof_set is proof_set
        assert first.final_phase is EnumCloseoutPhase.COMPLETED
        expected = (
            EnumCloseoutRecommendation.CUSTOMER_BETA
            if proof_set is EnumProofSet.FULL
            else EnumCloseoutRecommendation.INTERNAL_INTEGRATION
        )
        assert first.recommendation is expected
        assert first.recompute_recommendation() is expected

    @pytest.mark.parametrize(
        "case",
        [
            "missing_start",
            "missing_start_cid",
            "missing_lane",
            "missing_proof_set",
            "wrong_start_cid",
            "wrong_fact_cid",
            "missing_fact_cid",
            "missing_envelope_cid",
            "customer_lane_is_not_compose_dev",
        ],
    )
    async def test_proof_terminal_rejects_unbound_context(self, case: str) -> None:
        corr = uuid4()
        start = ModelCloseoutStartCommand(
            correlation_id=corr,
            runtime_lane=EnumRuntimeLane.DEV,
            proof_set=EnumProofSet.REQUIRED,
        ).model_dump(mode="json")
        fact = ModelCloseoutProofMatrixFact(
            correlation_id=corr, cell_verdicts=_proved_cells()
        ).model_dump(mode="json")
        payload = {"start": start, "proof_matrix": fact}
        if case == "missing_start":
            payload.pop("start")
        elif case in ("missing_start_cid", "missing_lane", "missing_proof_set"):
            field = {
                "missing_start_cid": "correlation_id",
                "missing_lane": "runtime_lane",
                "missing_proof_set": "proof_set",
            }[case]
            start.pop(field)
        elif case == "wrong_start_cid":
            start["correlation_id"] = str(uuid4())
        elif case == "wrong_fact_cid":
            fact["correlation_id"] = str(uuid4())
        elif case == "missing_fact_cid":
            fact.pop("correlation_id")
        elif case == "customer_lane_is_not_compose_dev":
            start["runtime_lane"] = "onex-dev"
        envelope = ModelEventEnvelope(
            payload=payload,
            correlation_id=None if case == "missing_envelope_cid" else corr,
            event_type="onex.evt.omnimarket.closeout-proof-matrix-completed.v1",
        )
        with pytest.raises(
            ValueError, match=r"proof-matrix terminal|correlation_id|runtime_lane"
        ):
            await HandlerRuntimeCloseoutOrchestrator().handle(envelope)

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
            proof_set=EnumProofSet.REQUIRED,
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
        receipt = ModelCloseoutReceipt(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            proof_set=EnumProofSet.FULL,
            final_phase=EnumCloseoutPhase.COMPLETED,
            cell_verdicts=_proved_cells(EnumProofSet.FULL),
        )
        assert (
            receipt.recompute_recommendation()
            is EnumCloseoutRecommendation.CUSTOMER_BETA
        )

    def test_required_scope_caps_recommendation_with_extra_canonical_proof(
        self,
    ) -> None:
        receipt = ModelCloseoutReceipt(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            proof_set=EnumProofSet.REQUIRED,
            final_phase=EnumCloseoutPhase.COMPLETED,
            cell_verdicts=_proved_cells(EnumProofSet.FULL),
        )
        assert (
            receipt.recompute_recommendation()
            is EnumCloseoutRecommendation.INTERNAL_INTEGRATION
        )

    @pytest.mark.parametrize("proof_set", tuple(EnumProofSet))
    @pytest.mark.parametrize("case", _UNPROVED_CASES)
    def test_unproved_receipt_holds(self, case: str, proof_set: EnumProofSet) -> None:
        receipt = ModelCloseoutReceipt(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            proof_set=proof_set,
            final_phase=EnumCloseoutPhase.COMPLETED,
            cell_verdicts=_unproved_cells(case, proof_set),
        )
        assert receipt.recompute_recommendation() is EnumCloseoutRecommendation.HOLD

    @pytest.mark.parametrize("case", _FULL_UNPROVED_CASES)
    def test_full_receipt_requires_every_requested_cell(self, case: str) -> None:
        receipt = ModelCloseoutReceipt(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            proof_set=EnumProofSet.FULL,
            final_phase=EnumCloseoutPhase.COMPLETED,
            cell_verdicts=_unproved_cells(case, EnumProofSet.FULL),
        )
        assert receipt.recompute_recommendation() is EnumCloseoutRecommendation.HOLD

    @pytest.mark.parametrize(
        "phase", [EnumCloseoutPhase.BLOCKED, EnumCloseoutPhase.FAILED]
    )
    def test_non_success_phase_holds_even_with_full_proof(
        self, phase: EnumCloseoutPhase
    ) -> None:
        receipt = ModelCloseoutReceipt(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            proof_set=EnumProofSet.FULL,
            final_phase=phase,
            cell_verdicts=_proved_cells(EnumProofSet.FULL),
        )
        assert receipt.recompute_recommendation() is EnumCloseoutRecommendation.HOLD
