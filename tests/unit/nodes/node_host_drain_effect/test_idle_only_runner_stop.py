# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The idle-only runner stop (OMN-22905): a runner that holds a job is never stopped."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import (
    FakeRunnerProbe,
    FakeRunnerStopper,
    StuckRunnerStopper,
)
from omnimarket.nodes.node_host_drain_effect.adapters.runner_ports import (
    ProcRunnerProbe,
)
from omnimarket.nodes.node_host_drain_effect.handlers.handler_idle_runner_stop import (
    HandlerIdleRunnerStop,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumRunnerActivity,
    EnumRunnerStopOutcome,
    ModelIdleRunnerStopRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostDrainOverlay

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


def _handler(
    overlay: ModelHostDrainOverlay,
    probe: object,
    stopper: object,
    sink: InMemoryEmissionSink,
) -> HandlerIdleRunnerStop:
    return HandlerIdleRunnerStop(
        overlay.runner_stop,
        probe=probe,  # type: ignore[arg-type]
        stopper=stopper,  # type: ignore[arg-type]
        sink=sink,
        clock=lambda: NOW,
        backend="in_memory",
    )


def test_idle_only_stop_stops_an_idle_runner(
    overlay: ModelHostDrainOverlay,
    probe: FakeRunnerProbe,
    stopper: FakeRunnerStopper,
    sink: InMemoryEmissionSink,
) -> None:
    probe.states["example-runner-1"] = EnumRunnerActivity.IDLE
    result = _handler(overlay, probe, stopper, sink).handle(
        ModelIdleRunnerStopRequest(runner_name="example-runner-1")
    )
    assert result.outcome is EnumRunnerStopOutcome.STOPPED
    assert stopper.stopped == ["example-runner-1"]


def test_idle_only_stop_of_a_busy_runner_is_refused_and_nothing_is_stopped(
    overlay: ModelHostDrainOverlay,
    probe: FakeRunnerProbe,
    stopper: FakeRunnerStopper,
    sink: InMemoryEmissionSink,
) -> None:
    probe.states["example-runner-1"] = EnumRunnerActivity.BUSY
    result = _handler(overlay, probe, stopper, sink).handle(
        ModelIdleRunnerStopRequest(runner_name="example-runner-1")
    )
    assert result.outcome is EnumRunnerStopOutcome.REFUSED_BUSY
    assert result.failure_code == overlay.runner_stop.codes.runner_busy
    assert stopper.stopped == []


def test_idle_only_stop_fails_closed_when_the_runner_cannot_be_read(
    overlay: ModelHostDrainOverlay,
    probe: FakeRunnerProbe,
    stopper: FakeRunnerStopper,
    sink: InMemoryEmissionSink,
) -> None:
    probe.states["example-runner-1"] = EnumRunnerActivity.UNKNOWN
    result = _handler(overlay, probe, stopper, sink).handle(
        ModelIdleRunnerStopRequest(runner_name="example-runner-1")
    )
    assert result.outcome is EnumRunnerStopOutcome.REFUSED_UNREADABLE
    assert stopper.stopped == []


def test_idle_only_stop_of_a_runner_the_overlay_does_not_declare_is_refused(
    overlay: ModelHostDrainOverlay,
    probe: FakeRunnerProbe,
    stopper: FakeRunnerStopper,
    sink: InMemoryEmissionSink,
) -> None:
    result = _handler(overlay, probe, stopper, sink).handle(
        ModelIdleRunnerStopRequest(runner_name="not-a-runner")
    )
    assert result.outcome is EnumRunnerStopOutcome.FAILED
    assert result.failure_code == overlay.runner_stop.codes.unknown_runner
    assert stopper.stopped == []


def test_idle_only_injected_break_a_stop_that_leaves_the_runner_up_reads_failed(
    overlay: ModelHostDrainOverlay,
    probe: FakeRunnerProbe,
    sink: InMemoryEmissionSink,
) -> None:
    """The failing control: a stop command that exits 0 and leaves the runner running is caught by the readback."""
    assert overlay.runner_stop.injected_fault
    probe.states["example-runner-1"] = EnumRunnerActivity.IDLE
    result = _handler(overlay, probe, StuckRunnerStopper(), sink).handle(
        ModelIdleRunnerStopRequest(runner_name="example-runner-1")
    )
    assert result.outcome is EnumRunnerStopOutcome.FAILED
    assert result.failure_code == overlay.runner_stop.codes.still_running


def test_idle_only_proc_probe_reads_a_worker_process_as_busy(
    overlay: ModelHostDrainOverlay, tmp_path: Path
) -> None:
    settings = overlay.runner_stop
    proc = tmp_path / "proc"
    runner_dir = tmp_path / "runners" / "example-runner-1"
    runner_dir.mkdir(parents=True)
    probe = ProcRunnerProbe(settings, proc_root=proc)
    # No process at all: the listener is idle.
    proc.mkdir()
    assert probe.activity("example-runner-1") is EnumRunnerActivity.IDLE
    # A worker process whose working directory is the runner's directory: busy.
    worker = proc / "4242"
    worker.mkdir()
    (worker / "cmdline").write_bytes(
        f"{runner_dir}/bin/{settings.worker_process_name}\0spawnclient\0".encode()
    )
    (worker / "cwd").symlink_to(runner_dir)
    probe_busy = ProcRunnerProbe(
        settings, proc_root=proc, runner_dirs={"example-runner-1": str(runner_dir)}
    )
    assert probe_busy.activity("example-runner-1") is EnumRunnerActivity.BUSY
    # A proc root that cannot be read is unknown, never idle.
    unreadable = ProcRunnerProbe(settings, proc_root=tmp_path / "absent")
    assert unreadable.activity("example-runner-1") is EnumRunnerActivity.UNKNOWN
