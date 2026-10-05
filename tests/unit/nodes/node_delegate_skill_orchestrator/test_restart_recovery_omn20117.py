# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable intake, process loss, late inner completion and bound expiry."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import (
    ModelDelegationCompleted,
    ModelDelegationFailed,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.cli.delegate_terminal_resolver import resolve_delegate_terminal
from omnibase_infra.runtime.dispatch_envelope_context import bind_dispatch_envelope
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegation_reaper,
    handler_delegation_recovery,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    _request_reap_context,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegation_reaper import (
    HandlerDelegationReaper,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegation_recovery import (
    HandlerDelegationRecovery,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_handler_execution_budget import (
    ModelDelegationReaperConfig,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
    _CONTRACT_PATH,
    CLAIMS_TABLE,
    DelegationClaimPort,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit


class _RuntimeKilled(BaseException):
    """A process loss cannot execute the graceful CancelledError branch."""


class _KilledDispatch:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.kill = asyncio.Event()

    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        self.started.set()
        await self.kill.wait()
        raise _RuntimeKilled


def _tick(now: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        correlation_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        scheduler_id="restart-recovery-test",
        tick_interval_ms=1000,
    )


async def _restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "claims.sqlite"
    port = DelegationClaimPort(database=SqliteDatabaseAdapter(db_path))
    request = ModelDelegateSkillRequest(
        prompt="the original prompt",
        task_type="document",
        source="codex",
        correlation_id=uuid4(),
        tenant_id="test-tenant",
        session_id=str(uuid4()),
        metadata={"ticket_id": "OMN-20117", "caller_lane": "restart-test"},
    )
    delivery_id = uuid4()
    dispatch = _KilledDispatch()
    handler = HandlerDelegateSkill(dispatch_port=dispatch, idempotency_port=port)
    with bind_dispatch_envelope(
        ModelEventEnvelope[object](
            envelope_id=delivery_id,
            payload={},
            correlation_id=request.correlation_id,
        )
    ):
        task = asyncio.create_task(handler.handle(request))
    await asyncio.wait_for(dispatch.started.wait(), timeout=5)
    dispatch.kill.set()
    with pytest.raises(_RuntimeKilled):
        await task
    # Drop every handler and adapter. Only the SQLite file crosses the restart.
    db = SqliteDatabaseAdapter(db_path)
    port = DelegationClaimPort(database=db)
    (claim,) = port.pending_claims(correlation_id=request.correlation_id)
    new_instance = uuid4()
    for module in (handler_delegation_reaper, handler_delegation_recovery):
        monkeypatch.setattr(module, "DELEGATION_RUNTIME_INSTANCE_ID", new_instance)
    return db, port, request, delivery_id, claim.context


def _inner(request: ModelDelegateSkillRequest, *, failed: bool = False):
    cls = ModelDelegationFailed if failed else ModelDelegationCompleted
    return cls(
        correlation_id=request.correlation_id,
        task_type=request.task_type,
        model_used="test-model",
        endpoint_url="http://test.invalid",
        content="" if failed else "the late answer",
        quality_passed=not failed,
        latency_ms=10,
        fallback_to_claude=False,
        failure_reason="provider failed" if failed else "",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("inner_failed", [False, True])
async def test_inner_terminal_after_restart_wins_one_outer_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inner_failed: bool
) -> None:
    db, port, request, delivery_id, ctx = await _restart(tmp_path, monkeypatch)
    recovery = HandlerDelegationRecovery(port=port)
    inner = _inner(request, failed=inner_failed)
    terminal = await recovery.handle(inner)
    expected = ModelDelegateSkillFailed if inner_failed else ModelDelegateSkillCompleted
    assert isinstance(terminal, expected)
    assert terminal.command_id == delivery_id
    assert terminal.correlation_id == request.correlation_id
    assert terminal.tenant_id == request.tenant_id
    assert terminal.ticket_id == "OMN-20117"
    assert terminal.caller_lane == "restart-test"
    assert terminal.session_id == request.session_id
    assert terminal.prompt_text == request.prompt
    if not inner_failed:
        assert terminal.response == "the late answer"
    replayed = await recovery.handle(inner)
    assert replayed is None
    reaper = HandlerDelegationReaper(port=port)
    reaped = await reaper.handle(_tick(ctx.deadline_at + timedelta(seconds=1)))
    assert reaped is None
    published = [event for event in (terminal, replayed) if event is not None]
    if reaped is not None:
        published.extend(reaped.events)
    assert len(published) == 1
    assert len(db.query(CLAIMS_TABLE, {"delivery_id": f"slot:{delivery_id}"})) == 1
    assert resolve_delegate_terminal(terminal.model_dump(mode="json")).status == (
        "failed" if inner_failed else "completed"
    )


@pytest.mark.asyncio
async def test_restart_withheld_completion_expires_with_one_typed_restart_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, port, request, delivery_id, ctx = await _restart(tmp_path, monkeypatch)
    reaper = HandlerDelegationReaper(
        port=port,
        config=ModelDelegationReaperConfig(
            grace_seconds=60, max_reaps_per_tick=25, scan_interval_seconds=1
        ),
    )
    assert await reaper.handle(_tick(ctx.deadline_at - timedelta(seconds=1))) is None
    output = await reaper.handle(_tick(ctx.deadline_at))
    assert output is not None
    (terminal,) = output.events
    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "failed"
    assert terminal.command_id == delivery_id
    assert terminal.terminal_failure_cause.value == "runtime_shutdown"
    assert "restarted" in terminal.error_message
    declared = yaml.safe_load(_CONTRACT_PATH.read_text())
    assert (
        terminal.terminal_failure_cause.value
        in declared["outputs"]["terminal_failure_cause"]["enum"]
    )
    assert await reaper.handle(_tick(ctx.deadline_at + timedelta(seconds=1))) is None
    assert await HandlerDelegationRecovery(port=port).handle(_inner(request)) is None
    reply = resolve_delegate_terminal(terminal.model_dump(mode="json"))
    assert reply.status == "failed"
    assert reply.terminal_failure_cause == "runtime_shutdown"


@pytest.mark.asyncio
async def test_recovery_does_not_steal_a_live_waiter_or_an_ambiguous_correlation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, port, request, _, ctx = await _restart(tmp_path, monkeypatch)
    recovery = HandlerDelegationRecovery(port=port)
    monkeypatch.setattr(
        handler_delegation_recovery,
        "DELEGATION_RUNTIME_INSTANCE_ID",
        ctx.runtime_instance_id,
    )
    assert await recovery.handle(_inner(request)) is None
    monkeypatch.setattr(
        handler_delegation_recovery, "DELEGATION_RUNTIME_INSTANCE_ID", uuid4()
    )
    port.claim(
        delivery_id=uuid4(), correlation_id=request.correlation_id, reap_context=ctx
    )
    assert await recovery.handle(_inner(request)) is None
    assert len(port.pending_claims(correlation_id=request.correlation_id)) == 2


def test_recovery_subscriptions_route_both_inner_terminal_types() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    for outcome in ("completed", "failed"):
        topic = contract["delegation_runtime_dispatch"]["topics"][outcome]
        operation = f"delegate-skill.recover_{outcome}"
        assert {"topic": topic, "operation": operation} in contract[
            "input_subscriptions"
        ]
        assert topic in contract["event_bus"]["subscribe_topics"]
        route = next(
            r
            for r in contract["handler_routing"]["handlers"]
            if r["operation"] == operation
        )
        assert route["handler"]["name"] == "HandlerDelegationRecovery"


def test_intake_context_keeps_the_original_request_and_incarnation() -> None:
    request = ModelDelegateSkillRequest(prompt="test", task_type="test", source="codex")
    context = _request_reap_context(request)
    assert context is not None
    assert context.request == request
    assert context.runtime_instance_id is not None


@pytest.mark.asyncio
async def test_a_completion_after_the_bound_cannot_replace_the_restart_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, port, request, _, ctx = await _restart(tmp_path, monkeypatch)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return ctx.deadline_at.astimezone(tz or UTC)

    monkeypatch.setattr(handler_delegation_recovery, "datetime", _Clock)
    assert await HandlerDelegationRecovery(port=port).handle(_inner(request)) is None
    output = await HandlerDelegationReaper(port=port).handle(_tick(ctx.deadline_at))
    assert output is not None
    assert output.events[0].terminal_failure_cause.value == "runtime_shutdown"
