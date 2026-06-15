# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13010: HandlerContextRoiRunner.handle_async must not pin the dispatch loop.

Root cause (broker-verified, probe2 run_id=20260611T2000Z-probe2):
The runtime auto-wiring dispatch callback invokes a *synchronous* ``handle()``
directly on the single effects-container event loop. The runner's ``handle()``
blocks up to ``generation_timeout_seconds`` per arm (240s for 2 arms) on the
correlated terminal-event consumer. While it blocks, every other consumer group
in the same runtime -- including ``node_generation_consumer``, the very consumer
that must produce the terminal the runner awaits -- is starved of poll/heartbeat
and rebalances (mass ``UnknownMemberIdError`` at 20:58:40). Generation therefore
cannot run until after the runner's windows close, so every row is degenerate
(failure_stage=generation, attempt_count=0).

Fix: the runner exposes ``handle_async`` (which the dispatch callback prefers,
omnibase_infra handler_wiring ``_make_dispatch_callback``) that runs the
synchronous, blocking ``handle()`` off the event loop via ``asyncio.to_thread``.
The event loop stays free to service co-resident consumers' heartbeats while the
runner blocks.

These tests drive the real handler entrypoint the runtime uses (``handle_async``)
and assert:
1. ``handle_async`` exists and is a coroutine function (so the dispatch callback
   prefers it over the synchronous ``handle``).
2. While ``handle_async`` is in its blocking consume wait, the event loop makes
   progress on a co-resident task (the loop is NOT pinned) -- the
   anti-starvation invariant.
3. ``handle_async`` returns the same non-degenerate result as ``handle`` for a
   terminal that arrives shortly after publish.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.events.context_roi import EnumFailureStage
from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    HandlerContextRoiRunner,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiArmSpec,
    ModelContextRoiRunRequest,
    ModelContextRoiTask,
)

_CONTRACT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_roi_runner"
    / "contract.yaml"
)

_VALID_EVENT: dict[str, Any] = {
    "attempt_count": 1,
    "contract_passed": True,
    "first_pass_success": True,
    "prompt_tokens": 150,
    "completion_tokens": 80,
    "cost_inference_usd": 0.0,
    "model_id": "Qwen3.6-35B-A3B",
    "provider": "local",
    "endpoint_class": "local-coder",
}


def _make_request() -> ModelContextRoiRunRequest:
    return ModelContextRoiRunRequest(
        run_id="run-omn13010",
        tasks=(
            ModelContextRoiTask(
                task_id="invoice_reconcile",
                task_description="Generate a reconciliation compute node.",
            ),
        ),
        arms=(ModelContextRoiArmSpec(label="off", factor_subset=()),),
        trials_per_cell=1,
        max_attempts=1,
        arm_order_seed=42,
        generation_timeout_seconds=5.0,
    )


def _noop_publisher(topic: str, payload: bytes) -> None:
    return None


def _blocking_consumer(
    block_seconds: float,
) -> Callable[[str, str, float], dict[str, Any] | None]:
    """Consumer that blocks the calling thread for ``block_seconds`` then returns
    a valid terminal event -- mirrors the real OMN-13005 blocking-correlate
    adapter which sleeps the calling thread until a correlated terminal arrives.
    """

    def _consume(topic: str, cid: str, timeout: float) -> dict[str, Any] | None:
        time.sleep(block_seconds)
        return {**_VALID_EVENT, "correlation_id": cid}

    return _consume


class _FakeTerminalSession:
    """Per-topic session fake: only the COMPLETED topic delivers a terminal.

    A topic with ``payload=None`` blocks briefly (stand-in for the real blocking
    correlate) and reports a genuine timeout — mirroring the OMN-13038 race where
    exactly one terminal topic delivers per command.
    """

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = payload
        self.calls: list[str] = []
        self.closed = False

    def wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        self.calls.append("wait")
        if self._payload is None:
            time.sleep(min(timeout_seconds, 0.2))
            return None
        return {**self._payload, "correlation_id": correlation_id}

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True


class _FakeTwoPhaseConsumer:
    def __init__(self) -> None:
        self.sessions: dict[str, _FakeTerminalSession] = {}

    def open(self, terminal_topic: str) -> _FakeTerminalSession:
        payload = _VALID_EVENT if "generation-completed" in terminal_topic else None
        session = _FakeTerminalSession(payload)
        self.sessions[terminal_topic] = session
        return session

    def __call__(
        self, terminal_topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        raise AssertionError("two-phase consumer should use open().wait()")


def _make_handler(
    consumer: Callable[[str, str, float], dict[str, Any] | None],
) -> HandlerContextRoiRunner:
    return HandlerContextRoiRunner(
        event_publisher=_noop_publisher,
        event_consumer=consumer,
        runner_contract_path=_CONTRACT_PATH,
    )


def test_handle_async_exists_and_is_coroutine() -> None:
    """The dispatch callback prefers handle_async; it must be a coroutine fn so
    the runtime offloads the blocking handle() off the dispatch loop."""
    assert hasattr(HandlerContextRoiRunner, "handle_async")
    assert inspect.iscoroutinefunction(HandlerContextRoiRunner.handle_async)


@pytest.mark.asyncio
async def test_handle_async_does_not_pin_event_loop() -> None:
    """Anti-starvation invariant: while handle_async is in its blocking consume
    wait, the event loop must keep servicing co-resident tasks.

    Models the live failure: the runner blocks ~per-arm; a co-resident consumer
    must still get loop time to heartbeat. We run handle_async (whose injected
    consumer blocks 1.0s) concurrently with a heartbeat task that ticks every
    50ms. If the loop were pinned (sync handle() called directly on the loop),
    the heartbeat would record ~0 ticks during the block. With the to_thread
    offload, it records many.
    """
    handler = _make_handler(_blocking_consumer(block_seconds=1.0))

    ticks = 0
    stop = False

    async def _heartbeat() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.05)

    hb_task = asyncio.create_task(_heartbeat())
    # Let the heartbeat establish, then run the blocking runner concurrently.
    await asyncio.sleep(0.05)
    result = await handler.handle_async(_make_request())
    stop = True
    await hb_task

    # The 1.0s block at 50ms cadence yields ~20 ticks if the loop stayed free.
    # A pinned loop yields close to 0 during the block. Require a strong margin.
    assert ticks >= 8, (
        f"event loop appears pinned during handle_async block (ticks={ticks}); "
        "the synchronous handle() is not being offloaded off the dispatch loop"
    )
    # And the result must be non-degenerate.
    assert result.total_trials == 1
    assert result.failed_trials == 0
    row = result.rows[0]
    assert row.failure_stage == EnumFailureStage.NONE
    assert row.attempt_count == 1
    assert row.model_id == "Qwen3.6-35B-A3B"
    assert row.provider == "local"
    assert row.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY


@pytest.mark.asyncio
async def test_handle_async_runs_on_worker_thread() -> None:
    """handle() must execute off the asyncio loop's own thread (proves the
    to_thread offload, not just an await of an inline coroutine)."""
    main_thread = threading.get_ident()
    captured: dict[str, int] = {}

    def _capturing_consumer(topic: str, cid: str, timeout: float) -> dict[str, Any]:
        captured["handle_thread"] = threading.get_ident()
        return {**_VALID_EVENT, "correlation_id": cid}

    handler = _make_handler(_capturing_consumer)
    await handler.handle_async(_make_request())
    assert "handle_thread" in captured
    assert captured["handle_thread"] != main_thread, (
        "handle() ran on the event-loop thread -- it was not offloaded via "
        "asyncio.to_thread; the dispatch loop would be pinned in production"
    )


@pytest.mark.asyncio
async def test_handle_async_result_matches_sync_handle() -> None:
    """handle_async must produce the same row as the synchronous handle()."""
    handler_async = _make_handler(_blocking_consumer(block_seconds=0.0))
    handler_sync = _make_handler(_blocking_consumer(block_seconds=0.0))

    async_result = await handler_async.handle_async(_make_request())
    sync_result = handler_sync.handle(_make_request())

    assert async_result.total_trials == sync_result.total_trials
    assert async_result.failed_trials == sync_result.failed_trials
    assert async_result.rows[0].attempt_count == sync_result.rows[0].attempt_count
    assert async_result.rows[0].model_id == sync_result.rows[0].model_id
    assert async_result.rows[0].failure_stage == sync_result.rows[0].failure_stage


def test_two_phase_terminal_session_closes_after_wait() -> None:
    """Every subscribe-before-publish session (one per terminal topic, OMN-13038)
    must close after the terminal wait — winner and loser alike."""
    consumer = _FakeTwoPhaseConsumer()
    handler = _make_handler(consumer)

    result = handler.handle(_make_request())

    assert result.failed_trials == 0
    completed_sessions = [
        s for t, s in consumer.sessions.items() if "generation-completed" in t
    ]
    assert len(completed_sessions) == 1
    assert completed_sessions[0].calls[0] == "wait"
    assert "close" in completed_sessions[0].calls
    for topic, session in consumer.sessions.items():
        assert session.closed is True, f"session for {topic} leaked (never closed)"
