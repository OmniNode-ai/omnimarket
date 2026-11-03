# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22223: the caller wait and the runtime budget are one budget, on one clock.

The caller waits ``execution + margin`` from publish. The runtime used to spend
``execution + allowance`` from pickup, so a run that queued 120 s ran its full
300 s and the caller gave up at 300 s from publish with no terminal.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from omnimarket.inference.task_class_authority import (
    load_task_class_authority,
    resolve_task_class_execution_budget,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    ProtocolDelegationDispatchPort,
    caller_wait_seconds,
    resolve_wait_allowance_seconds,
)

_CEILING = 240
_MARGIN = 60  # 240 + 60 = the 300 s total budget


class _CapturingPort:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        self.kwargs = kwargs
        return {
            "status": "completed",
            "content": "ok",
            "delegated_to": "qwen-coder",
            "model_name": "Qwen3-Coder-30B",
            "quality_gate_passed": True,
            "quality_score": 0.95,
        }


def _request(queue_wait_seconds: float | None) -> ModelDelegateSkillRequest:
    published_at = (
        None
        if queue_wait_seconds is None
        else datetime.now(UTC) - timedelta(seconds=queue_wait_seconds)
    )
    return ModelDelegateSkillRequest(
        prompt="write a test",
        task_type="code_generation",
        source="claude-code",
        correlation_id=uuid4(),
        published_at=published_at,
    )


def _handler(
    monkeypatch: pytest.MonkeyPatch, port: _CapturingPort
) -> tuple[HandlerDelegateSkill, list[float]]:
    monkeypatch.setattr(
        handler_delegate_skill,
        "resolve_task_class_execution_budget",
        lambda _task_type: SimpleNamespace(
            task_class_timeout_ceiling_seconds=_CEILING,
            terminal_delivery_margin_seconds=_MARGIN,
        ),
    )
    waits: list[float] = []
    real_wait_for = asyncio.wait_for

    async def _recording_wait_for(awaitable: Any, timeout: float) -> Any:
        waits.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(handler_delegate_skill.asyncio, "wait_for", _recording_wait_for)
    return HandlerDelegateSkill(
        dispatch_port=cast(ProtocolDelegationDispatchPort, port)
    ), waits


@pytest.mark.unit
@pytest.mark.asyncio
async def test_queue_wait_deducted_from_the_execution_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """120 s in the queue of a 300 s budget leaves 180 s, not a fresh 300 s."""
    port = _CapturingPort()
    handler, waits = _handler(monkeypatch, port)

    terminal = await handler.handle(_request(queue_wait_seconds=120))

    assert terminal.status == "completed"
    assert port.kwargs is not None
    # The queue wait is rounded up to whole seconds, so allow 1 s of test jitter.
    assert port.kwargs["execution_timeout_seconds"] == pytest.approx(
        _CEILING - 120, abs=2
    )
    assert waits == [pytest.approx(float(_CEILING - 120 + _MARGIN), abs=2.0)]
    assert waits[0] < _CEILING + _MARGIN


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unmeasured_queue_wait_deducts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _CapturingPort()
    handler, waits = _handler(monkeypatch, port)

    await handler.handle(_request(queue_wait_seconds=None))

    assert port.kwargs is not None
    assert port.kwargs["execution_timeout_seconds"] == _CEILING
    assert waits == [float(_CEILING + _MARGIN)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_queue_wait_deducted_budget_spent_in_the_queue_is_a_typed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that spent its execution budget queued is not dispatched at all."""
    port = _CapturingPort()
    handler, waits = _handler(monkeypatch, port)

    terminal = await handler.handle(_request(queue_wait_seconds=_CEILING + 5))

    assert terminal.status == "failed"
    assert port.kwargs is None, "an exhausted budget must reach no model"
    assert waits == []
    assert terminal.error_message.startswith("queue_budget_exhausted:")
    assert terminal.terminal_failure_cause == "refused_before_dispatch"
    assert terminal.queue_wait_ms is not None
    assert terminal.queue_wait_ms >= (_CEILING + 5) * 1000
    assert terminal.budget_evidence is not None


@pytest.mark.unit
def test_caller_wait_covers_runtime_for_every_task_class_and_request() -> None:
    """The runtime total never exceeds the caller's wait (same clock, publish)."""
    task_classes = sorted(load_task_class_authority().task_classes)
    assert task_classes, "positive control: the contract names task classes"
    for task_class in task_classes:
        budget = resolve_task_class_execution_budget(task_class)
        ceiling = budget.task_class_timeout_ceiling_seconds
        margin = budget.terminal_delivery_margin_seconds
        grace = getattr(budget, "requested_timeout_grace_seconds", None)
        for requested in (None, 1, ceiling // 2, ceiling):
            execution = ceiling if requested is None else requested
            allowance = resolve_wait_allowance_seconds(
                requested_timeout_seconds=requested,
                requested_grace_seconds=grace,
                terminal_delivery_margin_seconds=margin,
            )
            assert (
                caller_wait_seconds(
                    requested_timeout_seconds=requested,
                    task_class_timeout_ceiling_seconds=ceiling,
                    terminal_delivery_margin_seconds=margin,
                )
                >= execution + allowance
            ), task_class


@pytest.mark.unit
def test_caller_wait_covers_runtime_even_when_grace_exceeds_margin() -> None:
    """Negative control: an over-margin grace is clamped, never trusted."""
    assert (
        resolve_wait_allowance_seconds(
            requested_timeout_seconds=100,
            requested_grace_seconds=500,
            terminal_delivery_margin_seconds=60,
        )
        == 60
    )
    assert (
        caller_wait_seconds(
            requested_timeout_seconds=100,
            task_class_timeout_ceiling_seconds=240,
            terminal_delivery_margin_seconds=60,
        )
        >= 100 + 60
    )
