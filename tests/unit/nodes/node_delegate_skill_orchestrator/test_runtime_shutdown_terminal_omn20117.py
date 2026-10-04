# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20117: a redeploy that lands mid-flight must not leave the caller silent.

Measured on the .201 dev lane: a collaborator's delegate request arrived at
2026-09-29T23:41:44.8Z, the deploy agent recreated every runtime container at
23:41:45Z, and the caller waited out its 300 s window with no delegate-skill
terminal at all. Reproduced on 2026-09-30T11:15Z with a warm restart of the
runtime that owns this node: the CLI exited after 300 s with no resolvable
terminal.

On shutdown the runtime now drains running dispatches and then CANCELS the
ones still running while its producer is open (omnibase_infra OMN-20117). This
handler answers that cancellation with a typed failure terminal naming the
restart, so the caller is told, and records it against the delivery, so a
redelivery of the same record answers with the same terminal instead of
running the delegation a second time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.dispatch_envelope_context import bind_dispatch_envelope

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
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
    ModelDelegationClaimOutcome,
    ModelDelegationTerminalOutcome,
)

pytestmark = pytest.mark.unit

# Spelled as a literal: reaching for a member the installed core does not have
# yet would fail this module at import rather than fail the test.
RUNTIME_SHUTDOWN = "runtime_shutdown"


class _InFlightDispatchPort:
    """Accepts the dispatch and never resolves: the run the redeploy cuts off."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0

    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _MemoryClaimPort:
    """The durable claim store, in memory: one row per delivery."""

    def __init__(self) -> None:
        self.rows: dict[UUID, dict[str, object] | None] = {}

    def claim(
        self,
        *,
        delivery_id: UUID,
        correlation_id: UUID,
        reap_context: object | None = None,
    ) -> ModelDelegationClaimOutcome:
        if delivery_id not in self.rows:
            self.rows[delivery_id] = None
            return ModelDelegationClaimOutcome(won=True)
        return ModelDelegationClaimOutcome(
            won=False, served_terminal=self.rows[delivery_id]
        )

    def record_terminal(
        self, *, delivery_id: UUID, terminal: dict[str, object]
    ) -> ModelDelegationTerminalOutcome:
        self.rows[delivery_id] = terminal
        return ModelDelegationTerminalOutcome(won=True, held=None)


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="write a long essay",
        task_type="document",
        source="claude-code",
        correlation_id=uuid4(),
    )


def _handler(
    monkeypatch: pytest.MonkeyPatch,
    port: object,
    claims: _MemoryClaimPort | None = None,
) -> HandlerDelegateSkill:
    monkeypatch.setattr(
        handler_delegate_skill,
        "resolve_task_class_execution_budget",
        lambda _task_type: SimpleNamespace(
            task_class_timeout_ceiling_seconds=240,
            terminal_delivery_margin_seconds=30,
        ),
    )
    return HandlerDelegateSkill(
        dispatch_port=port,  # type: ignore[arg-type]
        idempotency_port=claims,
    )


async def _cancel_mid_flight(
    handler: HandlerDelegateSkill,
    port: _InFlightDispatchPort,
    request: ModelDelegateSkillRequest,
) -> object:
    """Run the handler as the runtime does, and cancel it the way shutdown does."""
    task = asyncio.create_task(handler.handle(request))
    await asyncio.wait_for(port.started.wait(), timeout=5)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=5)
    assert done, "the handler must settle promptly once cancelled"
    assert not task.cancelled(), (
        "OMN-20117: the handler let the runtime's shutdown cancellation escape, "
        "so no terminal was returned for the wiring to publish and the caller "
        "waited out its window in silence"
    )
    return task.result()


@pytest.mark.asyncio
async def test_a_cancelled_run_returns_a_failed_terminal_naming_the_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _InFlightDispatchPort()
    request = _request()

    terminal = await _cancel_mid_flight(_handler(monkeypatch, port), port, request)

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.correlation_id == request.correlation_id
    assert terminal.terminal_failure_cause is not None
    assert terminal.terminal_failure_cause.value == RUNTIME_SHUTDOWN
    assert terminal.status == "failed"
    assert "runtime" in (terminal.error_message or "")


@pytest.mark.asyncio
async def test_a_redelivery_after_the_restart_answers_without_dispatching_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly one terminal story per delivery: the redelivery replays it."""
    claims = _MemoryClaimPort()
    port = _InFlightDispatchPort()
    request = _request()
    # One delivered record: a redelivery of it carries the same envelope id.
    envelope = ModelEventEnvelope[object](
        envelope_id=uuid4(),
        payload={},
        correlation_id=request.correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type="omnimarket.delegate-skill",
    )

    with bind_dispatch_envelope(envelope):
        first = await _cancel_mid_flight(
            _handler(monkeypatch, port, claims), port, request
        )
        replayed = await _handler(monkeypatch, port, claims).handle(request)

    assert isinstance(first, ModelDelegateSkillFailed)
    assert isinstance(replayed, ModelDelegateSkillFailed)
    assert replayed.terminal_failure_cause == first.terminal_failure_cause
    assert port.calls == 1, "the redelivery must not run the delegation again"


def test_the_contract_declares_the_restart_cause() -> None:
    import yaml

    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
        _CONTRACT_PATH,
    )

    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    declared = contract["outputs"]["terminal_failure_cause"]["enum"]
    assert RUNTIME_SHUTDOWN in declared
    assert EnumDelegationTerminalFailureCause(RUNTIME_SHUTDOWN).value in declared
