# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Branch deploy ORCHESTRATOR handler (OMN-24027).

Composes one branch deploy over the bus and never does I/O: it publishes the next typed command or step event and waits
for the result. The install state reducer (``node_host_install_state_reducer``) holds the install record: each step the
orchestrator takes is published as a ``ModelHostInstallReduceCommand`` carrying the last record the reducer returned, and
the orchestrator sends nothing further for that deploy until the reducer's next record comes back. Steps: record the
request and snapshot, deploy the PR head through the deploy agent, verify build provenance and health through the probe,
and roll back on a failed verification.

The deploy context is held nowhere in memory (OMN-24058): each deploy's :class:`ModelBranchDeployContext` is one row
keyed by the deploy's correlation id, loaded before each message is routed and written back after it under the loaded
version's compare-and-set (:mod:`..orchestration.row_store`). Under the runtime the row persists through the
``state_io`` seam (table ``branch_deploy_state``), so a runtime recreated mid-deploy resumes the deploy; a leg that
loses the race raises and the runtime re-runs it against the winning row.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel

from omnimarket.models.branch_deploy_command import (
    ModelBranchDeployCommandRequest,
    ModelBranchDeployCommandResult,
)
from omnimarket.models.branch_deploy_probe import (
    ModelBranchDeployProbeRequest,
    ModelBranchDeployProbeResult,
)
from omnimarket.models.host_install_state import (
    EnumHostInstallOutcome,
    EnumHostInstallState,
    EnumHostInstallStep,
    ModelHostInstallRecord,
    ModelHostInstallReduceCommand,
    ModelHostInstallStepEvent,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.models.model_branch_deploy_orchestrator import (
    EnumBranchDeployNext,
    EnumBranchDeployPhase,
    ModelBranchDeployCompleted,
    ModelBranchDeployContext,
    ModelBranchDeployRequest,
    ModelDeployJobCompleted,
    ModelDeployJobRejected,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.orchestration.row_store import (
    BranchDeployRowConflictError,
    InMemoryBranchDeployRowStore,
    ProtocolBranchDeployRowStore,
    read_active_state_io_rows,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.overlay import (
    ModelBranchDeployOrchestratorOverlay,
    load_overlay,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.state_codec import (
    shared_state_io_store,
)

HANDLER_ID = "branch-deploy-orchestrator"

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _publish_topic(suffix: str) -> str:
    """Return the one publish topic the node contract declares for ``suffix``; fail loud if absent."""
    with open(_CONTRACT_PATH) as fh:
        data = yaml.safe_load(fh) or {}
    publishes = (data.get("event_bus") or {}).get("publish_topics") or []
    matches = [t for t in publishes if t.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one publish topic ending {suffix!r} in {_CONTRACT_PATH}, found {matches}"
        )
    return str(matches[0])


TOPIC_COMMAND_REQUESTED = _publish_topic("branch-deploy-command-requested.v1")
TOPIC_PROBE_REQUESTED = _publish_topic("branch-deploy-probe-requested.v1")
TOPIC_STEP = _publish_topic("host-install-step.v1")
TOPIC_COMPLETED = _publish_topic("branch-deploy-completed.v1")

logger = logging.getLogger(__name__)

PHASE = EnumBranchDeployPhase
NEXT = EnumBranchDeployNext
STEP = EnumHostInstallStep
OUTCOME = EnumHostInstallOutcome
STATE = EnumHostInstallState


class ModelBranchDeployOutput(BaseModel):
    """One thing the orchestrator publishes: the topic and the typed payload."""

    topic: str
    payload: BaseModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _digest(text: str, algorithm: str) -> str:
    return hashlib.new(algorithm, text.encode("utf-8")).hexdigest()


def _correlation_of(payload: object, fallback: UUID) -> UUID:
    """The payload's own correlation id, or the fallback it was routed under."""
    candidate = getattr(payload, "correlation_id", None)
    if isinstance(candidate, UUID):
        return candidate
    return fallback


@dataclass
class _BranchDeployLeg:
    """The one message being routed: its stored row, the row's key, and whether routing rewrote the row."""

    context: ModelBranchDeployContext | None
    key: UUID
    changed: bool = False

    def written(self) -> ModelBranchDeployContext | None:
        """The row the leg writes back, or None when the leg stores nothing."""
        return self.context if self.changed else None


class HandlerBranchDeployOrchestrator:
    """One leg per consumed message against the deploy's stored row.

    ``decide`` routes one payload; ``handle(envelope)`` wraps its outputs in event envelopes, and ``handle_async`` is
    the runtime entrypoint returning the bare typed events for the contract's ``published_events``.
    """

    def __init__(
        self,
        overlay: ModelBranchDeployOrchestratorOverlay | None = None,
        clock: Callable[[], datetime] | None = None,
        store: ProtocolBranchDeployRowStore | None = None,
    ) -> None:
        self._configured_overlay = overlay
        self._clock = clock if clock is not None else _utc_now
        self._configured_store = store
        self._in_memory_store: InMemoryBranchDeployRowStore | None = None
        self._leg: _BranchDeployLeg | None = None

    @property
    def _overlay(self) -> ModelBranchDeployOrchestratorOverlay:
        if self._configured_overlay is None:
            self._configured_overlay = load_overlay()
        return self._configured_overlay

    @property
    def _local_store(self) -> ProtocolBranchDeployRowStore:
        """The store used when no runtime dispatch has bound its ``state_io`` rows.

        The configured one when a store was given; otherwise in-memory rows built
        on first use (the overlay loads lazily, so the build waits for it).
        """
        if self._configured_store is not None:
            return self._configured_store
        if self._in_memory_store is None:
            self._in_memory_store = InMemoryBranchDeployRowStore(
                self._overlay.context_limit
            )
        return self._in_memory_store

    def _active_store(self) -> ProtocolBranchDeployRowStore:
        """The runtime's ``state_io`` seam when a dispatch bound its rows; the local store otherwise."""
        if read_active_state_io_rows() is not None:
            return shared_state_io_store()
        return self._local_store

    @property
    def known_deploys(self) -> tuple[UUID, ...]:
        """The deploys holding an unfinished row in the local in-memory store.

        The local store only: under the runtime's ``state_io`` seam the rows live in the ``branch_deploy_state`` table
        and are not readable here.
        """
        store = self._in_memory_store
        return store.active_keys() if store is not None else ()

    @property
    def _leg_in_flight(self) -> _BranchDeployLeg:
        """The leg ``decide`` is routing; the routing methods run only inside it."""
        leg = self._leg
        if leg is None:
            msg = "no branch deploy leg is in flight"
            raise RuntimeError(msg)
        return leg

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        correlation_id = envelope.correlation_id or uuid4()
        outputs = self.decide(envelope.payload, correlation_id)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=envelope.envelope_id,
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=tuple(
                ModelEventEnvelope(
                    payload=out.payload,
                    correlation_id=correlation_id,
                    event_type=out.topic,
                )
                for out in outputs
            ),
        )

    async def handle_async(self, request: object) -> ModelHandlerOutput[None]:
        """Runtime entrypoint: the same routing, its outputs as bare typed events.

        The compose runtime prefers this entrypoint when the class declares it and publishes ``events`` through the
        contract's ``published_events`` map; for this ``state_io`` node the outbox captures them in the row's
        compare-and-set (the PR landing orchestrator's pattern, OMN-20127).
        """
        correlation_id = _correlation_of(request, uuid4())
        outputs = self.decide(request, correlation_id)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=tuple(out.payload for out in outputs),
        )

    def decide(
        self, payload: object, correlation_id: UUID
    ) -> tuple[ModelBranchDeployOutput, ...]:
        """Route one consumed payload as one leg against its stored row.

        ``correlation_id`` is only the fallback: every consumed payload carries its own. The row is loaded before the
        routing and written back under the loaded version when the routing changed it; a lost race raises
        :class:`BranchDeployRowConflictError` and the runtime re-runs this leg. A leg that ends with no row and
        nothing changed stores nothing: an unknown deploy is ignored, published nowhere.
        """
        key = _correlation_of(payload, correlation_id)
        store = self._active_store()
        row, version = store.load(str(key))
        leg = _BranchDeployLeg(context=row, key=key)
        self._leg = leg
        try:
            outputs = self._route(payload)
            written = leg.written()
        finally:
            self._leg = None
        if written is not None and not store.compare_and_set(
            str(key), version, written
        ):
            raise BranchDeployRowConflictError(
                f"compare-and-set on {leg.key} lost the race; the leg is re-run"
            )
        return outputs

    def _route(self, payload: object) -> tuple[ModelBranchDeployOutput, ...]:
        """The isinstance routing of one payload against the leg's row."""
        if isinstance(payload, ModelBranchDeployRequest):
            return self._on_request(payload)
        if isinstance(payload, ModelBranchDeployCommandResult):
            return self._on_command_result(payload)
        if isinstance(payload, ModelDeployJobCompleted):
            return self._on_job_completed(payload)
        if isinstance(payload, ModelDeployJobRejected):
            return self._on_job_rejected(payload)
        if isinstance(payload, ModelBranchDeployProbeResult):
            return self._on_probe_result(payload)
        if isinstance(payload, ModelHostInstallRecord):
            return self._on_record(payload)
        logger.warning("branch deploy orchestrator ignored %s", type(payload).__name__)
        return ()

    # -- the stored row ------------------------------------------------------------------------------------------

    def _remember(self, key: UUID, context: ModelBranchDeployContext) -> None:
        """Hold the row the leg writes back; ``key`` names it (the leg already carries it)."""
        leg = self._leg_in_flight
        leg.context = context
        leg.changed = True

    def _awaiting(
        self, key: UUID, phase: EnumBranchDeployPhase
    ) -> ModelBranchDeployContext | None:
        context = self._leg_in_flight.context
        if context is None or context.phase is not phase:
            logger.warning(
                "branch deploy orchestrator ignored a result for %s in phase %s",
                key,
                phase.value,
            )
            return None
        return context

    # -- builders ------------------------------------------------------------------------------------------------

    def _cut(self, text: str) -> str:
        return text[: self._overlay.detail_max_chars]

    def _digest(self, text: str) -> str:
        return _digest(text, self._overlay.digest_algorithm)

    def _command(
        self, request: ModelBranchDeployRequest, mode: Literal["deploy", "rollback"]
    ) -> ModelBranchDeployOutput:
        if mode == "deploy":
            git_ref = request.head_sha
            image_ref = ""
        else:
            git_ref = request.rollback_head_sha
            image_ref = request.rollback_image
        return ModelBranchDeployOutput(
            topic=TOPIC_COMMAND_REQUESTED,
            payload=ModelBranchDeployCommandRequest(
                correlation_id=request.correlation_id,
                mode=mode,
                runtime_lane=request.runtime_lane,
                repo=request.repo,
                pr_number=request.pr_number,
                git_ref=git_ref,
                image_ref=image_ref,
                rollback_actor=request.actor,
                rollback_reason=request.rollback_reason,
                requested_by=request.actor,
            ),
        )

    def _probe(
        self, request: ModelBranchDeployRequest, phase: Literal["deploy", "rollback"]
    ) -> ModelBranchDeployOutput:
        expected = request.head_sha if phase == "deploy" else request.rollback_head_sha
        return ModelBranchDeployOutput(
            topic=TOPIC_PROBE_REQUESTED,
            payload=ModelBranchDeployProbeRequest(
                correlation_id=request.correlation_id,
                phase=phase,
                expected_sha=expected,
            ),
        )

    def _step(
        self,
        context: ModelBranchDeployContext,
        step: EnumHostInstallStep,
        outcome: EnumHostInstallOutcome,
        **cells: str,
    ) -> ModelHostInstallStepEvent:
        request = context.request
        return ModelHostInstallStepEvent(
            correlation_id=request.correlation_id,
            host=request.host,
            unit=request.unit,
            step=step,
            outcome=outcome,
            actor=request.actor,
            at=self._clock(),
            **cells,
        )

    def _publish_step(
        self, context: ModelBranchDeployContext, event: ModelHostInstallStepEvent
    ) -> ModelBranchDeployOutput:
        return ModelBranchDeployOutput(
            topic=TOPIC_STEP,
            payload=ModelHostInstallReduceCommand(prior=context.prior, event=event),
        )

    def _advance(
        self,
        context: ModelBranchDeployContext,
        event: ModelHostInstallStepEvent,
        next_action: EnumBranchDeployNext,
        *,
        then: tuple[ModelHostInstallStepEvent, ...] = (),
        **updates: object,
    ) -> tuple[ModelBranchDeployOutput, ...]:
        """Publish ``event``; the rest waits for the reducer's record."""
        key = context.request.correlation_id
        self._remember(
            key,
            context.model_copy(
                update={"next_action": next_action, "pending_steps": then, **updates}
            ),
        )
        return (self._publish_step(context, event),)

    def _finish(
        self,
        request: ModelBranchDeployRequest,
        record: ModelHostInstallRecord | None,
        detail: str,
        observed_sha: str = "",
    ) -> ModelBranchDeployOutput:
        state = record.state if record is not None else STATE.FAILED
        outcome = (
            "deployed"
            if state is STATE.RUN_OK
            else "rolled_back"
            if state is STATE.ROLLED_BACK
            else "failed"
        )
        return ModelBranchDeployOutput(
            topic=TOPIC_COMPLETED,
            payload=ModelBranchDeployCompleted(
                correlation_id=request.correlation_id,
                host=request.host,
                unit=request.unit,
                final_state=state,
                outcome=outcome,
                head_sha=request.head_sha,
                observed_sha=observed_sha,
                rollback_failures=record.rollback_failures if record else 0,
                detail=self._cut(record.failure_detail or detail if record else detail),
            ),
        )

    # -- the six consumed payloads -------------------------------------------------------------------------------

    def _on_request(
        self, request: ModelBranchDeployRequest
    ) -> tuple[ModelBranchDeployOutput, ...]:
        self._remember(
            request.correlation_id,
            ModelBranchDeployContext(
                request=request,
                phase=PHASE.RECORDING,
                next_action=NEXT.SNAPSHOT,
            ),
        )
        detail = self._cut(
            f"{request.repo}#{request.pr_number}@{request.head_sha} to lane {request.runtime_lane}; "
            f"rollback image {request.rollback_image} at {request.rollback_head_sha}: {request.rollback_reason}"
        )
        event = self._step(
            ModelBranchDeployContext(request=request, phase=PHASE.RECORDING),
            STEP.REQUEST,
            OUTCOME.OK,
            render_digest=self._digest(request.head_sha),
            detail=detail,
        )
        return (
            self._publish_step(
                ModelBranchDeployContext(request=request, phase=PHASE.RECORDING),
                event,
            ),
        )

    def _on_command_result(
        self, result: ModelBranchDeployCommandResult
    ) -> tuple[ModelBranchDeployOutput, ...]:
        key = result.correlation_id
        phase = PHASE.COMMAND if result.mode == "deploy" else PHASE.ROLLBACK_COMMAND
        context = self._awaiting(key, phase)
        if context is None:
            return ()
        if result.status == "published":
            target = PHASE.JOB if result.mode == "deploy" else PHASE.ROLLBACK_JOB
            self._remember(key, context.model_copy(update={"phase": target}))
            return ()
        detail = self._cut(f"deploy command {result.status}: {result.detail}")
        if result.mode == "deploy":
            event = self._step(context, STEP.INSTALL, OUTCOME.FAILED, detail=detail)
        else:
            event = self._step(context, STEP.ROLLBACK, OUTCOME.FAILED, detail=detail)
        return self._advance(context, event, NEXT.FINISH, phase=PHASE.RECORDING)

    def _on_job_completed(
        self, result: ModelDeployJobCompleted
    ) -> tuple[ModelBranchDeployOutput, ...]:
        key = result.correlation_id
        known = self._leg_in_flight.context
        if known is not None and known.phase is PHASE.ROLLBACK_JOB:
            return self._on_rollback_job(
                known, result.status == "success", self._failed_detail(result)
            )
        context = self._awaiting(key, PHASE.JOB)
        if context is None:
            return ()
        if result.status == "failed":
            failed = [
                f"{name}={value}"
                for name, value in result.phase_results.items()
                if value == "failed"
            ]
            detail = self._cut(
                "deploy job failed"
                + (" " + "; ".join(failed) if failed else "")
                + (f" {result.errors[0]}" if result.errors else "")
            )
            event = self._step(context, STEP.INSTALL, OUTCOME.FAILED, detail=detail)
            return self._advance(context, event, NEXT.FINISH, phase=PHASE.RECORDING)
        event = self._step(context, STEP.INSTALL, OUTCOME.OK)
        return self._advance(context, event, NEXT.SEND_PROBE, phase=PHASE.RECORDING)

    def _failed_detail(self, result: ModelDeployJobCompleted) -> str:
        failed = [
            f"{name}={value}"
            for name, value in result.phase_results.items()
            if value == "failed"
        ]
        return self._cut(
            "rollback job failed"
            + (" " + "; ".join(failed) if failed else "")
            + (f" {result.errors[0]}" if result.errors else "")
        )

    def _on_rollback_job(
        self, context: ModelBranchDeployContext, ok: bool, detail: str
    ) -> tuple[ModelBranchDeployOutput, ...]:
        """The rollback job ended: probe the runtime when it succeeded, otherwise record the failed rollback."""
        key = context.request.correlation_id
        if ok:
            self._remember(
                key, context.model_copy(update={"phase": PHASE.ROLLBACK_PROBE})
            )
            return (self._probe(context.request, "rollback"),)
        event = self._step(
            context, STEP.ROLLBACK, OUTCOME.FAILED, detail=self._cut(detail)
        )
        return self._advance(context, event, NEXT.FINISH, phase=PHASE.RECORDING)

    def _on_job_rejected(
        self, result: ModelDeployJobRejected
    ) -> tuple[ModelBranchDeployOutput, ...]:
        key = result.correlation_id
        known = self._leg_in_flight.context
        if known is not None and known.phase is PHASE.ROLLBACK_JOB:
            return self._on_rollback_job(
                known, False, f"rollback job rejected: {result.reason}"
            )
        context = self._awaiting(key, PHASE.JOB)
        if context is None:
            return ()
        detail = self._cut(f"deploy job rejected: {result.reason}")
        event = self._step(context, STEP.INSTALL, OUTCOME.FAILED, detail=detail)
        return self._advance(context, event, NEXT.FINISH, phase=PHASE.RECORDING)

    def _on_probe_result(
        self, result: ModelBranchDeployProbeResult
    ) -> tuple[ModelBranchDeployOutput, ...]:
        key = result.correlation_id
        if result.phase == "deploy":
            context = self._awaiting(key, PHASE.PROBE)
            if context is None:
                return ()
            if result.status == "unreadable":
                event = self._step(
                    context,
                    STEP.VERIFY,
                    OUTCOME.FAILED,
                    detail=self._cut(result.detail),
                )
                return self._advance(
                    context, event, NEXT.SEND_ROLLBACK, phase=PHASE.RECORDING
                )
            if not result.healthy:
                event = self._step(
                    context,
                    STEP.VERIFY,
                    OUTCOME.FAILED,
                    detail=self._cut(f"runtime unhealthy at {result.observed_sha}"),
                )
                return self._advance(
                    context, event, NEXT.SEND_ROLLBACK, phase=PHASE.RECORDING
                )
            event = self._step(
                context,
                STEP.VERIFY,
                OUTCOME.OK,
                installed_digest=self._digest(result.observed_sha),
            )
            return self._advance(
                context,
                event,
                NEXT.AFTER_VERIFY,
                phase=PHASE.RECORDING,
                observed_sha=result.observed_sha,
            )
        context = self._awaiting(key, PHASE.ROLLBACK_PROBE)
        if context is None:
            return ()
        if result.status == "read":
            event = self._step(
                context,
                STEP.ROLLBACK,
                OUTCOME.OK,
                installed_digest=self._digest(result.observed_sha),
            )
        else:
            event = self._step(
                context, STEP.ROLLBACK, OUTCOME.FAILED, detail=self._cut(result.detail)
            )
        return self._advance(context, event, NEXT.FINISH, phase=PHASE.RECORDING)

    def _on_record(
        self, record: ModelHostInstallRecord
    ) -> tuple[ModelBranchDeployOutput, ...]:
        key = record.correlation_id
        context = self._leg_in_flight.context
        if context is None or context.phase is PHASE.DONE:
            logger.warning("branch deploy orchestrator ignored a record for %s", key)
            return ()
        context = context.model_copy(update={"prior": record})
        self._remember(key, context)
        if context.pending_steps:
            first, rest = context.pending_steps[0], context.pending_steps[1:]
            self._remember(key, context.model_copy(update={"pending_steps": rest}))
            return (self._publish_step(context, first),)
        request = context.request
        action = context.next_action
        if action is NEXT.FINISH:
            self._remember(key, context.model_copy(update={"phase": PHASE.DONE}))
            return (self._finish(request, record, "", context.observed_sha),)
        if action is NEXT.SNAPSHOT:
            snapshot = self._step(
                context,
                STEP.SNAPSHOT,
                OUTCOME.OK,
                rollback_target_digest=self._digest(request.rollback_head_sha),
            )
            started = self._step(context, STEP.INSTALL, OUTCOME.STARTED)
            return self._advance(
                context,
                snapshot,
                NEXT.INSTALL_START,
                phase=PHASE.RECORDING,
                then=(started,),
            )
        if action in (NEXT.INSTALL_START, NEXT.SEND_DEPLOY):
            self._remember(key, context.model_copy(update={"phase": PHASE.COMMAND}))
            return (self._command(request, "deploy"),)
        if action is NEXT.SEND_PROBE:
            self._remember(key, context.model_copy(update={"phase": PHASE.PROBE}))
            return (self._probe(request, "deploy"),)
        if action is NEXT.AFTER_VERIFY:
            if record.state is STATE.VERIFIED:
                event = self._step(
                    context,
                    STEP.RUN,
                    OUTCOME.OK,
                    detail=self._cut(
                        "build provenance and health read at the deployed head"
                    ),
                )
                return self._advance(context, event, NEXT.FINISH, phase=PHASE.RECORDING)
            self._remember(
                key, context.model_copy(update={"phase": PHASE.ROLLBACK_COMMAND})
            )
            return (self._command(request, "rollback"),)
        if action is NEXT.SEND_ROLLBACK:
            self._remember(
                key, context.model_copy(update={"phase": PHASE.ROLLBACK_COMMAND})
            )
            return (self._command(request, "rollback"),)
        self._remember(key, context)
        return ()
