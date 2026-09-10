# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15504: the delegate-skill handler's own wall-clock bound.

The shape these tests reproduce, and which no existing test in this package
covers, is a dispatch port that **never returns**. Every other fake in
``tests/unit/nodes/node_delegate_skill_orchestrator/`` resolves promptly, so
``HandlerDelegateSkill.handle()`` has never been exercised against a port that
outruns the consumer's poll deadline.

Live defect this pins (.201 compose dev lane, 2026-09-10T11:56:43Z onward):
``delegation_runtime_dispatch.wait_timeout_seconds`` was ``300`` while the
lane's ``max_poll_interval_ms`` was the ``300000`` default. The handler's
worst case therefore equalled the consumer's eviction deadline exactly, with
zero margin, so aiokafka evicted the consumer at the same instant the handler
finished. The commit was refused with ``UnknownMemberIdError``, the offset
never advanced past 317, the same records were redelivered on rejoin, and the
delegation chain livelocked through four identical ~300 s cycles.

The handler now owns a bound of its own that is strictly below the port's
wait, so the handler is the thing that fires and it fires deterministically.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

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
    load_handler_execution_budget,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)


class _NeverReturningDispatchPort:
    """A port that accepts the dispatch and then never resolves.

    This is the stuck-record shape. It is deliberately NOT a slow port: a port
    that eventually returns would let a loop-body deadline check re-run, which
    is the same false comfort OMN-17137 found in the DLQ replay handler. A
    bound that is only checked between awaits is not a bound.
    """

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
    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        return {
            "status": "completed",
            "content": "ok",
            "delegated_to": "qwen-coder",
            "model_name": "Qwen3-Coder-30B",
            "quality_gate_passed": True,
            "quality_score": 0.95,
        }


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="write a test",
        task_type="code_generation",
        source="claude-code",
        correlation_id=uuid4(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_returns_within_its_budget_when_the_port_never_resolves() -> None:
    """RED before the fix: ``handle()`` parks forever and this times out."""
    port = _NeverReturningDispatchPort()
    handler = HandlerDelegateSkill(
        dispatch_port=port,  # type: ignore[arg-type]
        budget=ModelDelegateSkillHandlerBudget(max_handler_duration_seconds=1),
    )

    terminal = await asyncio.wait_for(handler.handle(_request()), timeout=15.0)

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert port.dispatch_started.is_set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_expiry_terminal_is_a_timeout_not_a_provider_failure() -> None:
    """The cause field names what the PROVIDER reported. It reported nothing.

    ``EnumDelegationTerminalFailureCause`` is documented as naming "the failure
    class the provider actually reported, never an inference drawn from it"
    (OMN-16998). A budget the handler imposed on itself is not a provider fact,
    so classifying it as ``provider_error`` would be exactly the misattribution
    that enum exists to prevent. ``status="timeout"`` carries the truth instead.
    """
    handler = HandlerDelegateSkill(
        dispatch_port=_NeverReturningDispatchPort(),  # type: ignore[arg-type]
        budget=ModelDelegateSkillHandlerBudget(max_handler_duration_seconds=1),
    )

    terminal = await asyncio.wait_for(handler.handle(_request()), timeout=15.0)

    assert terminal.status == "timeout"
    assert terminal.terminal_failure_cause is None
    assert "budget" in terminal.error_message.lower()
    assert "1s" in terminal.error_message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_expiry_cancels_the_dispatch_rather_than_orphaning_it() -> None:
    """An abandoned dispatch that keeps running is a leak, not a bound.

    The point of the bound is to return the consumer's poll loop to it. A
    dispatch left running in the background still holds the port's broker
    subscription, so the record after this one inherits the same stall.
    """
    port = _NeverReturningDispatchPort()
    handler = HandlerDelegateSkill(
        dispatch_port=port,  # type: ignore[arg-type]
        budget=ModelDelegateSkillHandlerBudget(max_handler_duration_seconds=1),
    )

    await asyncio.wait_for(handler.handle(_request()), timeout=15.0)
    await asyncio.sleep(0)

    assert port.cancelled is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_port_that_resolves_inside_the_budget_is_untouched() -> None:
    """No regression: the bound must not change the successful path."""
    handler = HandlerDelegateSkill(
        dispatch_port=_FastDispatchPort(),  # type: ignore[arg-type]
        budget=ModelDelegateSkillHandlerBudget(max_handler_duration_seconds=30),
    )

    terminal = await asyncio.wait_for(handler.handle(_request()), timeout=15.0)

    assert terminal.status == "completed"


@pytest.mark.unit
def test_contract_budget_is_strictly_below_the_ports_wait() -> None:
    """The invariant whose absence caused the live livelock.

    ``wait_timeout_seconds: 300`` equalled the lane's ``max_poll_interval_ms``
    default of ``300000``. A handler bound that is merely *equal* to the thing
    it is supposed to pre-empt pre-empts nothing. Requiring strict inequality
    here makes the margin a contract fact rather than a coincidence that a
    later edit can silently remove.
    """
    budget = load_handler_execution_budget(_CONTRACT_PATH)
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    port_wait = int(raw["delegation_runtime_dispatch"]["wait_timeout_seconds"])

    assert budget.max_handler_duration_seconds < port_wait


@pytest.mark.unit
def test_loader_refuses_a_contract_whose_budget_does_not_pre_empt_the_port(
    tmp_path: Path,
) -> None:
    """A positive control on the check above: the bad shape must be rejected."""
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    raw["handler_execution_budget"]["max_handler_duration_seconds"] = raw[
        "delegation_runtime_dispatch"
    ]["wait_timeout_seconds"]
    bad = tmp_path / "contract.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="strictly less than"):
        load_handler_execution_budget(bad)


@pytest.mark.unit
def test_loader_refuses_a_contract_with_no_budget_declared(tmp_path: Path) -> None:
    """Fail loud on a missing declaration; never fall back to a silent default.

    A default here would be an invisible constant governing an eviction
    deadline, which is the class CLAUDE.md rule 8 forbids.
    """
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    del raw["handler_execution_budget"]
    bad = tmp_path / "contract.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="handler_execution_budget"):
        load_handler_execution_budget(bad)
