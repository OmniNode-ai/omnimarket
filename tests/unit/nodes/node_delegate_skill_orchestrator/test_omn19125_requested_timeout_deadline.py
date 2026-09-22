# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A caller-stated deadline is honoured, never merely accepted (OMN-19125).

Declaring ``requested_timeout_seconds`` on the wire model is only half the
fix. A field that validates and then governs nothing is worse than the
refusal it replaces: the refusal at least told the caller its deadline had not
been taken. ``onex delegate --timeout`` has promised a deadline since
OMN-14397 and, on the handler side, delivered none.

The bound these tests pin:

    effective = min(requested_timeout_seconds, max_handler_duration_seconds)

A caller may TIGHTEN the handler's wall-clock bound and may never loosen it.
The contract-declared budget exists to pre-empt the consumer's
``max_poll_interval_ms`` eviction deadline (OMN-15504); a caller able to raise
it could re-arm the livelock that took the ``.201`` dev lane's delegation
chain down on 2026-09-10. The same ``min`` is what the CLI already computes
for its own terminal wait (``cli_delegate._terminal_wait_seconds``), so the
two ends of the call now agree on which number wins.

These tests use a port that never resolves rather than a slow one, for the
reason OMN-15504 gives: a port that eventually returns lets a between-awaits
deadline check re-run, and a bound only checked between awaits is not a bound.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_handler_execution_budget import (
    ModelDelegateSkillHandlerBudget,
)

# The contract's real value. Used as the budget in the tests below so a
# caller-stated deadline is measured against the number the lane actually
# runs, not against a number chosen to make the assertion easy.
_CONTRACT_BUDGET_SECONDS = 240


class _NeverReturningDispatchPort:
    """Accepts the dispatch and never resolves. The stuck-rung shape."""

    def __init__(self) -> None:
        self.dispatch_started = asyncio.Event()
        self.cancelled = False

    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        self.dispatch_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class _FastDispatchPort:
    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        self.seen = kwargs
        return {
            "status": "completed",
            "content": "READY",
            "delegated_to": "qwen",
            "model_name": "Qwen3.8-27B",
            "quality_gate_passed": True,
            "quality_score": 1.0,
        }


def _request(**overrides: object) -> ModelDelegateSkillRequest:
    payload: dict[str, object] = {
        "prompt": "Reply with exactly the word READY",
        "task_type": "reasoning",
        "source": "claude-code",
        "correlation_id": uuid4(),
    }
    payload.update(overrides)
    return ModelDelegateSkillRequest(**payload)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_caller_deadline_shorter_than_the_budget_fires_at_the_deadline() -> (
    None
):
    """The headline property: ``--timeout 1`` returns in about a second.

    RED before this change, in two stages: the request would not construct at
    all, and once it did the handler waited the full contract budget.
    """
    port = _NeverReturningDispatchPort()
    handler = HandlerDelegateSkill(
        dispatch_port=port,  # type: ignore[arg-type]
        budget=ModelDelegateSkillHandlerBudget(
            max_handler_duration_seconds=_CONTRACT_BUDGET_SECONDS
        ),
    )

    started = time.monotonic()
    terminal = await asyncio.wait_for(
        handler.handle(_request(requested_timeout_seconds=1)), timeout=30.0
    )
    elapsed = time.monotonic() - started

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "timeout"
    assert elapsed < 15.0, (
        "OMN-19125: the caller asked for a 1 s deadline against a 240 s "
        f"contract budget and the handler ran {elapsed:.1f}s, so the deadline "
        "was accepted and then ignored."
    )
    assert port.cancelled, (
        "OMN-19125: the dispatch was abandoned rather than cancelled, which "
        "leaves the runtime port's correlation-scoped subscription open and "
        "hands the same stall to the next record (OMN-15504)."
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_refusal_names_the_caller_deadline_that_fired() -> None:
    """A bound that fired must say whose bound it was.

    ``exceeded the handler execution budget of 240s`` is a false sentence for
    a run the caller bounded at 1 s, and it points the reader at the contract
    rather than at their own flag. Misattributing a deadline is the same
    class of defect as the original refusal naming an absent terminal.
    """
    handler = HandlerDelegateSkill(
        dispatch_port=_NeverReturningDispatchPort(),  # type: ignore[arg-type]
        budget=ModelDelegateSkillHandlerBudget(
            max_handler_duration_seconds=_CONTRACT_BUDGET_SECONDS
        ),
    )

    terminal = await asyncio.wait_for(
        handler.handle(_request(requested_timeout_seconds=1)), timeout=30.0
    )

    assert isinstance(terminal, ModelDelegateSkillFailed)
    message = terminal.error_message or ""
    attribution_failure = (
        "OMN-19125: the timeout terminal did not attribute the bound to the "
        f"caller's requested deadline. Message: {message!r}"
    )
    assert "1s" in message, attribution_failure
    assert "requested" in message.lower(), attribution_failure
    # Naming the contract budget is fine and useful -- the message says it was
    # NOT reached, which is how a reader rules out raising it. What must not
    # appear is the sentence CLAIMING the budget was exceeded, because that is
    # the false one for this run.
    assert "exceeded the handler execution budget" not in message, (
        "OMN-19125: the timeout terminal blamed the contract budget for a "
        f"bound the caller set. Message: {message!r}"
    )
    assert f"{_CONTRACT_BUDGET_SECONDS}s) was not reached" in message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_caller_cannot_loosen_the_contract_budget() -> None:
    """``min``, not ``or``. A caller may tighten and never extend.

    Without this the flag becomes a way to raise the handler's bound past the
    consumer's eviction deadline from the wire, re-arming the OMN-15504
    livelock that a container restart cannot clear.
    """
    handler = HandlerDelegateSkill(
        dispatch_port=_NeverReturningDispatchPort(),  # type: ignore[arg-type]
        budget=ModelDelegateSkillHandlerBudget(max_handler_duration_seconds=1),
    )

    started = time.monotonic()
    terminal = await asyncio.wait_for(
        handler.handle(_request(requested_timeout_seconds=3600)), timeout=30.0
    )
    elapsed = time.monotonic() - started

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "timeout"
    assert elapsed < 15.0, (
        "OMN-19125: a caller-supplied 3600 s deadline extended the 1 s "
        f"contract budget to {elapsed:.1f}s. The budget is a ceiling."
    )
    assert f"{1}s" in (terminal.error_message or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_absent_caller_deadline_leaves_the_budget_exactly_as_it_was() -> None:
    """Every existing caller is byte-for-byte unchanged.

    ``None`` resolves to the contract budget alone, and the refusal still
    reads as the OMN-15504 one, because for that caller nothing has changed.
    """
    handler = HandlerDelegateSkill(
        dispatch_port=_NeverReturningDispatchPort(),  # type: ignore[arg-type]
        budget=ModelDelegateSkillHandlerBudget(max_handler_duration_seconds=1),
    )

    terminal = await asyncio.wait_for(handler.handle(_request()), timeout=30.0)

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "timeout"
    message = terminal.error_message or ""
    assert "handler execution budget of 1s" in message, (
        "OMN-19125: the unbounded-caller refusal text changed, so a consumer "
        f"matching on it breaks. Message: {message!r}"
    )
    assert "requested" not in message.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_deadline_that_is_not_reached_does_not_disturb_the_dispatch() -> None:
    """The bound is a deadline, not a parameter the port has to learn.

    A run that finishes inside its deadline completes normally, and the
    dispatch port's keyword set is unchanged -- the deadline is enforced by
    the handler's own ``asyncio.wait_for``, which is the one place that binds
    on BOTH the runtime and the local in-process transport.
    """
    port = _FastDispatchPort()
    handler = HandlerDelegateSkill(
        dispatch_port=port,  # type: ignore[arg-type]
        budget=ModelDelegateSkillHandlerBudget(
            max_handler_duration_seconds=_CONTRACT_BUDGET_SECONDS
        ),
    )

    terminal = await asyncio.wait_for(
        handler.handle(_request(requested_timeout_seconds=300)), timeout=30.0
    )

    assert terminal.status == "completed"
    assert "requested_timeout_seconds" not in port.seen, (
        "OMN-19125: the deadline leaked into the dispatch port signature. It "
        "is the handler's bound; the port already clamps the inference rung "
        "by its own contract ceiling (OMN-18852)."
    )
