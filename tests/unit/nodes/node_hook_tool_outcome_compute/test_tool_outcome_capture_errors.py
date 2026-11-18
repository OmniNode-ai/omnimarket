# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain of the capture handler: records that yield no event, an absent overlay."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_hook_tool_outcome_compute.handlers import (
    HandlerHookToolOutcomeCapture,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models import (
    ModelHookToolOutcomeClassified,
    ModelToolContentRecord,
)

CORRELATION_ID = "0b4f9d2e-3c1a-4d57-9a6e-5f1c2b7d8e90"


def _fields(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": "session-1",
        "correlation_id": CORRELATION_ID,
        "content_kind": "tool_response",
        "tool_name": "Bash",
        "content": "REFUSED factory-floor-publish: x",
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    "over",
    [
        {"chunk_index": 1, "chunk_count": 2},
        {"content_kind": "prompt"},
        {"content_kind": "tool_input"},
        {"tool_name": "  "},
        {"tool_name": None},
        {"correlation_id": None},
        {"content": "   "},
        {"content": None},
    ],
)
def test_records_that_are_not_a_first_chunk_tool_result_yield_no_event(
    over: dict[str, Any],
) -> None:
    handler = HandlerHookToolOutcomeCapture(environ={})
    assert handler.handle(ModelToolContentRecord(**_fields(**over))) is None


def test_absent_overlay_refuses_at_classify_time_naming_the_setting() -> None:
    handler = HandlerHookToolOutcomeCapture(environ={})
    with pytest.raises(NodeOverlayError) as excinfo:
        handler.handle(ModelToolContentRecord(**_fields()))
    assert "overlay" in str(excinfo.value)


def test_constructing_without_rules_or_overlay_does_not_raise() -> None:
    HandlerHookToolOutcomeCapture(environ={})


def test_classified_event_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ModelHookToolOutcomeClassified.model_validate(
            {
                "session_id": "s",
                "correlation_id": CORRELATION_ID,
                "tool_name": "Bash",
                "outcome": "ok",
                "refusing_component": None,
                "matched_rule": None,
                "exit_code": None,
                "reason": "tool call succeeded",
                "surprise": 1,
            }
        )


def test_capture_record_ignores_a_field_the_producer_adds_later() -> None:
    record = ModelToolContentRecord(**_fields(digest="abc"))
    assert record.session_id == "session-1"
