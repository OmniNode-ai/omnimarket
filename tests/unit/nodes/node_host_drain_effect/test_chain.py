# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of both verbs, each chain under one correlation id (OMN-22905)."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import (
    FakeRunnerProbe,
    FakeRunnerStopper,
    FixedLaneSource,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import InMemoryDrainStore
from omnimarket.nodes.node_host_drain_effect.handlers.handler_host_drain import (
    HandlerHostDrain,
)
from omnimarket.nodes.node_host_drain_effect.handlers.handler_idle_runner_stop import (
    HandlerIdleRunnerStop,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumRunnerActivity,
    ModelHostDrainRequest,
    ModelIdleRunnerStopRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostDrainOverlay

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


def _drain(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> HandlerHostDrain:
    return HandlerHostDrain(
        overlay.drain,
        store=InMemoryDrainStore(),
        lanes=FixedLaneSource({"example-host-a": 1}),
        sink=sink,
        clock=lambda: NOW,
        backend="in_memory",
    )


def _runner(
    overlay: ModelHostDrainOverlay,
    sink: InMemoryEmissionSink,
    state: EnumRunnerActivity,
) -> HandlerIdleRunnerStop:
    probe = FakeRunnerProbe({"example-runner-1": state})
    return HandlerIdleRunnerStop(
        overlay.runner_stop,
        probe=probe,
        stopper=FakeRunnerStopper(probe),
        store=InMemoryDrainStore(),
        sink=sink,
        clock=lambda: NOW,
        backend="in_memory",
    )


def test_chain_drain_golden_is_a_start_then_a_success_under_one_correlation_id(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    _drain(overlay, sink).handle(
        ModelHostDrainRequest(
            op=EnumDrainOp.DRAIN, host_name="example-host-a", correlation_id="golden-1"
        )
    )
    assert [(e.phase, e.correlation_id, e.node) for e in sink.events] == [
        ("start", "golden-1", overlay.drain.chain_node),
        ("success", "golden-1", overlay.drain.chain_node),
    ]
    assert {e.piece for e in sink.events} == {overlay.drain.piece}


def test_chain_drain_error_is_a_start_then_a_typed_failure_under_one_correlation_id(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    _drain(overlay, sink).handle(
        ModelHostDrainRequest(
            op=EnumDrainOp.DRAIN, host_name="not-a-host", correlation_id="error-1"
        )
    )
    assert [(e.phase, e.correlation_id) for e in sink.events] == [
        ("start", "error-1"),
        ("failure", "error-1"),
    ]
    assert sink.events[-1].failure_code == overlay.drain.codes.unknown_host


def test_chain_refused_start_on_a_draining_host_is_the_error_chain(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    handler = _drain(overlay, sink)
    handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="example-host-a")
    )
    sink.events.clear()
    handler.handle(
        ModelHostDrainRequest(
            op=EnumDrainOp.CHECK_START, host_name="example-host-a", correlation_id="s-1"
        )
    )
    assert [(e.phase, e.correlation_id) for e in sink.events] == [
        ("start", "s-1"),
        ("failure", "s-1"),
    ]
    assert sink.events[-1].failure_code == overlay.drain.codes.host_draining


def test_chain_runner_stop_golden_and_error_each_hold_one_correlation_id(
    overlay: ModelHostDrainOverlay,
) -> None:
    golden = InMemoryEmissionSink()
    _runner(overlay, golden, EnumRunnerActivity.IDLE).handle(
        ModelIdleRunnerStopRequest(runner_name="example-runner-1", correlation_id="g-2")
    )
    assert [(e.phase, e.correlation_id, e.node) for e in golden.events] == [
        ("start", "g-2", overlay.runner_stop.chain_node),
        ("success", "g-2", overlay.runner_stop.chain_node),
    ]
    error = InMemoryEmissionSink()
    _runner(overlay, error, EnumRunnerActivity.BUSY).handle(
        ModelIdleRunnerStopRequest(runner_name="example-runner-1", correlation_id="e-2")
    )
    assert [(e.phase, e.correlation_id) for e in error.events] == [
        ("start", "e-2"),
        ("failure", "e-2"),
    ]
    assert error.events[-1].failure_code == overlay.runner_stop.codes.runner_busy


def test_chain_correlation_id_is_minted_when_the_caller_gives_none(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    result = _drain(overlay, sink).handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="example-host-a")
    )
    assert result.correlation_id
    assert {e.correlation_id for e in sink.events} == {result.correlation_id}
