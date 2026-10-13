# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Second hop of node_semantic_antipattern_validator_orchestrator.

Consumes the match effect's response and returns the classify command for
node_semantic_antipattern_classifier_compute. An empty response (no match, or a
graceful skip) still goes to the classifier, which returns zero violations, so every
validation ends on the same terminal event.
"""

from __future__ import annotations

from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_classify_command import (
    ModelAntipatternClassifyCommand,
    ModelAntipatternClassifyConfig,
    ModelAntipatternClassifyMatch,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_match_result import (
    ModelAntipatternMatchResult,
)


class HandlerAntipatternMatchResponseDispatch:
    """ORCHESTRATOR -- turns a match response into the classify command."""

    def handle(
        self, payload: ModelAntipatternMatchResult
    ) -> ModelAntipatternClassifyCommand:
        context = payload.validation_context
        return ModelAntipatternClassifyCommand(
            matches=tuple(
                ModelAntipatternClassifyMatch(
                    pattern_id=match.antipattern_name,
                    label=match.antipattern_name,
                    similarity=match.similarity_score,
                    enforcement=match.enforcement,
                    description=match.description,
                    file_path=context.file_path,
                    line_count=context.line_count,
                )
                for match in payload.matches
            ),
            config=ModelAntipatternClassifyConfig(
                similarity_threshold=context.similarity_threshold
            ),
            correlation_id=payload.correlation_id,
            validation_context=context.model_copy(
                update={"match_error": payload.error_message}
            ),
        )


__all__ = ["HandlerAntipatternMatchResponseDispatch"]
