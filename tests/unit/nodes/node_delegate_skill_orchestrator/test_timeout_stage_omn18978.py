# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A budget cancellation reports its observed stage, including after cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.dispatch_progress import (
    current_dispatch_progress,
    dispatch_stage,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegation_dispatch_progress import (
    DispatchStage,
    ModelDelegationDispatchProgress,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as local_port,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    RuntimeDelegationDispatchPort,
    load_runtime_delegation_dispatch_config,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import resolve_delegation_backend

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def short_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handler_delegate_skill,
        "resolve_task_class_execution_budget",
        lambda _task_type: SimpleNamespace(
            task_class_timeout_ceiling_seconds=1,
            terminal_delivery_margin_seconds=1,
        ),
    )


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="Summarize the supplied facts.",
        task_type="document",
        source="claude-code",
        correlation_id=uuid4(),
    )


class _BlockingBus:
    def __init__(self, blocked_stage: DispatchStage) -> None:
        self.blocked_stage = blocked_stage
        self.unsubscribed = 0

    async def publish(self, *_args: object, **_kwargs: object) -> None:
        if self.blocked_stage == "publish":
            await asyncio.Event().wait()

    async def subscribe(
        self, *_args: object, **_kwargs: object
    ) -> Callable[[], Awaitable[None]]:
        if self.blocked_stage == "subscribe":
            await asyncio.Event().wait()

        async def unsubscribe() -> None:
            self.unsubscribed += 1

        return unsubscribe


@pytest.mark.parametrize("stage", ["subscribe", "publish", "terminal_wait"])
async def test_handler_budget_names_the_bus_stage_and_keeps_it_after_cleanup(
    stage: DispatchStage,
) -> None:
    bus = _BlockingBus(stage)
    config = load_runtime_delegation_dispatch_config().model_copy(
        update={"wait_timeout_seconds": 30}
    )
    handler = HandlerDelegateSkill(
        dispatch_port=RuntimeDelegationDispatchPort(event_bus=bus, config=config)
    )
    request = _request()

    terminal = await asyncio.wait_for(handler.handle(request), timeout=5)

    assert terminal.status == "timeout"
    assert terminal.correlation_id == request.correlation_id
    assert f"stage={stage}" in terminal.error_message
    assert "stage=terminal_cleanup" not in terminal.error_message
    assert terminal.terminal_failure_cause == "timeout"
    assert bus.unsubscribed == (0 if stage == "subscribe" else 3)
    assert current_dispatch_progress.get() is None


@pytest.mark.parametrize("stage", ["subscribe", "publish"])
async def test_runtime_bus_deadline_preserves_the_stage_after_unwinding(
    stage: DispatchStage,
) -> None:
    class DeadlineBus(_BlockingBus):
        async def publish(self, *_args: object, **_kwargs: object) -> None:
            if stage == "publish":
                raise TimeoutError("publisher deadline expired")

        async def subscribe(
            self, *args: object, **kwargs: object
        ) -> Callable[[], Awaitable[None]]:
            if stage == "subscribe":
                raise TimeoutError("subscriber deadline expired")
            return await super().subscribe(*args, **kwargs)

    bus = DeadlineBus("terminal_wait")
    handler = HandlerDelegateSkill(
        dispatch_port=RuntimeDelegationDispatchPort(
            event_bus=bus, config=load_runtime_delegation_dispatch_config()
        )
    )

    terminal = await asyncio.wait_for(handler.handle(_request()), timeout=5)

    assert terminal.status == "timeout"
    assert f"stage={stage}" in terminal.error_message
    assert "stage=terminal_cleanup" not in terminal.error_message
    assert bus.unsubscribed == (0 if stage == "subscribe" else 3)
    assert current_dispatch_progress.get() is None


async def test_runtime_ports_own_wait_timeout_names_its_stage() -> None:
    config = load_runtime_delegation_dispatch_config().model_copy(
        update={"wait_timeout_seconds": 1}
    )
    handler = HandlerDelegateSkill(
        dispatch_port=RuntimeDelegationDispatchPort(
            event_bus=_BlockingBus("terminal_wait"), config=config
        )
    )

    terminal = await asyncio.wait_for(handler.handle(_request()), timeout=5)

    assert terminal.status == "timeout"
    assert "stage=terminal_wait" in terminal.error_message
    assert current_dispatch_progress.get() is None


class _StagedPort:
    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        stage: DispatchStage = kwargs["prompt"]
        try:
            with dispatch_stage(stage):
                await asyncio.Event().wait()
        finally:
            with dispatch_stage("terminal_cleanup"):
                await asyncio.sleep(0)
        raise AssertionError("unreachable")


async def test_concurrent_dispatches_keep_distinct_cancelled_stages() -> None:
    handler = HandlerDelegateSkill(dispatch_port=_StagedPort())
    requests = [
        _request().model_copy(update={"prompt": stage})
        for stage in ("inference", "quality_gate")
    ]

    terminals = await asyncio.wait_for(
        asyncio.gather(*(handler.handle(request) for request in requests)), timeout=5
    )

    for request, terminal in zip(requests, terminals, strict=True):
        assert terminal.status == "timeout"
        assert terminal.correlation_id == request.correlation_id
        assert f"stage={request.prompt}" in terminal.error_message
    assert current_dispatch_progress.get() is None


class _AcceptingPort:
    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        with dispatch_stage("inference"):
            await asyncio.sleep(0)
        return {
            "status": "completed",
            "content": "The answer.",
            "model_name": "test-model",
            "quality_gate_passed": True,
            "quality_score": 1.0,
        }


async def test_accepting_dispatch_preserves_the_answer_and_resets_progress() -> None:
    request = _request()
    terminal = await HandlerDelegateSkill(dispatch_port=_AcceptingPort()).handle(
        request
    )

    assert terminal.status == "completed"
    assert terminal.response == "The answer."
    assert terminal.correlation_id == request.correlation_id
    assert terminal.error_message == ""
    assert current_dispatch_progress.get() is None


@pytest.mark.parametrize("stage", ["effect_boot", "inference", "quality_gate"])
async def test_local_attempt_records_the_stage_of_the_cancelled_await(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: DispatchStage
) -> None:
    entered = asyncio.Event()

    def effect(request: Any) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="The answer.",
            output_hash="test-output",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )

    async def blocked(*_args: Any, **_kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    port = local_port.LocalDelegationDispatchPort(
        effect_handler=effect,
        effect_process_boundary=stage != "quality_gate",
        evidence_db_path=tmp_path / "evidence.sqlite",
    )
    if stage == "effect_boot":
        context = MagicMock()
        context.Process.return_value.is_alive.return_value = True
        monkeypatch.setattr(
            local_port, "_resolve_effect_process_context", lambda: context
        )

        def no_ready_message(_queue: object) -> None:
            entered.set()
            return

        monkeypatch.setattr(local_port, "_read_effect_worker_message", no_ready_message)
    elif stage == "inference":
        monkeypatch.setattr(
            local_port, "_run_effect_handler_with_killable_timeout", blocked
        )
    else:
        monkeypatch.setattr(port, "_evaluate_quality_gate", blocked)
    backend = resolve_delegation_backend(
        "document",
        backends=[
            {
                "backend_id": "local-coder",
                "model_name": "test-model",
                "endpoint_url": "https://inference.example/v1/chat/completions",
                "tier": "local",
                "capabilities": ["document"],
                "max_tokens": 4096,
                "timeout_ms": 60000,
            }
        ],
    )
    progress = ModelDelegationDispatchProgress()
    token = current_dispatch_progress.set(progress)
    try:
        task = asyncio.create_task(
            port._run_single_attempt(
                backend=backend,
                prompt="Summarize the facts.",
                task_type="document",
                correlation_id=uuid4(),
                max_tokens=None,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert progress.cancelled_stage == stage
    finally:
        current_dispatch_progress.reset(token)


async def test_local_watchdog_timeout_names_inference_after_worker_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(local_port, "_observed_child_boot_seconds", None)
    context = MagicMock()
    process = context.Process.return_value
    process.is_alive.return_value = True
    monkeypatch.setattr(local_port, "_resolve_effect_process_context", lambda: context)
    messages = iter([("ready",)])
    monkeypatch.setattr(
        local_port, "_read_effect_worker_message", lambda _queue: next(messages, None)
    )
    monkeypatch.setattr(local_port, "_DISPATCH_TIMEOUT_BUFFER_SECONDS", 0)
    backend = resolve_delegation_backend(
        "document",
        backends=[
            {
                "backend_id": "local-coder",
                "model_name": "test-model",
                "endpoint_url": "https://inference.example/v1/chat/completions",
                "tier": "local",
                "capabilities": ["document"],
                "max_tokens": 4096,
                "timeout_ms": 1,
            }
        ],
    )
    port = local_port.LocalDelegationDispatchPort(
        effect_handler=lambda _request: pytest.fail("worker must never return"),
        evidence_db_path=tmp_path / "evidence.sqlite",
    )

    outcome = await asyncio.wait_for(
        port._run_single_attempt(
            backend=backend,
            prompt="Summarize the facts.",
            task_type="document",
            correlation_id=uuid4(),
            max_tokens=None,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        ),
        timeout=5,
    )

    assert outcome.timeout_result is not None
    assert outcome.timeout_result.failure_class == "timeout"
    assert "stage=inference" in outcome.failure_message
    assert outcome.timeout_result.error_message == outcome.failure_message
    assert outcome.result is None
    assert outcome.gate_result is None
    process.terminate.assert_called()
    context.Queue.return_value.close.assert_called_once()
    context.Queue.return_value.join_thread.assert_called_once()
