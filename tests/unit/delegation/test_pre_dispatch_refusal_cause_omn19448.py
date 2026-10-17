# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC3: a refusal before dispatch never lands a NULL cause.

The lab readback (2026-10-15) found one failed ``delegation_events`` row with a
NULL ``terminal_failure_cause``: correlation
``252a8269-bcbc-44f1-a47e-c1fbfa036649``, a request the handler refused before
dispatch because it asked for 300 seconds against a 240 second task-class
ceiling. Its ``attempt_history`` was empty, so the ladder-derived causes had
nothing to read, and the refusal terminal declared no cause. omnibase_core
already defines ``refused_before_dispatch`` for exactly this event.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_projection_delegation.models.model_attempt_reduction import (
    reduce_delegation_attempts,
)

pytestmark = pytest.mark.unit


class _RecordingPort:
    def __init__(self) -> None:
        self.dispatched = False

    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        self.dispatched = True
        return {
            "status": "completed",
            "content": "ok",
            "delegated_to": "qwen-coder",
            "model_name": "Qwen3-Coder-30B",
            "quality_gate_passed": True,
            "quality_score": 0.95,
        }


def _handler(
    monkeypatch: pytest.MonkeyPatch, port: _RecordingPort, *, ceiling_seconds: int
) -> HandlerDelegateSkill:
    monkeypatch.setattr(
        handler_delegate_skill,
        "resolve_task_class_execution_budget",
        lambda _task_type: SimpleNamespace(
            task_class_timeout_ceiling_seconds=ceiling_seconds,
            terminal_delivery_margin_seconds=60,
        ),
    )
    return HandlerDelegateSkill(dispatch_port=port)


def _request(requested_timeout_seconds: int | None) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="write a test",
        task_type="code_generation",
        source="claude-code",
        correlation_id=uuid4(),
        requested_timeout_seconds=requested_timeout_seconds,
    )


@pytest.mark.asyncio
async def test_a_timeout_above_the_ceiling_is_refused_with_its_own_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _RecordingPort()

    terminal = await _handler(monkeypatch, port, ceiling_seconds=240).handle(
        _request(300)
    )

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.budget_refusal is not None
    assert not terminal.attempts
    assert (
        terminal.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.REFUSED_BEFORE_DISPATCH
    )
    assert port.dispatched is False


@pytest.mark.asyncio
async def test_a_request_inside_the_ceiling_is_not_given_the_refusal_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative control: the cause belongs to the refusal, not to the handler.
    port = _RecordingPort()

    terminal = await _handler(monkeypatch, port, ceiling_seconds=240).handle(
        _request(240)
    )

    assert port.dispatched is True
    assert terminal is not None
    assert terminal.terminal_failure_cause is None


def test_the_projection_keeps_the_refusal_cause_on_an_empty_ladder() -> None:
    reduction = reduce_delegation_attempts(
        declared_status="failed",
        declared_quality_gate_passed=False,
        error_message="timeout_exceeds_task_class_ceiling: requested_timeout_seconds=300",
        attempts=(),
        declared_failure_cause=EnumDelegationTerminalFailureCause.REFUSED_BEFORE_DISPATCH,
    )

    assert (
        reduction.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.REFUSED_BEFORE_DISPATCH
    )
    assert reduction.terminal_ok is False
