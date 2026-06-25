# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-closeout ORCHESTRATOR handler (OMN-13413).

Turns the overnight hand-run closeout pipeline into a canonical ORCHESTRATOR. It
owns phase sequencing but dispatches every phase OVER THE BUS — it never
constructs sibling handlers in-process, never runs an in-process FSM loop, and
never does I/O. ``handle(envelope)`` returns ``ModelHandlerOutput.for_orchestrator``
carrying the next command(s) as event envelopes the runtime publishes; the
orchestrator reacts to the resulting preflight / fitness-gate / deploy /
proof-matrix completion facts.

Flow (event-driven, no in-process loop):

  1. consume ``closeout-start`` -> emit the preflight command (read-only:
     identity / broker / projection / migration / rollback). Preflight never
     mutates the lane.
  2. consume ``closeout-preflight-completed``:
       - ready      -> emit the fresh-deploy fitness-gate command.
       - not ready  -> emit closeout-completed:BLOCKED (HOLD).
  3. consume ``closeout-fitness-gated``:
       - fit        -> emit the deploy command by REUSING node_redeploy_orchestrator
                       (``redeploy-start``). Prod stays operator-gated: the redeploy
                       orchestrator's own prod-promotion gate runs there, never
                       relaxed here.
       - not fit    -> emit closeout-completed:BLOCKED (HOLD).
  4. consume ``redeploy-completed`` (deploy phase fact):
       - DONE/READY -> emit the proof-matrix command (REUSING node_golden_chain_sweep
                       + node_integration_sweep through the proof-matrix phase).
       - BLOCKED    -> emit closeout-completed:BLOCKED.
       - else       -> emit closeout-completed:FAILED.
  5. consume ``closeout-proof-matrix-completed`` -> emit the terminal
     closeout-completed event carrying ``ModelCloseoutReceipt`` (SHA/image table,
     migration ledger, per-cell verdicts, rollback plan, residual risk,
     recommendation).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_closeout import (
    PROOF_MATRIX_CELLS,
    EnumCloseoutPhase,
    EnumCloseoutRecommendation,
    EnumProofClass,
    EnumProofSet,
    ModelCloseoutReceipt,
)
from omnimarket.events.runtime_deployment import (
    EnumRedeployPhase,
    ModelRedeployCommand,
    ModelRedeployCompletedEvent,
)
from omnimarket.nodes.contract_topics import contract_publish_topics
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

HANDLER_ID = "runtime-closeout-orchestrator"

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_PUBLISH = contract_publish_topics(_CONTRACT)


def _topic_with_suffix(suffix: str) -> str:
    """Resolve exactly one contract publish topic ending with ``suffix``."""
    matches = [t for t in _PUBLISH if t.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"Contract {_CONTRACT} must declare exactly one event_bus.publish_topics "
            f"topic ending in {suffix!r}; found {matches}"
        )
    return matches[0]


TOPIC_CLOSEOUT_PREFLIGHT = _topic_with_suffix("closeout-preflight.v1")
TOPIC_CLOSEOUT_FITNESS_GATE = _topic_with_suffix("closeout-fitness-gate.v1")
TOPIC_REDEPLOY_START = _topic_with_suffix("redeploy-start.v1")
TOPIC_CLOSEOUT_PROOF_MATRIX = _topic_with_suffix("closeout-proof-matrix.v1")
TOPIC_CLOSEOUT_COMPLETED = _topic_with_suffix("closeout-completed.v1")


class HandlerRuntimeCloseoutOrchestrator:
    """Canonical orchestrator: dispatch closeout phases over the bus."""

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Route a closeout lifecycle event to the next bus command."""
        event_type = envelope.event_type or ""
        correlation_id = envelope.correlation_id or uuid4()

        if event_type.endswith("closeout-preflight-completed.v1"):
            events = self._on_preflight(envelope, correlation_id)
        elif event_type.endswith("closeout-fitness-gated.v1"):
            events = self._on_fitness(envelope, correlation_id)
        elif event_type.endswith("redeploy-completed.v1"):
            events = self._on_deploy(envelope, correlation_id)
        elif event_type.endswith("closeout-proof-matrix-completed.v1"):
            events = self._on_proof_matrix(envelope, correlation_id)
        else:
            # Default entrypoint: the closeout-start command.
            events = self._on_start(envelope, correlation_id)

        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=envelope.envelope_id,
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=tuple(events),
        )

    # ------------------------------------------------------------------ phases

    def _on_start(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        """closeout-start -> read-only preflight command."""
        start = _coerce_start(envelope.payload, correlation_id)
        command = ModelCloseoutPreflightCommand(
            correlation_id=start.correlation_id,
            runtime_lane=start.runtime_lane,
            requested_by=start.requested_by,
        )
        return [
            ModelEventEnvelope(
                payload=command,
                correlation_id=start.correlation_id,
                event_type=TOPIC_CLOSEOUT_PREFLIGHT,
            )
        ]

    def _on_preflight(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        """preflight-done -> fitness-gate command (ready) or BLOCKED (not ready)."""
        fact, start = _coerce_preflight(envelope.payload, correlation_id)
        if not fact.ready:
            return self._completed(
                correlation_id=correlation_id,
                start=start,
                final_phase=EnumCloseoutPhase.BLOCKED,
                error_message=f"preflight not ready: {fact.detail}",
                rollback_plan=_rollback_plan(fact.rollback_target),
                residual_risk="lane preflight failed before any deploy",
            )
        command = ModelCloseoutFitnessGateCommand(
            correlation_id=correlation_id,
            runtime_lane=fact.runtime_lane,
            images=fact.images,
        )
        return [
            ModelEventEnvelope(
                payload=command,
                correlation_id=correlation_id,
                event_type=TOPIC_CLOSEOUT_FITNESS_GATE,
            )
        ]

    def _on_fitness(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        """fitness-gated -> deploy via redeploy-start (fit) or BLOCKED (not fit).

        The deploy phase REUSES ``node_redeploy_orchestrator``: prod promotion
        stays operator-gated because the redeploy orchestrator's own
        prod-promotion gate runs there. The closeout never bypasses it.
        """
        fact, start = _coerce_fitness(envelope.payload, correlation_id)
        if not fact.fit:
            return self._completed(
                correlation_id=correlation_id,
                start=start,
                final_phase=EnumCloseoutPhase.BLOCKED,
                error_message=f"fresh-deploy fitness gate: {fact.reason}",
                residual_risk="artifact not fit to deploy fresh",
            )
        # Reuse node_redeploy_orchestrator via the SHARED ModelRedeployCommand
        # (omnimarket.events.runtime_deployment) — never import the redeploy node's
        # private start model across the node boundary. The redeploy orchestrator
        # maps this shared command into its own start command (scope/git_ref
        # defaulted there). Prod promotion stays operator-gated by the redeploy
        # orchestrator's prod-promotion gate; this never relaxes it.
        deploy = ModelRedeployCommand(
            correlation_id=correlation_id,
            requested_at=datetime.now(UTC),
            runtime_lane=start.runtime_lane,
            image_digest=start.image_digest,
            promotion_batch_id=start.promotion_batch_id,
        )
        return [
            ModelEventEnvelope(
                payload=deploy,
                correlation_id=correlation_id,
                event_type=TOPIC_REDEPLOY_START,
            )
        ]

    def _on_deploy(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        """redeploy-completed -> proof-matrix command (DONE) or terminal otherwise."""
        completed, start = _coerce_deploy(envelope.payload, correlation_id)
        phase = completed.final_phase
        if phase in (EnumRedeployPhase.DONE, EnumRedeployPhase.READY):
            cells = _cells_for_proof_set(start.proof_set)
            command = ModelCloseoutProofMatrixCommand(
                correlation_id=correlation_id,
                runtime_lane=start.runtime_lane,
                proof_set=start.proof_set,
                cells=cells,
            )
            return [
                ModelEventEnvelope(
                    payload=command,
                    correlation_id=correlation_id,
                    event_type=TOPIC_CLOSEOUT_PROOF_MATRIX,
                )
            ]
        if phase is EnumRedeployPhase.BLOCKED:
            return self._completed(
                correlation_id=correlation_id,
                start=start,
                final_phase=EnumCloseoutPhase.BLOCKED,
                error_message=f"deploy blocked: {completed.error_message or ''}".strip(),
                residual_risk="deploy gate blocked the promotion",
            )
        return self._completed(
            correlation_id=correlation_id,
            start=start,
            final_phase=EnumCloseoutPhase.FAILED,
            error_message=f"deploy failed: {completed.error_message or ''}".strip(),
            residual_risk="deploy phase did not reach a healthy runtime",
        )

    def _on_proof_matrix(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        """proof-matrix-done -> terminal closeout-completed receipt."""
        fact, start = _coerce_proof_matrix(envelope.payload, correlation_id)
        return self._completed(
            correlation_id=correlation_id,
            start=start,
            final_phase=EnumCloseoutPhase.COMPLETED,
            cell_verdicts=tuple(fact.cell_verdicts),
            residual_risk=fact.detail or "",
        )

    # ------------------------------------------------------------------ helpers

    def _completed(
        self,
        *,
        correlation_id: UUID,
        start: ModelCloseoutStartCommand,
        final_phase: EnumCloseoutPhase,
        cell_verdicts: tuple[Any, ...] = (),
        error_message: str | None = None,
        rollback_plan: str = "",
        residual_risk: str = "",
    ) -> list[ModelEventEnvelope[Any]]:
        """Build the terminal closeout-completed event carrying the receipt."""
        receipt = ModelCloseoutReceipt(
            correlation_id=correlation_id,
            runtime_lane=start.runtime_lane,
            final_phase=final_phase,
            cell_verdicts=cell_verdicts,
            rollback_plan=rollback_plan or _rollback_plan(start.rollback_target),
            residual_risk=residual_risk,
            error_message=error_message,
        )
        if final_phase is EnumCloseoutPhase.COMPLETED:
            recommendation = receipt.recompute_recommendation()
        else:
            recommendation = EnumCloseoutRecommendation.HOLD
        receipt = receipt.model_copy(update={"recommendation": recommendation})
        return [
            ModelEventEnvelope(
                payload=receipt,
                correlation_id=correlation_id,
                event_type=TOPIC_CLOSEOUT_COMPLETED,
            )
        ]


def _cells_for_proof_set(proof_set: EnumProofSet) -> tuple[str, ...]:
    """Resolve the cell names to prove for a proof set.

    ``required`` proves only REQUIRED cells; ``full`` proves the whole matrix.
    """
    if proof_set is EnumProofSet.FULL:
        return tuple(spec.cell for spec in PROOF_MATRIX_CELLS)
    return tuple(
        spec.cell
        for spec in PROOF_MATRIX_CELLS
        if spec.proof_class is EnumProofClass.REQUIRED
    )


def _rollback_plan(rollback_target: str | None) -> str:
    if rollback_target:
        return f"restore previous-good digest {rollback_target}"
    return "no rollback target recorded"


def _coerce_start(payload: Any, correlation_id: UUID) -> ModelCloseoutStartCommand:
    if isinstance(payload, ModelCloseoutStartCommand):
        return payload
    if isinstance(payload, Mapping):
        data = dict(payload)
        data.setdefault("correlation_id", str(correlation_id))
        return ModelCloseoutStartCommand.model_validate(data)
    if hasattr(payload, "model_dump"):
        return ModelCloseoutStartCommand.model_validate(payload.model_dump())
    raise TypeError(
        f"closeout-start payload must be ModelCloseoutStartCommand or a mapping; "
        f"got {type(payload).__name__}"
    )


def _start_from(
    mapping: Mapping[str, Any], correlation_id: UUID
) -> ModelCloseoutStartCommand:
    """Rebuild the echoed start command, or a minimal default when absent."""
    start_raw = mapping.get("start")
    if start_raw is not None:
        return ModelCloseoutStartCommand.model_validate(_as_dict(start_raw))
    return ModelCloseoutStartCommand(correlation_id=correlation_id)


def _coerce_preflight(
    payload: Any, correlation_id: UUID
) -> tuple[ModelCloseoutPreflightFact, ModelCloseoutStartCommand]:
    mapping = _require_mapping(payload, "closeout-preflight-completed")
    fact_raw = mapping.get("preflight", mapping)
    fact = ModelCloseoutPreflightFact.model_validate(_as_dict(fact_raw))
    return fact, _start_from(mapping, correlation_id)


def _coerce_fitness(
    payload: Any, correlation_id: UUID
) -> tuple[ModelCloseoutFitnessGateFact, ModelCloseoutStartCommand]:
    mapping = _require_mapping(payload, "closeout-fitness-gated")
    fact_raw = mapping.get("fitness", mapping)
    fact = ModelCloseoutFitnessGateFact.model_validate(_as_dict(fact_raw))
    return fact, _start_from(mapping, correlation_id)


def _coerce_deploy(
    payload: Any, correlation_id: UUID
) -> tuple[ModelRedeployCompletedEvent, ModelCloseoutStartCommand]:
    mapping = _require_mapping(payload, "redeploy-completed")
    completed_raw = mapping.get("redeploy", mapping)
    completed = ModelRedeployCompletedEvent.model_validate(_as_dict(completed_raw))
    return completed, _start_from(mapping, correlation_id)


def _coerce_proof_matrix(
    payload: Any, correlation_id: UUID
) -> tuple[ModelCloseoutProofMatrixFact, ModelCloseoutStartCommand]:
    mapping = _require_mapping(payload, "closeout-proof-matrix-completed")
    fact_raw = mapping.get("proof_matrix", mapping)
    fact = ModelCloseoutProofMatrixFact.model_validate(_as_dict(fact_raw))
    return fact, _start_from(mapping, correlation_id)


def _require_mapping(payload: Any, label: str) -> Mapping[str, Any]:
    mapping = _as_mapping(payload)
    if mapping is None:
        raise TypeError(
            f"{label} payload must be a mapping or model; got {type(payload).__name__}"
        )
    return mapping


def _as_mapping(candidate: Any) -> Mapping[str, Any] | None:
    if isinstance(candidate, Mapping):
        return candidate
    model_dump = getattr(candidate, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _as_dict(candidate: Any) -> dict[str, Any]:
    mapping = _as_mapping(candidate)
    if mapping is None:
        raise TypeError(f"expected a mapping/model; got {type(candidate).__name__}")
    return dict(mapping)


__all__: list[str] = [
    "HANDLER_ID",
    "TOPIC_CLOSEOUT_COMPLETED",
    "TOPIC_CLOSEOUT_FITNESS_GATE",
    "TOPIC_CLOSEOUT_PREFLIGHT",
    "TOPIC_CLOSEOUT_PROOF_MATRIX",
    "TOPIC_REDEPLOY_START",
    "HandlerRuntimeCloseoutOrchestrator",
]
