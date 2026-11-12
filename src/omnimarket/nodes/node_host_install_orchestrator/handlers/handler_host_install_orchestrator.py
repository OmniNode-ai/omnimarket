# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host unit install ORCHESTRATOR handler (OMN-23957).

Composes one install over the bus and never does I/O: it publishes the next command or step event and waits for the
result. The reducer (``node_host_install_state_reducer``) holds the install record: each step the orchestrator takes is
published as a ``ModelHostInstallReduceCommand`` carrying the last record the reducer returned, and the orchestrator
sends nothing further for that install until the reducer's next record comes back (it never sends a second step with a
stale prior). Steps: dry-run render and install through the registry installer effect, snapshot, read and restore through
the file effect, the readback compute, one run read (registry mode status), and an automatic rollback after a failed
install, verify or run.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel

from omnimarket.nodes.node_host_install_file_effect.models.model_host_install_file import (
    ModelHostInstallFileRequest,
    ModelHostInstallFileResult,
)
from omnimarket.nodes.node_host_install_orchestrator.models.model_host_install_orchestrator import (
    EnumHostInstallNext,
    EnumHostInstallPhase,
    ModelHostInstallCompleted,
    ModelHostInstallContext,
    ModelHostInstallRequest,
)
from omnimarket.nodes.node_host_install_orchestrator.overlay import (
    ModelHostInstallOrchestratorOverlay,
)
from omnimarket.nodes.node_host_install_readback_compute.digest import digest_text
from omnimarket.nodes.node_host_install_readback_compute.models.model_host_install_readback import (
    ModelHostInstallReadbackRequest,
    ModelHostInstallReadbackResult,
)
from omnimarket.nodes.node_host_install_state_reducer.models import (
    EnumHostInstallOutcome,
    EnumHostInstallState,
    EnumHostInstallStep,
    ModelHostInstallRecord,
    ModelHostInstallReduceCommand,
    ModelHostInstallStepEvent,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsResult,
)

HANDLER_ID = "host-install-orchestrator"

TOPIC_REGISTRY_REQUESTED = "onex.cmd.omnimarket.host-registry-unit-requested.v1"
TOPIC_FILE_REQUESTED = "onex.cmd.omnimarket.host-install-file-requested.v1"
TOPIC_READBACK_REQUESTED = "onex.cmd.omnimarket.host-install-readback-requested.v1"
TOPIC_STEP = "onex.evt.omnimarket.host-install-step.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.host-install-completed.v1"

logger = logging.getLogger(__name__)

PHASE = EnumHostInstallPhase
NEXT = EnumHostInstallNext
STEP = EnumHostInstallStep
OUTCOME = EnumHostInstallOutcome


class ModelHostInstallOutput(BaseModel):
    """One thing the orchestrator publishes: the topic and the typed payload."""

    topic: str
    payload: BaseModel


class HandlerHostInstallOrchestrator:
    """``handle(envelope)`` routes one consumed payload; ``decide`` is the same routing without the runtime wrapper."""

    def __init__(
        self,
        overlay: ModelHostInstallOrchestratorOverlay,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._clock = clock
        self._contexts: OrderedDict[UUID, ModelHostInstallContext] = OrderedDict()

    @property
    def known_installs(self) -> tuple[UUID, ...]:
        return tuple(self._contexts)

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

    def decide(
        self, payload: object, correlation_id: UUID
    ) -> tuple[ModelHostInstallOutput, ...]:
        """Route one consumed payload. ``correlation_id`` is the envelope's: the registry result carries none of its own."""
        if isinstance(payload, ModelHostInstallRequest):
            return self._on_request(payload)
        if isinstance(payload, ModelRegistryUnitsResult):
            return self._on_registry_result(correlation_id, payload)
        if isinstance(payload, ModelHostInstallFileResult):
            return self._on_file_result(payload)
        if isinstance(payload, ModelHostInstallReadbackResult):
            return self._on_readback_result(payload)
        if isinstance(payload, ModelHostInstallRecord):
            return self._on_record(payload)
        logger.warning("host install orchestrator ignored %s", type(payload).__name__)
        return ()

    # -- working memory ------------------------------------------------------------------------------------------

    def _remember(self, key: UUID, context: ModelHostInstallContext) -> None:
        self._contexts[key] = context
        self._contexts.move_to_end(key)
        while len(self._contexts) > self._overlay.context_limit:
            self._contexts.popitem(last=False)

    def _awaiting(
        self, key: UUID, phase: EnumHostInstallPhase
    ) -> ModelHostInstallContext | None:
        context = self._contexts.get(key)
        if context is None or context.phase is not phase:
            logger.warning(
                "host install orchestrator ignored a result for %s in phase %s",
                key,
                phase.value,
            )
            return None
        return context

    # -- builders ------------------------------------------------------------------------------------------------

    def _paths(self, request: ModelHostInstallRequest) -> tuple[str, ...]:
        templates = (
            self._overlay.systemd_paths
            if request.platform == "systemd"
            else self._overlay.launchd_paths
        )
        return tuple(
            t.format(home=request.registry_request.home, label=request.label)
            for t in templates
        )

    def _cut(self, text: str) -> str:
        return text[: self._overlay.detail_max_chars]

    def _registry(
        self, request: ModelHostInstallRequest, mode: str
    ) -> ModelHostInstallOutput:
        return ModelHostInstallOutput(
            topic=TOPIC_REGISTRY_REQUESTED,
            payload=request.registry_request.model_copy(update={"mode": mode}),
        )

    def _file(
        self,
        request: ModelHostInstallRequest,
        mode: Literal["snapshot", "read", "restore"],
    ) -> ModelHostInstallOutput:
        return ModelHostInstallOutput(
            topic=TOPIC_FILE_REQUESTED,
            payload=ModelHostInstallFileRequest(
                correlation_id=request.correlation_id,
                host=request.host,
                unit=request.unit,
                mode=mode,
                paths=self._paths(request),
            ),
        )

    def _step(
        self,
        context: ModelHostInstallContext,
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
        self, context: ModelHostInstallContext, event: ModelHostInstallStepEvent
    ) -> ModelHostInstallOutput:
        return ModelHostInstallOutput(
            topic=TOPIC_STEP,
            payload=ModelHostInstallReduceCommand(prior=context.prior, event=event),
        )

    def _advance(
        self,
        context: ModelHostInstallContext,
        event: ModelHostInstallStepEvent,
        next_action: EnumHostInstallNext,
        *,
        then: tuple[ModelHostInstallStepEvent, ...] = (),
        **updates: object,
    ) -> tuple[ModelHostInstallOutput, ...]:
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
        request: ModelHostInstallRequest,
        record: ModelHostInstallRecord | None,
        detail: str,
    ) -> ModelHostInstallOutput:
        state = record.state if record is not None else EnumHostInstallState.FAILED
        outcome = (
            "installed"
            if state is EnumHostInstallState.RUN_OK
            else "rolled_back"
            if state is EnumHostInstallState.ROLLED_BACK
            else "failed"
        )
        return ModelHostInstallOutput(
            topic=TOPIC_COMPLETED,
            payload=ModelHostInstallCompleted(
                correlation_id=request.correlation_id,
                host=request.host,
                unit=request.unit,
                final_state=state,
                outcome=outcome,
                render_digest=record.render_digest if record else "",
                installed_digest=record.installed_digest if record else "",
                rollback_target_digest=record.rollback_target_digest if record else "",
                detail=self._cut(record.failure_detail or detail if record else detail),
            ),
        )

    # -- the five consumed payloads ------------------------------------------------------------------------------

    def _on_request(
        self, request: ModelHostInstallRequest
    ) -> tuple[ModelHostInstallOutput, ...]:
        self._remember(
            request.correlation_id,
            ModelHostInstallContext(request=request, phase=PHASE.RENDER),
        )
        return (self._registry(request, "dry-run"),)

    def _on_registry_result(
        self, key: UUID, result: ModelRegistryUnitsResult
    ) -> tuple[ModelHostInstallOutput, ...]:
        context = self._contexts.get(key)
        if context is None:
            logger.warning(
                "host install orchestrator ignored a registry result for %s", key
            )
            return ()
        ok = result.status == "success" and result.exit_code == 0
        detail = self._cut(result.stderr or result.stdout or result.status)
        if context.phase is PHASE.RENDER:
            if not ok or not result.rendered:
                self._contexts.pop(key, None)
                return (self._finish(context.request, None, detail or "render_failed"),)
            digest = digest_text(result.rendered, self._overlay.digest_algorithm)
            event = self._step(context, STEP.REQUEST, OUTCOME.OK, render_digest=digest)
            return self._advance(
                context,
                event,
                NEXT.SNAPSHOT,
                render_text=result.rendered,
                render_digest=digest,
            )
        if context.phase is PHASE.INSTALL:
            if ok:
                return self._advance(
                    context, self._step(context, STEP.INSTALL, OUTCOME.OK), NEXT.READ
                )
            return self._advance(
                context,
                self._step(context, STEP.INSTALL, OUTCOME.FAILED, detail=detail),
                NEXT.RESTORE,
            )
        if context.phase is PHASE.RUN:
            if ok:
                return self._advance(
                    context, self._step(context, STEP.RUN, OUTCOME.OK), NEXT.FINISH
                )
            return self._advance(
                context,
                self._step(context, STEP.RUN, OUTCOME.FAILED, detail=detail),
                NEXT.RESTORE,
            )
        logger.warning(
            "host install orchestrator ignored a registry result in phase %s",
            context.phase.value,
        )
        return ()

    def _on_file_result(
        self, result: ModelHostInstallFileResult
    ) -> tuple[ModelHostInstallOutput, ...]:
        key = result.correlation_id
        context = self._contexts.get(key)
        if context is None:
            logger.warning(
                "host install orchestrator ignored a file result for %s", key
            )
            return ()
        detail = self._cut(result.detail)
        ok = result.status == "success"
        if context.phase is PHASE.SNAPSHOT and result.mode == "snapshot":
            if not ok:
                return self._advance(
                    context,
                    self._step(context, STEP.SNAPSHOT, OUTCOME.FAILED, detail=detail),
                    NEXT.FINISH,
                )
            started = self._step(context, STEP.INSTALL, OUTCOME.STARTED)
            return self._advance(
                context,
                self._step(
                    context,
                    STEP.SNAPSHOT,
                    OUTCOME.OK,
                    rollback_target_digest=result.digest,
                ),
                NEXT.INSTALL,
                then=(started,),
            )
        if context.phase is PHASE.READ and result.mode == "read":
            if not ok:
                return self._advance(
                    context,
                    self._step(context, STEP.VERIFY, OUTCOME.FAILED, detail=detail),
                    NEXT.RESTORE,
                )
            self._remember(key, context.model_copy(update={"phase": PHASE.READBACK}))
            return (
                ModelHostInstallOutput(
                    topic=TOPIC_READBACK_REQUESTED,
                    payload=ModelHostInstallReadbackRequest(
                        correlation_id=key,
                        host=context.request.host,
                        unit=context.request.unit,
                        render_text=context.render_text,
                        installed_present=result.present,
                        installed_text=result.text,
                    ),
                ),
            )
        if context.phase is PHASE.ROLLBACK and result.mode == "restore":
            if ok:
                event = self._step(
                    context, STEP.ROLLBACK, OUTCOME.OK, installed_digest=result.digest
                )
            else:
                event = self._step(
                    context, STEP.ROLLBACK, OUTCOME.FAILED, detail=detail
                )
            return self._advance(context, event, NEXT.FINISH)
        logger.warning(
            "host install orchestrator ignored a %s file result in phase %s",
            result.mode,
            context.phase.value,
        )
        return ()

    def _on_readback_result(
        self, result: ModelHostInstallReadbackResult
    ) -> tuple[ModelHostInstallOutput, ...]:
        context = self._awaiting(result.correlation_id, PHASE.READBACK)
        if context is None:
            return ()
        if result.status == "match":
            return self._advance(
                context,
                self._step(
                    context,
                    STEP.VERIFY,
                    OUTCOME.OK,
                    installed_digest=result.installed_digest,
                ),
                NEXT.STATUS,
            )
        if result.status == "mismatch":
            # The reducer, not this node, moves a differing digest to failed: it is sent as read.
            return self._advance(
                context,
                self._step(
                    context,
                    STEP.VERIFY,
                    OUTCOME.OK,
                    installed_digest=result.installed_digest,
                    detail=self._cut(result.detail),
                ),
                NEXT.RESTORE,
            )
        return self._advance(
            context,
            self._step(
                context, STEP.VERIFY, OUTCOME.FAILED, detail=self._cut(result.detail)
            ),
            NEXT.RESTORE,
        )

    def _on_record(
        self, record: ModelHostInstallRecord
    ) -> tuple[ModelHostInstallOutput, ...]:
        key = record.correlation_id
        context = self._contexts.get(key)
        if context is None:
            logger.warning("host install orchestrator ignored a record for %s", key)
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
            self._contexts.pop(key, None)
            return (self._finish(request, record, ""),)
        if action is NEXT.SNAPSHOT:
            self._remember(key, context.model_copy(update={"phase": PHASE.SNAPSHOT}))
            return (self._file(request, "snapshot"),)
        if action is NEXT.INSTALL:
            self._remember(key, context.model_copy(update={"phase": PHASE.INSTALL}))
            return (self._registry(request, "install"),)
        if action is NEXT.READ:
            self._remember(key, context.model_copy(update={"phase": PHASE.READ}))
            return (self._file(request, "read"),)
        if action is NEXT.STATUS:
            self._remember(key, context.model_copy(update={"phase": PHASE.RUN}))
            return (self._registry(request, "status"),)
        self._remember(key, context.model_copy(update={"phase": PHASE.ROLLBACK}))
        return (self._file(request, "restore"),)
