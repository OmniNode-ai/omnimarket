# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerResumeSessionCompute — Load projected session state by task_id and agent_id.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omnimarket.nodes.node_resume_session_compute.models.model_resume_session_compute_request import (
    ModelResumeSessionComputeRequest,
)
from omnimarket.nodes.node_resume_session_compute.models.model_resume_session_compute_result import (
    ModelResumeSessionComputeResult,
)
from omnimarket.nodes.session_state_projection import (
    CheckpointProjectionStore,
    load_session_phase_projection,
)


class HandlerResumeSessionCompute:
    """Resolve the latest agent session projection for a task."""

    def __init__(
        self,
        store: CheckpointProjectionStore | None = None,
        state_dir: Path | str | None = None,
    ) -> None:
        self._state_dir = state_dir
        self._store = store or CheckpointProjectionStore(state_dir=state_dir)

    def handle(
        self, request: ModelResumeSessionComputeRequest
    ) -> ModelResumeSessionComputeResult:
        snapshots = self._store.find_session_snapshots(
            task_id=request.task_id,
            agent_id=request.agent_id,
        )
        if not snapshots:
            return ModelResumeSessionComputeResult(
                status="not_found",
                session_state={},
                phase="",
                progress_pct=0.0,
                error=None,
            )

        state = dict(snapshots[0].state)
        phase_projection = load_session_phase_projection(self._state_dir)
        if (
            phase_projection is not None
            and phase_projection.get("session_id") == request.task_id
        ):
            state.setdefault("phase_projection", phase_projection)

        return ModelResumeSessionComputeResult(
            status="ok",
            session_state=state,
            phase=_extract_phase(state),
            progress_pct=_extract_progress_pct(state),
            error=None,
        )


def _extract_phase(state: dict[str, Any]) -> str:
    for field_name in ("phase", "current_phase", "status_phase"):
        value = state.get(field_name)
        if isinstance(value, str):
            return value
    nested = state.get("session_state")
    if isinstance(nested, dict):
        return _extract_phase(nested)
    phase_projection = state.get("phase_projection")
    if isinstance(phase_projection, dict):
        value = phase_projection.get("current_phase")
        if isinstance(value, str):
            return value
    return ""


def _extract_progress_pct(state: dict[str, Any]) -> float:
    for field_name in ("progress_pct", "progress", "completion_pct"):
        value = state.get(field_name)
        if isinstance(value, int | float):
            return max(0.0, min(1.0, float(value)))
    nested = state.get("session_state")
    if isinstance(nested, dict):
        return _extract_progress_pct(nested)
    phase_projection = state.get("phase_projection")
    if isinstance(phase_projection, dict):
        budget_elapsed_pct = phase_projection.get("budget_elapsed_pct")
        if isinstance(budget_elapsed_pct, int | float):
            return max(0.0, min(1.0, float(budget_elapsed_pct) / 100.0))
    return 0.0
