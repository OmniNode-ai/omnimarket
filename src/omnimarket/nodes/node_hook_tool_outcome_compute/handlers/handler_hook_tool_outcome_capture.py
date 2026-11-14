# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHookToolOutcomeCapture: captured tool content in, classified outcome out.

Wraps the pure HandlerHookToolOutcome with the bus-facing record shape. Rules
are loaded lazily from the overlay on the first handle call that needs them,
so an absent overlay raises NodeOverlayError at classify time, never at
construction.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.nodes.node_hook_tool_outcome_compute.handlers.handler_hook_tool_outcome import (
    HandlerHookToolOutcome,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models.model_hook_tool_outcome import (
    ModelHookToolOutcomeRequest,
    ModelToolOutcomeRules,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models.model_hook_tool_outcome_classified import (
    ModelHookToolOutcomeClassified,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models.model_tool_content_record import (
    ModelToolContentRecord,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.overlay import load_outcome_rules

TOOL_RESPONSE_KIND = "tool_response"
TOOL_ERROR_KIND = "tool_error"


class HandlerHookToolOutcomeCapture:
    """Classify one captured tool call into a typed outcome event."""

    def __init__(
        self,
        rules: ModelToolOutcomeRules | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._rules = rules
        self._environ = environ
        self._inner: HandlerHookToolOutcome | None = None

    def _get_inner(self) -> HandlerHookToolOutcome:
        if self._inner is None:
            if self._rules is None:
                self._rules = load_outcome_rules(self._environ)
            self._inner = HandlerHookToolOutcome(self._rules)
        return self._inner

    def handle(
        self, payload: ModelToolContentRecord
    ) -> ModelHookToolOutcomeClassified | None:
        """Return the classified outcome event for a tool record, else None."""
        record = payload
        if record.content_kind not in (TOOL_RESPONSE_KIND, TOOL_ERROR_KIND):
            return None
        if record.chunk_index != 0:
            return None
        if not record.tool_name or not record.tool_name.strip():
            return None
        if not record.correlation_id or not record.correlation_id.strip():
            return None
        if not record.content or not record.content.strip():
            return None

        inner = self._get_inner()
        if record.content_kind == TOOL_RESPONSE_KIND:
            request = ModelHookToolOutcomeRequest(
                tool_name=record.tool_name,
                hook_event_name="PostToolUse",
                tool_response=record.content,
            )
        else:
            request = ModelHookToolOutcomeRequest(
                tool_name=record.tool_name,
                hook_event_name="PostToolUseFailure",
                error=record.content,
            )

        result = inner.handle(request)
        return ModelHookToolOutcomeClassified(
            session_id=record.session_id,
            correlation_id=record.correlation_id,
            tool_name=record.tool_name,
            outcome=result.outcome,
            refusing_component=result.refusing_component,
            matched_rule=result.matched_rule,
            exit_code=result.exit_code,
            reason=result.reason,
            emitted_at=record.emitted_at,
        )


__all__ = [
    "TOOL_ERROR_KIND",
    "TOOL_RESPONSE_KIND",
    "HandlerHookToolOutcomeCapture",
]
