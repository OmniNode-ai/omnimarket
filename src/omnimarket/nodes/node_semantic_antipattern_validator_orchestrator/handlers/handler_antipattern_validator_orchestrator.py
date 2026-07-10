# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_semantic_antipattern_validator_orchestrator (OMN-11922).

ORCHESTRATOR node. Receives a file validation request, emits a single
ModelAntipatternMatchCommand event for node_antipattern_match_effect to consume
(Qdrant similarity lookup). The effect's response is then routed to
node_semantic_antipattern_classifier_compute for deterministic violation
classification.

``handle()`` returns the typed ``ModelAntipatternMatchCommand`` directly
(OMN-14242 thin canonical shape -- no ``ModelHandlerOutput`` envelope, no
coercion in the handler; the runtime wraps this for publication on the
declared match-requested topic).
"""

from __future__ import annotations

from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_match_command import (
    ModelAntipatternMatchCommand,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_validator_request import (
    ModelAntipatternValidatorRequest,
)


class HandlerAntipatternValidatorOrchestrator:
    """ORCHESTRATOR — returns the antipattern match command for the effect to consume."""

    def handle(
        self, payload: ModelAntipatternValidatorRequest
    ) -> ModelAntipatternMatchCommand:
        """Build the match command the effect needs to run the similarity lookup.

        ``correlation_id`` is forwarded verbatim from the request (OMN-14242:
        the prior UUID-parse-with-uuid4-fallback only ever fed the now-removed
        ``ModelHandlerOutput`` envelope's own correlation_id -- it never
        affected the emitted command, so it is dropped rather than ported).
        """
        return ModelAntipatternMatchCommand(
            file_path=payload.file_path,
            file_content=payload.file_content,
            enforcement_mode=payload.enforcement_mode,
            similarity_threshold=payload.similarity_threshold,
            correlation_id=payload.correlation_id,
        )


__all__ = ["HandlerAntipatternValidatorOrchestrator"]
