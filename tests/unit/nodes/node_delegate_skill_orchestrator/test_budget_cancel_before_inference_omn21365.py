# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run the budget cancels after routing, before inference, still names its rung.

OMN-21365: 700 dev-lane timeouts at stage=dispatch carried an empty ladder and
latency 0. A routed run records the rung it was routed to as a timeout attempt,
and reports the time it ran as its latency.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import EnumDelegationTerminalFailureCause

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    delegate_skill_terminal_from_response,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.dispatch_progress import (
    budget_cancelled_result,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _response_from_result,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegation_dispatch_progress import (
    DispatchStage,
    ModelDelegationDispatchProgress,
)

pytestmark = pytest.mark.unit

_ROUTED: dict[str, object] = {
    "tier": "local",
    "backend_id": "local-coder",
    "model_id": "served-model",
    "cost_usd": 0.0,
}


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize("stage", ["dispatch", "effect_boot"])
def test_a_routed_run_cancelled_before_inference_records_its_rung_and_latency(
    stage: DispatchStage,
) -> None:
    clock = _FakeClock()
    picked_up = clock.now
    progress = ModelDelegationDispatchProgress(
        stage=stage, in_flight_attempt=dict(_ROUTED)
    )
    clock.advance(50)
    elapsed_ms = int((clock.now - picked_up) * 1000)
    request = ModelDelegateSkillRequest(
        prompt="summarize this",
        task_type="summarization",
        source="claude-code",
        correlation_id=uuid4(),
    )

    terminal = delegate_skill_terminal_from_response(
        _response_from_result(
            request,
            budget_cancelled_result(
                progress,
                cancel_message="cancelled at stage=" + stage,
                cancelled_stage=stage,
            ),
            tenant_id=None,
            queue_wait_ms=None,
            execution_duration_ms=elapsed_ms,
            budget_evidence=None,
        )
    )

    assert terminal.terminal_failure_cause is EnumDelegationTerminalFailureCause.TIMEOUT
    assert len(terminal.attempts) == 1
    attempt = terminal.attempts[0]
    assert (attempt.backend_id, attempt.tier) == ("local-coder", "local")
    assert attempt.failure_class == "timeout"
    assert terminal.metrics.latency_ms == 50_000


def test_a_run_cancelled_before_it_was_routed_still_names_nothing() -> None:
    # Positive control: no rung was chosen, so none is invented.
    result = budget_cancelled_result(
        ModelDelegationDispatchProgress(),
        cancel_message="cancelled at stage=dispatch",
        cancelled_stage="dispatch",
    )

    assert result["attempts"] == []
