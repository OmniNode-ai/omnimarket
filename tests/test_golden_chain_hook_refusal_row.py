# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the hook refusal row decision."""

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_hook_refusal_row_compute.handlers import (
    HandlerHookRefusalRowCompute,
)
from omnimarket.nodes.node_hook_refusal_row_compute.models import (
    ModelHookRefusalRowRequest,
    ModelHookRefusalRowResult,
)

pytestmark = pytest.mark.unit


def test_golden_chain_refusal_to_ledger_row() -> None:
    command = ModelHookRefusalRowRequest(
        guard="pre_tool_use_shared_tree_git_guard.sh",
        reason="unauthorised shared-tree git mutation",
        detail="BLOCKED: this command moves the shared registry clone",
        lane="omn20685-0d56",
        lane_source="sidecar",
        suppressed=2,
        timestamp="2026-10-09T06:00:00Z",
    )
    result = HandlerHookRefusalRowCompute().handle(command)
    assert isinstance(result, ModelHookRefusalRowResult)
    fields = [cell.strip() for cell in result.row.split(" | ")]
    assert fields[0] == "2026-10-09T06:00:00Z"
    assert fields[1] == "FRICTION"
    assert "lane=omn20685-0d56" in fields
    assert "guard=pre_tool_use_shared_tree_git_guard.sh" in fields
    assert "reason=unauthorised-shared-tree-git-mutation" in fields
    assert f"dedupe={result.key}" in fields
    assert "suppressed_since_last_row=2" in fields
    assert len(result.key) == 12
    assert not result.repeated_secret


def test_error_chain_malformed_command_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelHookRefusalRowRequest.model_validate({"guard": "g", "reason": 7})
    missing = {err["loc"][0] for err in caught.value.errors()}
    assert {"reason", "timestamp"} <= missing


def test_error_chain_hostile_text_is_neutralised_not_raised() -> None:
    hostile = "| |\r\n" * 50 + "token=" + "x" * 5000
    result = HandlerHookRefusalRowCompute().handle(
        ModelHookRefusalRowRequest(
            guard=hostile,
            reason=hostile,
            detail=hostile,
            lane=hostile,
            timestamp="2026-10-09T06:00:00Z",
        )
    )
    assert "\n" not in result.row
    assert "\r" not in result.row
    assert len(result.detail) <= 240
    assert len(result.guard) <= 64
