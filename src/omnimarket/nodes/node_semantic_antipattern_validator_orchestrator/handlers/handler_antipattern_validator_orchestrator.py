# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_semantic_antipattern_validator_orchestrator (OMN-11922).

ORCHESTRATOR node, first hop. Receives a file validation request and returns the
``ModelAntipatternMatchCommand`` the match effect consumes (Qdrant similarity
lookup). The match response is handled by ``HandlerAntipatternMatchResponseDispatch``
and the classification by ``HandlerAntipatternClassifiedEmit``, each on its own
contract route.

``handle()`` returns the typed command directly (OMN-14242 thin canonical shape --
no ``ModelHandlerOutput`` envelope, no coercion in the handler); the runtime wraps
it and publishes it on the topic the contract's ``published_events`` map names.
"""

from __future__ import annotations

from omnimarket.models.antipattern import ModelAntipatternValidationContext
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_match_command import (
    ModelAntipatternMatchCommand,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_validator_request import (
    ModelAntipatternValidatorRequest,
)


class HandlerAntipatternValidatorOrchestrator:
    """ORCHESTRATOR -- returns the antipattern match command for the effect to consume."""

    def handle(
        self, payload: ModelAntipatternValidatorRequest
    ) -> ModelAntipatternMatchCommand:
        """Build the match command the effect needs to run the similarity lookup.

        The file path, its line count and the policy parameters ride on the command
        as ``validation_context``; the effect echoes them on its response, so later
        hops need no state. An empty file has nothing to embed, so its path stands
        in as the query description (the effect refuses a command with neither).
        """
        has_content = bool(payload.file_content.strip())
        return ModelAntipatternMatchCommand(
            correlation_id=payload.correlation_id,
            code_text=payload.file_content if has_content else None,
            description=None if has_content else payload.file_path,
            min_similarity=payload.similarity_threshold,
            validation_context=ModelAntipatternValidationContext(
                file_path=payload.file_path,
                line_count=len(payload.file_content.splitlines()),
                similarity_threshold=payload.similarity_threshold,
                enforcement_mode=payload.enforcement_mode,
            ),
        )


__all__ = ["HandlerAntipatternValidatorOrchestrator"]
