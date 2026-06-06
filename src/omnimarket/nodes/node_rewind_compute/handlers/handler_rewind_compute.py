# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRewindCompute — Query event stream by agent identity and timestamp window.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_rewind_compute.models.model_rewind_compute_request import (
    ModelRewindComputeRequest,
)
from omnimarket.nodes.node_rewind_compute.models.model_rewind_compute_result import (
    ModelRewindComputeResult,
)
from omnimarket.nodes.session_state_projection import (
    EventProjectionStore,
    parse_event_timestamp,
    summarize_event_action,
)


class HandlerRewindCompute:
    """Read Onex event projections for an agent within a rewind window."""

    def __init__(
        self,
        store: EventProjectionStore | None = None,
        state_dir: Path | str | None = None,
    ) -> None:
        self._store = store or EventProjectionStore(state_dir=state_dir)

    def handle(self, request: ModelRewindComputeRequest) -> ModelRewindComputeResult:
        anchor = parse_event_timestamp(request.timestamp)
        if anchor is None:
            return ModelRewindComputeResult(
                status="error",
                events=[],
                actions_taken=[],
                event_count=0,
                error=f"invalid timestamp: {request.timestamp!r}",
            )

        try:
            events = self._store.matching_events(
                agent_name=request.agent_name,
                anchor=anchor,
                window_seconds=request.window_seconds,
            )
        except ValueError as exc:
            return ModelRewindComputeResult(
                status="error",
                events=[],
                actions_taken=[],
                event_count=0,
                error=str(exc),
            )

        if not events:
            return ModelRewindComputeResult(
                status="not_found",
                events=[],
                actions_taken=[],
                event_count=0,
                error=None,
            )

        actions = [summarize_event_action(event) for event in events]
        return ModelRewindComputeResult(
            status="ok",
            events=events,
            actions_taken=actions,
            event_count=len(events),
            error=None,
        )
