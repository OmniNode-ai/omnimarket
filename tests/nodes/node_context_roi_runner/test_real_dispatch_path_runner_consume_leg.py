# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-dispatch-path regression tests for the runner terminal correlate leg.

Ticket: OMN-13012 (lineage OMN-13038, OMN-13099). Surfaces the
EXP1-3_RUNNER_CONSUME_LEG_BLOCKER class proven live on stability image
``c0505521f1fa`` (HALT_RUNNER_CONSUME_LEG_REGRESSION.md).

Live defect (per the halt diagnosis + runner-logs-tail.txt):
  The runner races correlated waits across BOTH terminal topics — completed
  (``node-generation-completed.v1``) and failed (``node-generation-failed.v1``,
  OMN-13038). On the live multi-trial path the FAILED-topic ``wait`` RAISES an
  exception (logged as ``terminal-event consumer: wait failed ... (topic=...
  failed.v1, cid=...):`` with an empty message — a TimeoutError/transport error
  surfacing from the worker loop), while the COMPLETED terminal is on the broker.
  The matrix never advances past the first cell and every row is degenerate.

Why the prior K=1 / "fake that always returns" suite missed it
  ``feedback_real_dispatch_path_tests``: a stubbed two-phase fake whose ``wait``
  simply returns the correlated terminal passes at K=1 but never models the
  cross-topic race where one topic's wait FAILS. The battery (K=10, multi-cell)
  exposed the wedge that probe4 (K=1) raced past.

These tests drive the REAL ``HandlerContextRoiRunner`` through the REAL two-phase
consumer injection protocol (``open(topic) -> session`` then ``session.wait``),
faithfully modeling:
  - the FAILED-topic ``wait`` RAISING (not just returning None), per the live log,
  - the COMPLETED terminal being available on its own topic,
  - K >= 2 trials over multiple cells so matrix ADVANCEMENT is asserted, not just
    a single non-degenerate row.

The defining assertions (RED before the fix, GREEN after):
  (a) the matrix ADVANCES past the first cell (distinct run_orders / cells), and
  (b) a non-degenerate row is produced even when the OTHER terminal topic's wait
      raises (the completed terminal still wins the race).
"""

from __future__ import annotations

import json
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
    TerminalConsumerSessionLike,
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

# Terminal payload modeling what the generation pipeline emits on the COMPLETED
# topic (contract_passed=True). Telemetry fields read back into the row.
_VALID_TERMINAL: dict[str, Any] = {
    "attempt_count": 1,
    "contract_passed": True,
    "first_pass_success": True,
    "prompt_tokens": 150,
    "completion_tokens": 80,
    "cost_inference_usd": 0.001,
    "model_id": "Qwen3.6-35B-A3B",
    "provider": "local",
    "endpoint_class": "local-coder",
}

# Failed terminal: generation ran but contract validation failed (lands on the
# FAILED topic per OMN-13038).
_FAILED_TERMINAL: dict[str, Any] = {
    **_VALID_TERMINAL,
    "contract_passed": False,
    "first_pass_success": False,
    "attempt_count": 3,
}


# ---------------------------------------------------------------------------
# Two-phase fake consumer infrastructure modeling the LIVE cross-topic race.
# ---------------------------------------------------------------------------


class FakeTerminalLog:
    """Per-topic append-only log modeling seek-to-end offset semantics.

    A session opened at ``current_end()`` only sees messages appended at-or-after
    that offset, so a terminal emitted in the publish->wait gap is still delivered
    (the two-phase open-before-publish guarantee, OMN-13012 / probe3).
    """

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._cond = threading.Condition()

    def append(self, payload: dict[str, Any]) -> None:
        with self._cond:
            self._messages.append(payload)
            self._cond.notify_all()

    def current_end(self) -> int:
        with self._cond:
            return len(self._messages)

    def poll(
        self, start_offset: int, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_seconds
        with self._cond:
            idx = start_offset
            while True:
                while idx < len(self._messages):
                    msg = self._messages[idx]
                    idx += 1
                    if str(msg.get("correlation_id")) == correlation_id:
                        return msg
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)


class FakeSession:
    """Session over one topic's log, positioned at a captured pre-publish offset."""

    def __init__(self, log: FakeTerminalLog, start_offset: int) -> None:
        self._log = log
        self._start_offset = start_offset
        self._closed = False

    def wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        if self._closed:
            return None
        result = self._log.poll(self._start_offset, correlation_id, timeout_seconds)
        self.close()
        return result

    def close(self) -> None:
        self._closed = True


# FakeSession satisfies the protocol the handler expects.
assert isinstance(FakeSession(FakeTerminalLog(), 0), TerminalConsumerSessionLike)


class _RaisingSession:
    """Session whose ``wait`` RAISES — models the live FAILED-topic failure.

    The live runner-logs-tail.txt shows, for every trial, exactly one
    ``terminal-event consumer: wait failed ... (topic=...failed.v1, cid=...):``
    with an EMPTY exception message. That is an exception surfacing from the
    worker loop (TimeoutError on the assign/poll submit), NOT a clean None
    timeout. A faithful repro must RAISE here so the race logic is exercised
    exactly as it is live; a fake that returns None hides the defect.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self._closed = False

    def wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        # Mirror the assign-cap delay before the live wait surfaces its error so
        # the COMPLETED topic is genuinely racing (not pre-resolved).
        time.sleep(0.05)
        self.close()
        raise self._exc

    def close(self) -> None:
        self._closed = True


assert isinstance(_RaisingSession(RuntimeError("x")), TerminalConsumerSessionLike)


class CrossTopicRaceConsumer:
    """Two-phase consumer modeling the live cross-topic race.

    ``open(completed_topic)`` -> a real ``FakeSession`` over the completed-terminal
    log; ``open(failed_topic)`` -> a ``_RaisingSession`` (the live FAILED-topic
    wait raises). The completed terminal is appended after publish, so the race
    must let the COMPLETED wait win even though the FAILED wait raises.

    ``opens`` records every opened topic so tests can assert the handler used the
    two-phase path and opened BOTH terminal topics per trial (OMN-13038).
    """

    def __init__(
        self,
        completed_topic: str,
        failed_topic: str,
        completed_log: FakeTerminalLog,
        *,
        failed_exc_factory: Callable[[], BaseException] | None = None,
    ) -> None:
        self._completed_topic = completed_topic
        self._failed_topic = failed_topic
        self._completed_log = completed_log
        self._failed_exc_factory = failed_exc_factory or (
            lambda: TimeoutError()  # empty message, like the live log
        )
        self.opens: list[str] = []

    def open(self, terminal_topic: str) -> Any:
        self.opens.append(terminal_topic)
        if terminal_topic == self._failed_topic:
            return _RaisingSession(self._failed_exc_factory())
        if terminal_topic == self._completed_topic:
            return FakeSession(self._completed_log, self._completed_log.current_end())
        raise AssertionError(f"unexpected terminal topic opened: {terminal_topic}")

    def __call__(
        self, terminal_topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        # Legacy single-call form must not be the path the handler takes for the
        # two-phase fix; if it is, the test will observe a degenerate result.
        if terminal_topic == self._completed_topic:
            session = FakeSession(
                self._completed_log, self._completed_log.current_end()
            )
            return session.wait(correlation_id, timeout_seconds)
        raise self._failed_exc_factory()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _contract_topics() -> tuple[str, str, str]:
    import yaml

    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    gen = data["generation_pipeline"]
    return (
        gen["command_topic"],
        gen["terminal_event_topic"],
        gen["terminal_failed_event_topic"],
    )


def _make_request(
    *,
    tasks: tuple[ModelContextRoiTask, ...] | None = None,
    arms: tuple[ModelContextRoiArmSpec, ...] | None = None,
    trials_per_cell: int = 2,
) -> ModelContextRoiRunRequest:
    return ModelContextRoiRunRequest(
        run_id="run-omn13012-cross-topic-race",
        tasks=tasks
        or (
            ModelContextRoiTask(
                task_id="invoice_reconcile",
                task_description="Generate a compute node that validates invoices.",
            ),
        ),
        arms=arms or (ModelContextRoiArmSpec(label="off", factor_subset=()),),
        trials_per_cell=trials_per_cell,
        max_attempts=2,
        arm_order_seed=42,
        generation_timeout_seconds=5.0,
        contract_hash="test-sha256-omn13012",
    )


def _drive(
    request: ModelContextRoiRunRequest,
    *,
    terminal_for_completed: dict[str, Any] = _VALID_TERMINAL,
    failed_exc_factory: Callable[[], BaseException] | None = None,
    completed_delay_seconds: float = 0.0,
) -> tuple[Any, CrossTopicRaceConsumer, list[tuple[str, bytes]]]:
    command_topic, completed_topic, failed_topic = _contract_topics()
    completed_log = FakeTerminalLog()
    published: list[tuple[str, bytes]] = []

    def _publisher(topic: str, payload: bytes) -> None:
        published.append((topic, payload))
        if command_topic in topic:
            body = json.loads(payload.decode("utf-8"))
            cid = str(body.get("correlation_id", ""))
            if cid:

                def _append(cid: str = cid) -> None:
                    if completed_delay_seconds > 0:
                        time.sleep(completed_delay_seconds)
                    completed_log.append(
                        {**terminal_for_completed, "correlation_id": cid}
                    )

                threading.Thread(target=_append, daemon=True).start()

    consumer = CrossTopicRaceConsumer(
        completed_topic,
        failed_topic,
        completed_log,
        failed_exc_factory=failed_exc_factory,
    )
    handler = HandlerContextRoiRunner(
        event_publisher=_publisher,
        event_consumer=consumer,
        runner_contract_path=_CONTRACT_PATH,
    )
    result = handler.handle(request)
    return result, consumer, published


# ---------------------------------------------------------------------------
# Test 1 (defining repro): completed terminal wins the race even when the
# FAILED-topic wait RAISES, across K>=2 trials over multiple cells.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_completed_wins_race_when_failed_topic_wait_raises_k2_multicell() -> None:
    """RED before the OMN-13012 correlate fix; GREEN after.

    Reproduces the live wedge: the FAILED-topic ``wait`` RAISES (TimeoutError,
    empty message) for every trial; the COMPLETED terminal is on the broker. The
    race must swallow the FAILED wait's exception and let the COMPLETED wait win.

    Asserts (a) MATRIX ADVANCEMENT — 2 tasks x 1 arm x 2 trials = 4 distinct
    run_orders, every cell reached — and (b) every row NON-DEGENERATE
    (failure_stage=none, populated telemetry). A handler that lets the FAILED
    wait's exception abort the trial yields degenerate (generation-failure) rows
    and a stuck matrix.
    """
    request = _make_request(
        tasks=(
            ModelContextRoiTask(
                task_id="invoice_reconcile",
                task_description="Generate a compute node that validates invoices.",
            ),
            ModelContextRoiTask(
                task_id="address_norm",
                task_description="Generate a compute node that normalizes addresses.",
            ),
        ),
        arms=(ModelContextRoiArmSpec(label="off", factor_subset=()),),
        trials_per_cell=2,
    )

    result, consumer, published = _drive(request)

    # (b) Non-degenerate rows.
    assert result.total_trials == 4, (
        f"expected 4 rows (2 tasks x 1 arm x 2 trials), got {result.total_trials}"
    )
    assert result.failed_trials == 0, (
        f"failed_trials={result.failed_trials} — the FAILED-topic wait raising "
        "aborted the trial and produced degenerate rows; the completed terminal "
        "must still win the cross-topic race (EXP1-3_RUNNER_CONSUME_LEG_BLOCKER)"
    )
    for row in result.rows:
        assert row.failure_stage == EnumFailureStage.NONE, (
            f"degenerate row (failure_stage={row.failure_stage!r}): the completed "
            "terminal was not correlated while the failed-topic wait raised"
        )
        assert row.attempt_count == 1
        assert row.model_id == "Qwen3.6-35B-A3B"
        assert row.provider == "local"
        assert row.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY

    # (a) Matrix advanced: every run_order is distinct and covers 1..4 (no cell
    # re-fired, the wedge signature).
    run_orders = sorted(r.run_order for r in result.rows)
    assert run_orders == [1, 2, 3, 4], (
        f"matrix did not advance cleanly — run_orders={run_orders}; the wedge "
        "re-fires a single cell instead of progressing"
    )
    task_ids = {r.task_id for r in result.rows}
    assert task_ids == {"invoice_reconcile", "address_norm"}, (
        f"matrix stuck on a subset of tasks: {task_ids}"
    )

    # The handler used the two-phase path and opened BOTH terminal topics per
    # trial (OMN-13038): 4 trials x 2 topics = 8 opens.
    _command_topic, completed_topic, failed_topic = _contract_topics()
    assert consumer.opens.count(completed_topic) == 4, consumer.opens
    assert consumer.opens.count(failed_topic) == 4, consumer.opens

    # One generation command per trial; one run-completed result emitted.
    command_topic = _command_topic
    gen_commands = [t for t, _ in published if command_topic in t]
    assert len(gen_commands) == 4
    completed_evt = [t for t, _ in published if "context-roi-run-completed" in t]
    assert len(completed_evt) == 1


# ---------------------------------------------------------------------------
# Test 2: the same race, but the generation FAILED (contract_passed=False) lands
# on the FAILED topic while the COMPLETED-topic wait returns None. The runner
# must still correlate it as a VALIDATION failure (not a degenerate GENERATION
# failure that signals the consumer never delivered any terminal).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_failed_terminal_correlated_as_validation_not_degenerate() -> None:
    """A contract_passed=False terminal on the FAILED topic is a VALIDATION row.

    Here the FAILED topic delivers a real terminal and the COMPLETED topic stays
    silent (returns None) — the inverse of the wedge. The runner must record
    failure_stage=VALIDATION (terminal arrived, contract failed), never
    failure_stage=GENERATION (which means no terminal was delivered at all).
    """
    command_topic, _completed_topic, failed_topic = _contract_topics()
    failed_log = FakeTerminalLog()
    published: list[tuple[str, bytes]] = []

    def _publisher(topic: str, payload: bytes) -> None:
        published.append((topic, payload))
        if command_topic in topic:
            body = json.loads(payload.decode("utf-8"))
            cid = str(body.get("correlation_id", ""))
            if cid:
                threading.Thread(
                    target=lambda c=cid: failed_log.append(
                        {**_FAILED_TERMINAL, "correlation_id": c}
                    ),
                    daemon=True,
                ).start()

    completed_log = FakeTerminalLog()  # stays empty -> completed wait returns None

    class _Consumer:
        def __init__(self) -> None:
            self.opens: list[str] = []

        def open(self, terminal_topic: str) -> Any:
            self.opens.append(terminal_topic)
            if terminal_topic == failed_topic:
                return FakeSession(failed_log, failed_log.current_end())
            return FakeSession(completed_log, completed_log.current_end())

        def __call__(
            self, terminal_topic: str, correlation_id: str, timeout_seconds: float
        ) -> dict[str, Any] | None:
            return None

    consumer = _Consumer()
    handler = HandlerContextRoiRunner(
        event_publisher=_publisher,
        event_consumer=consumer,
        runner_contract_path=_CONTRACT_PATH,
    )
    result = handler.handle(_make_request(trials_per_cell=1))

    row = result.rows[0]
    assert row.failure_stage != EnumFailureStage.GENERATION, (
        "failure_stage=generation: the FAILED terminal on the failed topic was "
        "not correlated — degenerate (EXP1-3 defect), not the expected validation"
    )
    assert row.failure_stage == EnumFailureStage.VALIDATION, (
        f"expected failure_stage=validation, got {row.failure_stage!r}"
    )
    assert row.attempt_count == 3


# ---------------------------------------------------------------------------
# Test 3: a slow completed terminal (arrives after the failed wait raises) must
# still be delivered — the race cannot return early on the failed wait's error.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_slow_completed_terminal_delivered_after_failed_wait_raises() -> None:
    """The completed terminal arrives late; the failed wait raises early.

    A handler that returns the moment any wait completes (including by raising)
    would record a degenerate row. The correlated completed terminal arriving
    ~0.2s later must still be delivered within the per-trial timeout.
    """
    result, _consumer, _ = _drive(
        _make_request(trials_per_cell=1),
        completed_delay_seconds=0.2,
    )

    row = result.rows[0]
    assert row.failure_stage == EnumFailureStage.NONE, (
        "slow completed terminal missed — the race returned early on the failed "
        f"wait's exception (failure_stage={row.failure_stage!r})"
    )
    assert row.attempt_count == 1
