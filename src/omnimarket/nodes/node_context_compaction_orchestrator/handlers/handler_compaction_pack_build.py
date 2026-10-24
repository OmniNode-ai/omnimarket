# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live ProtocolContextCompactionPackBuild (OMN-17427, plan task TA.11 calls TA.8).

Ask the context-pack orchestrator for the ``compaction_orchestrator`` profile at
the command's session barrier and map the built package or its failure to
``ModelContextCompactionBuildOutcome``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Protocol
from uuid import UUID

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_access import ModelContextAccess

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

COMPACTION_PROFILE = "compaction_orchestrator"


class ProtocolContextPackBuildCall(Protocol):
    """The context-pack build call used by the compaction adapter."""

    def handle(self, command: ModelContextPackCommand) -> ModelContextPackBuilt: ...


class HandlerCompactionPackBuild:
    """Build a compaction package with the tenant access from the command envelope."""

    def __init__(
        self,
        pack_orchestrator: ProtocolContextPackBuildCall,
        *,
        access: ModelContextAccess,
        profile_name: str = COMPACTION_PROFILE,
    ) -> None:
        self._pack_orchestrator = pack_orchestrator
        self._access = access
        self._profile_name = profile_name

    def build(
        self,
        request: ModelContextCompactionRequested,
        *,
        source_revision: str | None = None,
    ) -> ModelContextCompactionBuildOutcome:
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
                profile_name=self._profile_name,
                session_id=request.session_id,
                newest_turn=request.newest_turn,
                session_barrier=request.barrier_id,
                tier_revisions=revisions,
            ),
            access=self._access,
        )
        built = self._pack_orchestrator.handle(command)
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
            )
        )


__all__ = ["COMPACTION_PROFILE", "HandlerCompactionPackBuild"]
