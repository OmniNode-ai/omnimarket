# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real handler boundaries must leave a complete trace of an interrupted run.

Failure modes enumerated before the fix:
* budget expiry, shutdown and an exception interrupt inference or the gate;
* a gate interruption loses the provider call which already succeeded;
* the interrupted step/terminal is absent, duplicated or names itself;
* two cancelled runs share a counter or a finalizer;
* failed step delivery masks the handler's original terminal;
* a final failed model hides an earlier failed model on the same route.

Only the provider and quality evaluator wait or fail. The handler, local
dispatch, step recorder, SQLite store and published event path are real.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.dispatch_progress import (
    current_dispatch_progress,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_earlier_model_attempt import (
    ModelEarlierModelAttempt,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_observation import (
    ModelLlmDelegationCallObservation,
)
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_escalation_omn13849 import (
    _install_ladder,
)
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_run_steps_omn19987 import (
    _PerTierEffect,
    _port,
    _RaisingPublisher,
    _steps,
    _stored,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="explain the tradeoff",
        task_type="research",
        source="claude-code",
        correlation_id=uuid4(),
        requested_timeout_seconds=1,
    )


def _budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handler_delegate_skill,
        "resolve_task_class_execution_budget",
        lambda _: SimpleNamespace(
            task_class_timeout_ceiling_seconds=1,
            terminal_delivery_margin_seconds=1,
        ),
    )


@pytest.mark.parametrize("stage", ["inference", "quality_gate"])
@pytest.mark.parametrize("ending", ["budget", "shutdown", "exception"])
async def test_handler_interruption_closes_the_actual_step_and_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, ending: str
) -> None:
    _install_ladder(monkeypatch, max_escalations=0)
    _budget(monkeypatch)
    ready = asyncio.Event()

    async def interrupted(*_: Any, **__: Any) -> Any:
        ready.set()
        if ending == "exception":
            raise RuntimeError("provider or gate exploded")
        await asyncio.Event().wait()
        raise AssertionError("the handler must stop the pending stage")

    port, publisher, db_path = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"local"}))
    )
    if stage == "inference":
        port._effect_process_boundary = True
        monkeypatch.setattr(
            port_mod, "_run_effect_handler_with_killable_timeout", interrupted
        )
    else:
        monkeypatch.setattr(port, "_evaluate_quality_gate", interrupted)
    request = _request()
    task = asyncio.create_task(HandlerDelegateSkill(dispatch_port=port).handle(request))
    await asyncio.wait_for(ready.wait(), timeout=5)
    if ending == "shutdown":
        task.cancel()
    terminal = await asyncio.wait_for(task, timeout=5)
    assert terminal.status in {"failed", "timeout"}
    assert terminal.terminal_failure_cause is not None
    cause = terminal.terminal_failure_cause.value
    if ending == "budget":
        assert cause == "timeout"
    elif ending == "shutdown":
        assert cause == "runtime_shutdown"
    else:
        assert "exploded" in (terminal.error_message or "")

    steps = _steps(publisher, request.correlation_id)
    expected = ["request", "routing_decision", "provider_call"]
    if stage == "quality_gate":
        expected.append("quality_gate")
    expected.append("terminal")
    assert [s["step_kind"] for s in steps] == expected
    assert [s["step_sequence"] for s in steps] == list(range(1, len(steps) + 1))
    failed, last = steps[-2:]
    assert failed["outcome"] == last["outcome"] == "failed"
    assert failed["failure_cause"] == last["failure_cause"] == cause
    assert last["failing_step_sequence"] == failed["step_sequence"]
    assert failed["failing_step_sequence"] is None
    if stage == "quality_gate":
        assert steps[2]["outcome"] == "succeeded"
    stored = _stored(db_path, request.correlation_id)
    assert [(r["step_kind"], r["outcome"]) for r in stored] == [
        (s["step_kind"], s["outcome"]) for s in steps
    ]


async def test_two_concurrent_shutdowns_keep_their_own_step_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=0)
    _budget(monkeypatch)
    ready = asyncio.Event()
    started = 0

    async def pending(*_: Any, **__: Any) -> Any:
        nonlocal started
        started += 1
        if started == 2:
            ready.set()
        await asyncio.Event().wait()
        raise AssertionError("shutdown must cancel both calls")

    monkeypatch.setattr(port_mod, "_run_effect_handler_with_killable_timeout", pending)
    port, publisher, _ = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"local"}))
    )
    port._effect_process_boundary = True
    requests = [_request(), _request()]
    tasks = [
        asyncio.create_task(HandlerDelegateSkill(dispatch_port=port).handle(request))
        for request in requests
    ]
    await asyncio.wait_for(ready.wait(), timeout=5)
    for task in tasks:
        task.cancel()
    terminals = await asyncio.gather(*tasks)
    for request, terminal in zip(requests, terminals, strict=True):
        assert terminal.correlation_id == request.correlation_id
        steps = _steps(publisher, request.correlation_id)
        assert [s["step_sequence"] for s in steps] == [1, 2, 3, 4]
        assert steps[-1]["step_kind"] == "terminal"
        assert steps[-1]["failure_cause"] == "runtime_shutdown"


async def test_failed_step_publication_preserves_the_shutdown_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=0)
    _budget(monkeypatch)
    ready = asyncio.Event()

    async def pending(*_: Any, **__: Any) -> Any:
        ready.set()
        await asyncio.Event().wait()
        raise AssertionError("shutdown must cancel the call")

    monkeypatch.setattr(port_mod, "_run_effect_handler_with_killable_timeout", pending)
    port, _, db_path = _port(
        tmp_path,
        _PerTierEffect(pass_tiers=frozenset({"local"})),
        _RaisingPublisher(),
    )
    port._effect_process_boundary = True
    request = _request()
    task = asyncio.create_task(HandlerDelegateSkill(dispatch_port=port).handle(request))
    await asyncio.wait_for(ready.wait(), timeout=5)
    task.cancel()
    terminal = await asyncio.wait_for(task, timeout=5)
    assert terminal.terminal_failure_cause is not None
    assert terminal.terminal_failure_cause.value == "runtime_shutdown"
    rows = _stored(db_path, request.correlation_id)
    assert [row["step_kind"] for row in rows] == [
        "request",
        "routing_decision",
        "provider_call",
        "terminal",
    ]


async def test_two_failed_models_keep_both_calls_in_the_failure_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=0)

    def failed_fallback(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
            error_message="second model unavailable",
            served_model_id="second-model",
            earlier_model_attempts=(
                ModelEarlierModelAttempt(
                    model_id="first-model",
                    failure_class=EnumDelegationFailureClass.RATE_LIMITED,
                    error_message="first model throttled",
                    http_status=429,
                ),
            ),
        )

    port, publisher, _ = _port(tmp_path, failed_fallback)
    request = _request()
    terminal = await HandlerDelegateSkill(dispatch_port=port).handle(request)
    assert terminal.status == "failed"
    steps = _steps(publisher, request.correlation_id)
    calls = [s for s in steps if s["step_kind"] == "provider_call"]
    assert [(s["model_id"], s["failure_cause"]) for s in calls] == [
        ("first-model", "rate_limited"),
        ("second-model", "model_unavailable"),
    ]
    assert [s["attempt_number"] for s in calls] == [1, 2]
    assert steps[-1]["failing_step_sequence"] == calls[-1]["step_sequence"]


@pytest.mark.parametrize("second_call_started", [False, True])
async def test_shutdown_during_stalled_discovery_never_invents_a_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_call_started: bool
) -> None:
    _install_ladder(monkeypatch, max_escalations=0)
    _budget(monkeypatch)
    ready = asyncio.Event()

    async def stalled(*_: Any, **__: Any) -> Any:
        progress = current_dispatch_progress.get()
        assert progress is not None
        progress.in_flight_calls.extend(
            [
                ModelLlmDelegationCallObservation(
                    phase="started", model_id="first-model"
                ),
                ModelLlmDelegationCallObservation(
                    phase="finished",
                    model_id="first-model",
                    success=False,
                    failure_class="rate_limited",
                    http_status=429,
                    error_message="first call throttled",
                ),
            ]
        )
        if second_call_started:
            progress.in_flight_calls.append(
                ModelLlmDelegationCallObservation(
                    phase="started", model_id="second-model"
                )
            )
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(port_mod, "_run_effect_handler_with_killable_timeout", stalled)
    port, publisher, db_path = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"local"}))
    )
    port._effect_process_boundary = True
    request = _request()
    task = asyncio.create_task(HandlerDelegateSkill(dispatch_port=port).handle(request))
    await asyncio.wait_for(ready.wait(), timeout=5)
    task.cancel()
    terminal = await asyncio.wait_for(task, timeout=5)
    assert terminal.terminal_failure_cause is not None
    assert terminal.terminal_failure_cause.value == "runtime_shutdown"
    steps = _steps(publisher, request.correlation_id)
    calls = [s for s in steps if s["step_kind"] == "provider_call"]
    expected = [("first-model", "rate_limited")]
    if second_call_started:
        expected.append(("second-model", "runtime_shutdown"))
    assert [(s["model_id"], s["failure_cause"]) for s in calls] == expected
    assert [s["step_sequence"] for s in steps] == list(range(1, len(steps) + 1))
    assert steps[-1]["failing_step_sequence"] == calls[-1]["step_sequence"]
    assert len(_stored(db_path, request.correlation_id)) == len(steps)
