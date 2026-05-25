# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_semantic_antipattern_validator_orchestrator (OMN-11922).

ORCHESTRATOR node. Receives a file validation request, emits a single
ModelAntipatternMatchCommand event for node_antipattern_match_effect to consume
(Qdrant similarity lookup). The effect's response is then routed to
node_semantic_antipattern_classifier_compute for deterministic violation
classification.

ORCHESTRATOR contract: emits events[], never returns result.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_match_command import (
    ModelAntipatternMatchCommand,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_validator_request import (
    ModelAntipatternValidatorRequest,
)


class HandlerAntipatternValidatorOrchestrator:
    """ORCHESTRATOR — emits antipattern match command for the effect to consume."""

    def handle(
        self, request: ModelAntipatternValidatorRequest
    ) -> ModelHandlerOutput[None]:
        match_cmd = ModelAntipatternMatchCommand(
            file_path=request.file_path,
            file_content=request.file_content,
            enforcement_mode=request.enforcement_mode,
            similarity_threshold=request.similarity_threshold,
            correlation_id=request.correlation_id,
        )

        correlation_uuid: UUID
        try:
            correlation_uuid = UUID(request.correlation_id)
        except (ValueError, AttributeError):
            correlation_uuid = uuid4()

        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=correlation_uuid,
            handler_id="node_semantic_antipattern_validator_orchestrator",
            events=(match_cmd,),
        )


__all__ = ["HandlerAntipatternValidatorOrchestrator"]
