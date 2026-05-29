# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerResumeSessionCompute."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.events.checkpoint import ModelCheckpointRequest
from omnimarket.nodes.node_checkpoint_compute.handlers.handler_checkpoint_compute import (
    HandlerCheckpointCompute,
)
from omnimarket.nodes.node_resume_session_compute.handlers.handler_resume_session_compute import (
    HandlerResumeSessionCompute,
)
from omnimarket.nodes.node_resume_session_compute.models.model_resume_session_compute_request import (
    ModelResumeSessionComputeRequest,
)


@pytest.mark.unit
def test_handler_loads_matching_checkpoint(tmp_path: Path) -> None:
    """Handler returns the latest matching checkpoint projection."""
    checkpoint = HandlerCheckpointCompute(state_dir=tmp_path)
    checkpoint.handle(
        ModelCheckpointRequest(
            checkpoint_id="task-001-agent-001",
            action="save",
            payload={
                "task_id": "task-001",
                "agent_id": "agent-001",
                "phase": "executing",
                "progress_pct": 0.75,
            },
        )
    )

    handler = HandlerResumeSessionCompute(state_dir=tmp_path)
    request = ModelResumeSessionComputeRequest(task_id="task-001", agent_id="agent-001")
    result = handler.handle(request)

    assert result.status == "ok"
    assert result.phase == "executing"
    assert result.progress_pct == 0.75
    assert result.session_state["task_id"] == "task-001"


@pytest.mark.unit
def test_handler_returns_not_found(tmp_path: Path) -> None:
    """Handler returns not_found when no projection matches."""
    handler = HandlerResumeSessionCompute(state_dir=tmp_path)
    request = ModelResumeSessionComputeRequest(task_id="task-001", agent_id="agent-001")
    result = handler.handle(request)

    assert result.status == "not_found"
    assert result.session_state == {}


@pytest.mark.unit
def test_request_model_frozen() -> None:
    """Request model must be frozen (immutable)."""
    request = ModelResumeSessionComputeRequest(task_id="task-001", agent_id="agent-001")
    with pytest.raises(ValidationError):
        request.task_id = "task-002"  # type: ignore[misc]
