# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer one compaction command with a package or a native fallback (OMN-21113, TA.11).

A built package is published as ``prepared``, then a fresh ``activation_id`` is
appended to the identity trailer (the last message), ``installed_messages_hash``
is taken over the whole list, and ``activated`` and the ``install`` reply carry
it. Any failure before activation, typed or raised, replies ``native`` and
publishes ``fallback`` with the failure class and the command's
``invocation_id``. No partial package is ever replied.
"""

from __future__ import annotations

import hashlib
import json
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
)
from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
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


def installed_messages_hash(messages: tuple[ModelContextPackRenderMessage, ...]) -> str:
    """sha256 over the canonical JSON of the role/text list, as the renderer hashes a body."""
    raw = json.dumps(
        [m.model_dump() for m in messages],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
    ) -> None:
        self._pack_build = pack_build if pack_build is not None else _UnwiredPackBuild()
        self._activation_ids = activation_ids

    def handle(
        self, request: ModelContextCompactionRequested
    ) -> ModelContextCompactionResult:
        try:
            outcome = self._pack_build.build(request)
        except Exception:
            return self._native(request, EnumContextCompactionFailureClass.BUILD_ERROR)
        if outcome.package is None:
            return self._native(
                request,
                outcome.failure_class
                or EnumContextCompactionFailureClass.PACK_BUILD_FAILED,
            )
        return self._install(request, outcome.package)

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

    def _native(
        self,
        request: ModelContextCompactionRequested,
        failure_class: EnumContextCompactionFailureClass,
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
            ),
        )

    def _install(
        self,
        request: ModelContextCompactionRequested,
        package: ModelContextCompactionBuiltPackage,
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


__all__ = ["HandlerContextCompactionOrchestrator", "installed_messages_hash"]
