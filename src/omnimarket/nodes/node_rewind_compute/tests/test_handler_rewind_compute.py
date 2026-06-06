# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerRewindCompute."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_rewind_compute.handlers.handler_rewind_compute import (
    HandlerRewindCompute,
)
from omnimarket.nodes.node_rewind_compute.models.model_rewind_compute_request import (
    ModelRewindComputeRequest,
)


@pytest.mark.unit
def test_handler_returns_events_in_window(tmp_path: Path) -> None:
    """Handler reads Onex event projections for the requested agent."""
    event_dir = tmp_path / "dispatch-log"
    event_dir.mkdir()
    event_path = event_dir / "events.jsonl"
    events = [
        {
            "agent_name": "other-agent",
            "timestamp": "2026-05-25T00:05:00Z",
            "action": "ignored",
        },
        {
            "agent_name": "test-agent",
            "timestamp": "2026-05-25T00:10:00Z",
            "action": "started",
        },
        {
            "agent_name": "test-agent",
            "timestamp": "2026-05-25T00:30:00Z",
            "event_type": "completed",
        },
    ]
    event_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )

    handler = HandlerRewindCompute(state_dir=tmp_path)
    request = ModelRewindComputeRequest(
        agent_name="test-agent",
        timestamp="2026-05-25T00:30:00Z",
        window_seconds=1800,
    )
    result = handler.handle(request)

    assert result.status == "ok"
    assert result.event_count == 2
    assert result.actions_taken == ["started", "completed"]


@pytest.mark.unit
def test_handler_returns_not_found(tmp_path: Path) -> None:
    """Handler returns not_found when no event matches."""
    handler = HandlerRewindCompute(state_dir=tmp_path)
    result = handler.handle(
        ModelRewindComputeRequest(
            agent_name="test-agent",
            timestamp="2026-05-25T00:00:00Z",
        )
    )

    assert result.status == "not_found"


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
