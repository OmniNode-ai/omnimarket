# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerRewindCompute stub."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_rewind_compute.handlers.handler_rewind_compute import (
    HandlerRewindCompute,
)
from omnimarket.nodes.node_rewind_compute.models.model_rewind_compute_request import (
    ModelRewindComputeRequest,
)


@pytest.mark.unit
def test_handler_raises_not_implemented() -> None:
    """Stub handler must raise NotImplementedError."""
    handler = HandlerRewindCompute()
    request = ModelRewindComputeRequest(
        agent_name="test-agent",
        timestamp="2026-05-25T00:00:00Z",
    )
    with pytest.raises(NotImplementedError):
        handler.handle(request)


@pytest.mark.unit
def test_request_model_frozen() -> None:
    """Request model must be frozen (immutable)."""
    request = ModelRewindComputeRequest(
        agent_name="test-agent",
        timestamp="2026-05-25T00:00:00Z",
    )
    with pytest.raises(ValidationError):
        request.agent_name = "other-agent"  # type: ignore[misc]


@pytest.mark.unit
def test_request_default_window() -> None:
    """window_seconds defaults to 3600."""
    request = ModelRewindComputeRequest(
        agent_name="test-agent",
        timestamp="2026-05-25T00:00:00Z",
    )
    assert request.window_seconds == 3600
