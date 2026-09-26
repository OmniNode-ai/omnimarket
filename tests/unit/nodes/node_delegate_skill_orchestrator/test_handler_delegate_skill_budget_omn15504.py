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
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
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
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_handler_execution_budget import (
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


class _DelayedSuccessfulDispatchPort(_FastDispatchPort):
    """Returns a terminal after the execution window but inside delivery margin."""

    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.kwargs: dict[str, object] = {}

    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        self.kwargs = kwargs
        await asyncio.sleep(self.delay_seconds)
        return await super().dispatch(**kwargs)


class _CapturingDispatchPort(_FastDispatchPort):
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        self.kwargs = kwargs
        return await super().dispatch(**kwargs)


class _OutputRefusalDispatchPort(_FastDispatchPort):
    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        result = await super().dispatch(**kwargs)
        result["content"] = ""
        result["output_refusal"] = {
            "reason": "no_schema_conforming_json",
            "output_shape": "json",
            "contract_failure_reasons": ("required property 'answer' is missing",),
        }
        return result


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="write a test",
        task_type="code_generation",
        source="claude-code",
        correlation_id=uuid4(),
    )


def _handler(
    monkeypatch: pytest.MonkeyPatch,
    dispatch_port: object,
    *,
    timeout_seconds: int = 1,
    terminal_delivery_margin_seconds: int = 60,
) -> HandlerDelegateSkill:
    monkeypatch.setattr(
        handler_delegate_skill,
        "resolve_task_class_execution_budget",
        lambda _task_type: SimpleNamespace(
            task_class_timeout_ceiling_seconds=timeout_seconds,
            terminal_delivery_margin_seconds=terminal_delivery_margin_seconds,
        ),
    )
    return HandlerDelegateSkill(dispatch_port=dispatch_port)  # type: ignore[arg-type]


@pytest.mark.unit
def test_request_preserves_an_explicit_execution_timeout() -> None:
    request = ModelDelegateSkillRequest(
        prompt="write a test",
        task_type="code_generation",
        source="claude-code",
        correlation_id=uuid4(),
        requested_timeout_seconds=120,
    )

    assert request.requested_timeout_seconds == 120


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_forwards_resolved_execution_and_delivery_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _CapturingDispatchPort()
    request = _request().model_copy(update={"requested_timeout_seconds": 1})

    terminal = await _handler(monkeypatch, port).handle(request)

    assert terminal.budget_evidence is not None
    assert terminal.budget_evidence.requested_timeout_seconds == 1
    assert terminal.budget_evidence.execution_timeout_seconds == 1
    assert port.kwargs["execution_timeout_seconds"] == 1
    assert port.kwargs["terminal_delivery_margin_seconds"] == 60


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_carries_the_local_typed_output_refusal_to_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = await _handler(monkeypatch, _OutputRefusalDispatchPort()).handle(
        _request()
    )

    assert terminal.response == ""
    assert terminal.output_refusal is not None
    assert terminal.output_refusal.reason == "no_schema_conforming_json"
    assert terminal.output_refusal.contract_failure_reasons == (
        "required property 'answer' is missing",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_refuses_timeout_above_the_task_class_ceiling_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _CapturingDispatchPort()
    request = _request().model_copy(update={"requested_timeout_seconds": 2})

    terminal = await _handler(monkeypatch, port).handle(request)

    assert terminal.budget_evidence is None
    assert terminal.budget_refusal is not None
    assert terminal.budget_refusal.reason == "timeout_exceeds_task_class_ceiling"
    assert terminal.budget_refusal.requested_timeout_seconds == 2
    assert port.kwargs == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_returns_within_its_budget_when_the_port_never_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED before the fix: ``handle()`` parks forever and this times out."""
    port = _NeverReturningDispatchPort()
    handler = _handler(monkeypatch, port, terminal_delivery_margin_seconds=1)

    terminal = await asyncio.wait_for(handler.handle(_request()), timeout=15.0)

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert port.dispatch_started.is_set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_expiry_terminal_names_timeout_as_its_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler-owned budget cancellation is a typed timeout terminal."""
    handler = _handler(
        monkeypatch,
        _NeverReturningDispatchPort(),
        terminal_delivery_margin_seconds=1,
    )

    terminal = await asyncio.wait_for(handler.handle(_request()), timeout=15.0)

    assert terminal.status == "timeout"
    assert terminal.terminal_failure_cause is EnumDelegationTerminalFailureCause.TIMEOUT
    assert terminal.budget_evidence is not None
    assert terminal.budget_evidence.requested_timeout_seconds is None
    assert terminal.budget_evidence.execution_timeout_seconds == 1
    assert "budget" in terminal.error_message.lower()
    assert "1s" in terminal.error_message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_expiry_cancels_the_dispatch_rather_than_orphaning_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abandoned dispatch that keeps running is a leak, not a bound.

    The point of the bound is to return the consumer's poll loop to it. A
    dispatch left running in the background still holds the port's broker
    subscription, so the record after this one inherits the same stall.
    """
    port = _NeverReturningDispatchPort()
    handler = _handler(monkeypatch, port, terminal_delivery_margin_seconds=1)

    await asyncio.wait_for(handler.handle(_request()), timeout=15.0)
    await asyncio.sleep(0)

    assert port.cancelled is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_port_that_resolves_inside_the_budget_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No regression: the bound must not change the successful path."""
    handler = _handler(monkeypatch, _FastDispatchPort(), timeout_seconds=30)

    terminal = await asyncio.wait_for(handler.handle(_request()), timeout=15.0)

    assert terminal.status == "completed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_accepts_one_terminal_arriving_inside_delivery_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delivery margin extends terminal waiting, not model execution time."""
    port = _DelayedSuccessfulDispatchPort(delay_seconds=1.1)
    request = _request().model_copy(update={"requested_timeout_seconds": 1})

    terminal = await asyncio.wait_for(
        _handler(
            monkeypatch,
            port,
            timeout_seconds=1,
            terminal_delivery_margin_seconds=1,
        ).handle(request),
        timeout=4.0,
    )

    assert terminal.status == "completed"
    assert terminal.correlation_id == request.correlation_id
    assert port.kwargs["execution_timeout_seconds"] == 1
    assert port.kwargs["terminal_delivery_margin_seconds"] == 1


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
def test_contract_declares_every_terminal_failure_cause_the_handler_can_emit() -> None:
    """The response contract stays complete as the core enum gains members."""
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    declared = set(raw["outputs"]["terminal_failure_cause"]["enum"])
    emitted = {member.value for member in EnumDelegationTerminalFailureCause}

    assert emitted <= declared


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
