# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Last hop of node_semantic_antipattern_validator_orchestrator.

Consumes the classifier's result and returns the terminal antipattern-validated
event for the correlation id that started the validation.
"""

from __future__ import annotations

from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_classified_result import (
    ModelAntipatternClassifiedResult,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_validated_event import (
    ModelAntipatternValidatedEvent,
)


class HandlerAntipatternClassifiedEmit:
    """ORCHESTRATOR -- turns a classification into the terminal event."""

    def handle(
        self, payload: ModelAntipatternClassifiedResult
    ) -> ModelAntipatternValidatedEvent:
        context = payload.validation_context
        return ModelAntipatternValidatedEvent(
            correlation_id=payload.correlation_id,
            file_path=context.file_path,
            enforcement_mode=context.enforcement_mode,
            violation_count=len(payload.violations),
            has_blocking_violation=any(v.is_blocking for v in payload.violations),
            violations=payload.violations,
            match_error=context.match_error,
        )


__all__ = ["HandlerAntipatternClassifiedEmit"]
