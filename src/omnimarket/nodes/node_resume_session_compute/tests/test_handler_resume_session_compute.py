# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerResumeSessionCompute stub."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_resume_session_compute.handlers.handler_resume_session_compute import (
    HandlerResumeSessionCompute,
)
from omnimarket.nodes.node_resume_session_compute.models.model_resume_session_compute_request import (
    ModelResumeSessionComputeRequest,
)


@pytest.mark.unit
def test_handler_raises_not_implemented() -> None:
    """Stub handler must raise NotImplementedError."""
    handler = HandlerResumeSessionCompute()
    request = ModelResumeSessionComputeRequest(task_id="task-001", agent_id="agent-001")
    with pytest.raises(NotImplementedError):
        handler.handle(request)


@pytest.mark.unit
def test_request_model_frozen() -> None:
    """Request model must be frozen (immutable)."""
    request = ModelResumeSessionComputeRequest(task_id="task-001", agent_id="agent-001")
    with pytest.raises(ValidationError):
        request.task_id = "task-002"  # type: ignore[misc]
