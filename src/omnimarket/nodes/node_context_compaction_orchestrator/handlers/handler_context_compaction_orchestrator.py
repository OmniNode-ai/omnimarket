# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer one compaction command with a package or a native fallback (OMN-21113, TA.11).

When a barrier wait is wired, the build runs only after the session barrier the
command carries is met: a barrier not met at the deadline (or not yet projected)
falls back ``source_not_caught_up``, a session with content capture off
``source_disabled``, and a command carrying no barrier ``source_incomplete``.
A built package is published as ``prepared``, then a fresh ``activation_id`` is
appended to the identity trailer (the last message), ``installed_messages_hash``
is taken over the whole list, and ``activated`` and the ``install`` reply carry
it. Any failure before activation, typed or raised, replies ``native`` and
publishes ``fallback`` with the failure class and the command's
``invocation_id``. No partial package is ever replied.
Receipts carry the pack build's wall time and the model-written section's share (TA.12).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID, uuid4

from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass,
    ModelContextCompactionActivated,
    ModelContextCompactionBuildOutcome,
    ModelContextCompactionBuiltPackage,
    ModelContextCompactionFallback,
    ModelContextCompactionPackageReply,
    ModelContextCompactionPrepared,
    ModelContextCompactionRequested,
    ModelContextCompactionResult,
    installed_messages_hash,
)
from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)
from omnimarket.protocols.protocol_context_compaction_barrier_wait import (
    ProtocolContextCompactionBarrierWait,
)
from omnimarket.protocols.protocol_context_compaction_pack_build import (
    ProtocolContextCompactionPackBuild,
)


class _UnwiredPackBuild:
    """The default until the context-pack orchestrator is wired: every build is unavailable."""

    def build(
        self, request: ModelContextCompactionRequested
    ) -> ModelContextCompactionBuildOutcome:
        return ModelContextCompactionBuildOutcome(
            failure_class=EnumContextCompactionFailureClass.PACK_BUILDER_UNAVAILABLE
        )


def _with_activation_line(
    messages: tuple[ModelContextPackRenderMessage, ...], activation_id: UUID
) -> tuple[ModelContextPackRenderMessage, ...]:
    trailer = messages[-1]
    return (
        *messages[:-1],
        ModelContextPackRenderMessage(
            role=trailer.role,
            text=f"{trailer.text}\nactivation_id: {activation_id}",
        ),
    )


class HandlerContextCompactionOrchestrator:
    """handle(ModelContextCompactionRequested) -> ModelContextCompactionResult."""

    def __init__(
        self,
        pack_build: ProtocolContextCompactionPackBuild | None = None,
        activation_ids: Callable[[], UUID] = uuid4,
        barrier_wait: ProtocolContextCompactionBarrierWait | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._pack_build = pack_build if pack_build is not None else _UnwiredPackBuild()
        self._barrier_wait = barrier_wait
        self._activation_ids = activation_ids
        self._clock = clock if clock is not None else time.perf_counter

    def handle(
        self, request: ModelContextCompactionRequested
    ) -> ModelContextCompactionResult:
        build_ms: float | None = None
        try:
            barrier_failure = self._barrier_failure(request)
            if barrier_failure is not None:
                return self._native(request, barrier_failure)
            started = self._clock()
            try:
                outcome = self._pack_build.build(request)
            finally:
                build_ms = (self._clock() - started) * 1000.0
        except Exception:
            return self._native(
                request,
                EnumContextCompactionFailureClass.BUILD_ERROR,
                build_ms=build_ms,
            )
        if outcome.package is None:
            return self._native(
                request,
                outcome.failure_class
                or EnumContextCompactionFailureClass.PACK_BUILD_FAILED,
                build_ms=build_ms,
            )
        return self._install(request, outcome.package, build_ms=build_ms)

    async def handle_async(
        self, request: ModelContextCompactionRequested
    ) -> ModelHandlerOutput[None]:
        """Runtime entrypoint: the emissions as publishable handler output.

        The runtime publishes ``events`` through the contract's
        ``published_events`` map, correlated by the command's ``invocation_id``.
        """
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=request.invocation_id,
            handler_id="node_context_compaction_orchestrator.compaction",
            events=self.handle(request).emissions(),
        )

    def _barrier_failure(
        self, request: ModelContextCompactionRequested
    ) -> EnumContextCompactionFailureClass | None:
        """The failure class when the session barrier is not met; None to build."""
        if self._barrier_wait is None:
            return None
        if request.barrier_id is None:
            return EnumContextCompactionFailureClass.SOURCE_INCOMPLETE
        verdict = self._barrier_wait.wait(request)
        if verdict.outcome == "met":
            return None
        if verdict.outcome == "disabled":
            return EnumContextCompactionFailureClass.SOURCE_DISABLED
        return EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP

    def _native(
        self,
        request: ModelContextCompactionRequested,
        failure_class: EnumContextCompactionFailureClass,
        *,
        build_ms: float | None = None,
    ) -> ModelContextCompactionResult:
        return ModelContextCompactionResult(
            reply=ModelContextCompactionPackageReply(
                invocation_id=request.invocation_id,
                action=EnumClientVerdictAction.NATIVE,
            ),
            fallback=ModelContextCompactionFallback(
                invocation_id=request.invocation_id,
                session_id=request.session_id,
                failure_class=failure_class,
                build_ms=build_ms,
            ),
        )

    def _install(
        self,
        request: ModelContextCompactionRequested,
        package: ModelContextCompactionBuiltPackage,
        *,
        build_ms: float,
    ) -> ModelContextCompactionResult:
        activation_id = self._activation_ids()
        messages = _with_activation_line(package.messages, activation_id)
        installed_hash = installed_messages_hash(messages)
        return ModelContextCompactionResult(
            reply=ModelContextCompactionPackageReply(
                invocation_id=request.invocation_id,
                action=EnumClientVerdictAction.INSTALL,
                messages=messages,
                installed_messages_hash=installed_hash,
            ),
            prepared=ModelContextCompactionPrepared(
                candidate_id=package.candidate_id,
                session_id=request.session_id,
                input_messages_hash=request.input_messages_hash,
                assembly_hash=package.assembly_hash,
                version_vector_hash=package.version_vector_hash,
                profile_name=package.profile_name,
                profile_content_hash=package.profile_content_hash,
                build_ms=build_ms,
                model_section_ms=package.model_section_ms,
            ),
            activated=ModelContextCompactionActivated(
                activation_id=activation_id,
                candidate_id=package.candidate_id,
                invocation_id=request.invocation_id,
                session_id=request.session_id,
                trigger=request.trigger,
                model_outputs_hash=package.model_outputs_hash,
                final_pack_hash=package.final_pack_hash,
                body_hash=package.body_hash,
                installed_messages_hash=installed_hash,
            ),
        )


__all__ = ["HandlerContextCompactionOrchestrator"]
