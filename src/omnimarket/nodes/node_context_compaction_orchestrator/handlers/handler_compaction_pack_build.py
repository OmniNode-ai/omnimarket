# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus request/reply for the compaction context pack.

Ask the context-pack orchestrator for the ``compaction_orchestrator`` profile at
the command's session barrier and map the built package or its failure to
``ModelContextCompactionBuildOutcome``.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass,
    ModelContextCompactionBuildOutcome,
    ModelContextCompactionBuiltPackage,
    ModelContextCompactionRequested,
)
from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackBuilt,
    ModelContextPackCommand,
    ModelContextPackRequest,
)

if TYPE_CHECKING:
    from omnibase_infra.runtime.service_terminal_event_consumer import (
        TerminalEventConsumer,
    )

COMPACTION_PROFILE = "compaction_orchestrator"


def build_pack_command(
    request: ModelContextCompactionRequested,
    access: ModelContextAccess,
    source_revision: str | None,
    *,
    lane: str | None = None,
) -> ModelContextPackCommand:
    """Build the shared command, retaining each source's revision precision."""
    # Ledger row revisions have second precision; transcript boundaries may
    # have fractions and must retain every pre-barrier prompt.
    revisions: dict[EnumContextFactor, str] = {}
    if source_revision is not None:
        ledger_revision = datetime.fromisoformat(source_revision).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        revisions = dict.fromkeys(
            (
                EnumContextFactor.OPERATOR_RULINGS,
                EnumContextFactor.OPEN_WORK,
                EnumContextFactor.ORCHESTRATION_STATE,
            ),
            ledger_revision,
        )
        revisions[EnumContextFactor.OPERATOR_WORDS_UNRECORDED] = source_revision
    command = ModelContextPackCommand(
        request=ModelContextPackRequest(
            profile_name=COMPACTION_PROFILE,
            session_id=request.session_id,
            newest_turn=request.newest_turn,
            session_barrier=request.barrier_id,
            tier_revisions=revisions,
            caller_scope=ModelContextCallerScope(lane=lane),
        ),
        access=access,
    )
    return command


def map_pack_built(built: ModelContextPackBuilt) -> ModelContextCompactionBuildOutcome:
    """Validate the package identities before offering it for installation."""
    if (
        built.status is not EnumContextPackOrchestratorStatus.OK
        or built.candidate_id is None
        or built.assembly_hash is None
        or built.final_pack_hash is None
        or built.body_hash is None
        or built.version_vector_hash is None
        or built.profile_content_hash is None
        or not built.messages
    ):
        return ModelContextCompactionBuildOutcome(
            failure_class=EnumContextCompactionFailureClass.PACK_BUILD_FAILED
        )
    model_outputs_json = json.dumps(
        list(built.model_outputs), sort_keys=True, separators=(",", ":")
    )
    return ModelContextCompactionBuildOutcome(
        package=ModelContextCompactionBuiltPackage(
            candidate_id=UUID(built.candidate_id),
            assembly_hash=built.assembly_hash,
            model_outputs_hash=hashlib.sha256(
                model_outputs_json.encode("utf-8")
            ).hexdigest(),
            final_pack_hash=built.final_pack_hash,
            body_hash=built.body_hash,
            version_vector_hash=built.version_vector_hash,
            profile_name=built.profile_name,
            profile_content_hash=built.profile_content_hash,
            messages=built.messages,
            model_section_ms=built.model_section_ms(),
        )
    )


class HandlerCompactionPackBuildBus:
    """Publish a pack command and await either correlated terminal."""

    def __init__(
        self,
        event_publisher: Callable[[str, bytes], None],
        event_consumer: TerminalEventConsumer,
        access: ModelContextAccess,
        request_topic: str,
        completed_topic: str,
        failed_topic: str,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._publisher = event_publisher
        self._consumer = event_consumer
        self._access = access
        self._request_topic = request_topic
        self._completed_topic = completed_topic
        self._failed_topic = failed_topic
        self._monotonic = monotonic

    def build(
        self,
        request: ModelContextCompactionRequested,
        *,
        source_revision: str | None,
        deadline: float,
        lane: str | None = None,
    ) -> ModelContextCompactionBuildOutcome:
        command_id = uuid4()
        command = build_pack_command(request, self._access, source_revision, lane=lane)
        with ExitStack() as stack:
            completed = self._consumer.open(self._completed_topic)
            stack.callback(completed.close)
            failed = self._consumer.open(self._failed_topic)
            stack.callback(failed.close)
            completed.register(str(command_id))
            failed.register(str(command_id))
            if self._monotonic() >= deadline:
                return ModelContextCompactionBuildOutcome(
                    failure_class=EnumContextCompactionFailureClass.PACK_BUILDER_UNAVAILABLE
                )
            self._publisher(
                self._request_topic,
                ModelEventEnvelope[ModelContextPackCommand](
                    payload=command,
                    correlation_id=command_id,
                    event_type=self._request_topic,
                )
                .model_dump_json()
                .encode(),
            )
            remaining = deadline - self._monotonic()
            reply = (
                completed.wait(str(command_id), remaining) if remaining > 0 else None
            )
        if reply is None or self._monotonic() >= deadline:
            return ModelContextCompactionBuildOutcome(
                failure_class=EnumContextCompactionFailureClass.PACK_BUILDER_UNAVAILABLE
            )
        if str(reply.get("correlation_id")) != str(command_id):
            raise ValueError("Pack terminal correlation_id differs from command id")
        try:
            built = ModelContextPackBuilt.model_validate(reply.get("payload"))
        except ValidationError:
            return ModelContextCompactionBuildOutcome(
                failure_class=EnumContextCompactionFailureClass.PACK_BUILD_FAILED
            )
        return map_pack_built(built)


__all__ = [
    "COMPACTION_PROFILE",
    "HandlerCompactionPackBuildBus",
    "build_pack_command",
    "map_pack_built",
]
