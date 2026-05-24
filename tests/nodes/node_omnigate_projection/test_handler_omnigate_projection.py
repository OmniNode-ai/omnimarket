# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerOmniGateProjection."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_omnigate_projection.handlers.handler_omnigate_projection import (
    HandlerOmniGateProjection,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def handler() -> HandlerOmniGateProjection:
    return HandlerOmniGateProjection()


def test_handler_type_and_category(handler: HandlerOmniGateProjection) -> None:
    assert handler.handler_type == "NODE_HANDLER"
    assert handler.handler_category == "COMPUTE"


def test_handle_dict_empty_state_produces_one_row(
    handler: HandlerOmniGateProjection,
) -> None:
    result = handler.handle_dict(
        {
            "activity": [],
            "metrics": {},
            "event": {
                "repository_id": "repo-1",
                "project_name": "Omni",
                "branch": "main",
                "ok": True,
                "checked_at": "2026-05-17T12:00:00Z",
            },
        }
    )

    assert len(result["activity"]) == 1
    assert result["activity"][0]["status"] == "pass"
    assert result["metrics"]["total_events"] == 1
    assert result["metrics"]["passed"] == 1


def test_handle_dict_fail_event_increments_failed_counter(
    handler: HandlerOmniGateProjection,
) -> None:
    result = handler.handle_dict(
        {
            "activity": [],
            "metrics": {},
            "event": {
                "repository_id": "repo-2",
                "project_name": "Omni",
                "branch": "feature",
                "ok": False,
                "reason": "Checks failed",
                "checked_at": "2026-05-17T13:00:00Z",
                "checks": [{"name": "lint", "status": "FAIL"}],
            },
        }
    )

    assert result["activity"][0]["status"] == "fail"
    assert result["metrics"]["failed"] == 1
    assert result["metrics"]["total_events"] == 1


def test_handle_dict_accumulates_existing_activity(
    handler: HandlerOmniGateProjection,
) -> None:
    # Seed one existing row via handle_dict
    first = handler.handle_dict(
        {
            "activity": [],
            "metrics": {},
            "event": {
                "repository_id": "repo-1",
                "project_name": "Omni",
                "branch": "main",
                "ok": True,
                "checked_at": "2026-05-17T12:00:00Z",
            },
        }
    )

    # Apply a second event on top of the first result
    second = handler.handle_dict(
        {
            "activity": first["activity"],
            "metrics": first["metrics"],
            "event": {
                "repository_id": "repo-2",
                "project_name": "Omni",
                "branch": "feature",
                "ok": False,
                "checked_at": "2026-05-17T13:00:00Z",
            },
        }
    )

    assert len(second["activity"]) == 2
    assert second["metrics"]["total_events"] == 2
    assert second["metrics"]["passed"] == 1
    assert second["metrics"]["failed"] == 1


def test_handle_delegates_to_handle_dict(handler: HandlerOmniGateProjection) -> None:
    input_data = {
        "activity": [],
        "metrics": {},
        "event": {
            "repository_id": "repo-x",
            "project_name": "X",
            "branch": "dev",
            "ok": True,
            "checked_at": "2026-05-17T14:00:00Z",
        },
    }
    assert handler.handle(input_data) == handler.handle_dict(input_data)
