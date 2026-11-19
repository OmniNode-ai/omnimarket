# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC8: a stored latency of 0 on a failed call is not a measured duration.

The audit found 58 failed runs stored with latency 0 and counted as measured durations. A call that did not
complete and carries a latency of 0 never timed a model call, so the row stores NULL, as an unpriced cost is
(OMN-20717). A real latency, or any latency on a completed call, is kept as recorded.

Failure modes, each with a test below:
  D1  a failed call stored at latency 0 is a measured duration of 0 ms
  D2  a timed-out call stored at latency 0 is a measured duration of 0 ms
  D3  a failed call loses a latency it did measure
  D4  a completed call loses its latency
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    delegate_skill_terminal_from_response,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
    ModelDelegationEventProjectionRow,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _response_from_result,
)

pytestmark = pytest.mark.unit


def _row(
    status: Literal["completed", "failed", "timeout"], latency_ms: int
) -> ModelDelegationEventProjectionRow:
    request = ModelDelegateSkillRequest(
        prompt="what is 2+2",
        task_type="research",
        source="claude-code",
        correlation_id=uuid4(),
    )
    result: dict[str, object] = {
        "status": "completed",
        "content": "4",
        "quality_gate_passed": True,
        "input_tokens": 10,
        "output_tokens": 2,
        "model_cloud_baseline": "claude-sonnet-5-5",
        "attempts": [],
        "cost_usd": 0.0,
    }
    response = _response_from_result(
        request,
        result,
        tenant_id=None,
        queue_wait_ms=None,
        execution_duration_ms=10,
        budget_evidence=None,
    )
    terminal = ModelDelegateSkillTerminalProjection.from_payload(
        delegate_skill_terminal_from_response(response).model_dump(mode="json")
    )
    terminal = terminal.model_copy(
        update={
            "status": status,
            "metrics": terminal.metrics.model_copy(update={"latency_ms": latency_ms}),
        }
    )
    return ModelDelegationEventProjectionRow.from_terminal_event(terminal)


@pytest.mark.parametrize(
    "status", ["failed", "timeout"], ids=["D1-failed", "D2-timeout"]
)
def test_failed_call_latency_zero_is_not_a_measured_duration(
    status: Literal["failed", "timeout"],
) -> None:
    assert _row(status, 0).latency_ms is None


def test_failed_call_latency_a_measured_latency_is_kept() -> None:
    """D3."""
    assert _row("failed", 1234).latency_ms == 1234


def test_failed_call_latency_a_completed_call_keeps_its_latency() -> None:
    """D4."""
    assert _row("completed", 0).latency_ms == 0
    assert _row("completed", 850).latency_ms == 850
