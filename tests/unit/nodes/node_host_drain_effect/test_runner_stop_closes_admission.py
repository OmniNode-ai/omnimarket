# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The idle-only stop closes admission on the runner's host before it reads the runner (OMN-22905 review finding).

Before, the stop read the runner and then stopped it, so a lane admitted between the read and the stop lost its job.
Now the host is set draining first, so no start is admitted while the stop reads and acts; a refusal or a failed stop
returns the host to the state it had.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import (
    FakeRunnerProbe,
    FakeRunnerStopper,
    StuckWorkerStopper,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import InMemoryDrainStore
from omnimarket.nodes.node_host_drain_effect.handlers.handler_idle_runner_stop import (
    HandlerIdleRunnerStop,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumRunnerActivity,
    EnumRunnerStopOutcome,
    ModelIdleRunnerStopRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostDrainOverlay
from omnimarket.nodes.node_host_drain_effect.protocols import (
    ProtocolDrainStore,
    ProtocolRunnerStopper,
)

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
RUNNER = "example-runner-1"


class _WatchingProbe(FakeRunnerProbe):
    """Records whether the runner's host read as draining at the moment the runner's activity was read."""

    def __init__(self, store: ProtocolDrainStore, host: str) -> None:
        super().__init__({})
        self._store = store
        self._host = host
        self.draining_when_read: list[bool] = []

    def activity(self, runner_name: str) -> EnumRunnerActivity:
        self.draining_when_read.append(self._store.is_draining(self._host))
        return super().activity(runner_name)


class _WriteFailsStore(InMemoryDrainStore):
    def set_draining(
        self, host_name: str, draining: bool, *, reason: str, at: datetime
    ) -> None:
        raise OSError("drain store down")


def _host(overlay: ModelHostDrainOverlay) -> str:
    row = overlay.runner(RUNNER)
    assert row is not None
    return row.host


def _handler(
    overlay: ModelHostDrainOverlay,
    probe: FakeRunnerProbe,
    stopper: ProtocolRunnerStopper,
    store: ProtocolDrainStore,
) -> HandlerIdleRunnerStop:
    return HandlerIdleRunnerStop(
        overlay.runner_stop,
        probe=probe,
        stopper=stopper,
        store=store,
        sink=InMemoryEmissionSink(),
        clock=lambda: NOW,
        backend="in_memory",
    )


def test_the_host_is_draining_when_the_runner_is_read_and_stays_so_after_the_stop(
    overlay: ModelHostDrainOverlay,
) -> None:
    store = InMemoryDrainStore()
    host = _host(overlay)
    probe = _WatchingProbe(store, host)
    probe.states[RUNNER] = EnumRunnerActivity.IDLE
    result = _handler(overlay, probe, FakeRunnerStopper(probe), store).handle(
        ModelIdleRunnerStopRequest(runner_name=RUNNER)
    )
    assert result.outcome is EnumRunnerStopOutcome.STOPPED
    assert probe.draining_when_read == [True]
    assert store.is_draining(host) is True


def test_a_busy_runner_returns_the_host_to_placement(
    overlay: ModelHostDrainOverlay,
) -> None:
    store = InMemoryDrainStore()
    host = _host(overlay)
    probe = _WatchingProbe(store, host)
    probe.states[RUNNER] = EnumRunnerActivity.BUSY
    result = _handler(overlay, probe, FakeRunnerStopper(probe), store).handle(
        ModelIdleRunnerStopRequest(runner_name=RUNNER)
    )
    assert result.outcome is EnumRunnerStopOutcome.REFUSED_BUSY
    assert store.is_draining(host) is False


def test_a_host_the_operator_drained_stays_drained_after_a_refused_stop(
    overlay: ModelHostDrainOverlay,
) -> None:
    store = InMemoryDrainStore()
    host = _host(overlay)
    store.set_draining(host, True, reason="operator", at=NOW)
    probe = _WatchingProbe(store, host)
    probe.states[RUNNER] = EnumRunnerActivity.BUSY
    _handler(overlay, probe, FakeRunnerStopper(probe), store).handle(
        ModelIdleRunnerStopRequest(runner_name=RUNNER)
    )
    assert store.is_draining(host) is True


def test_a_failed_stop_returns_the_host_to_placement(
    overlay: ModelHostDrainOverlay,
) -> None:
    store = InMemoryDrainStore()
    host = _host(overlay)
    probe = _WatchingProbe(store, host)
    probe.states[RUNNER] = EnumRunnerActivity.IDLE
    result = _handler(overlay, probe, StuckWorkerStopper(), store).handle(
        ModelIdleRunnerStopRequest(runner_name=RUNNER)
    )
    assert result.failure_code == overlay.runner_stop.codes.still_running
    assert store.is_draining(host) is False


def test_when_admission_cannot_be_closed_nothing_is_read_or_stopped(
    overlay: ModelHostDrainOverlay,
) -> None:
    store = _WriteFailsStore()
    probe = _WatchingProbe(store, _host(overlay))
    probe.states[RUNNER] = EnumRunnerActivity.IDLE
    stopper = FakeRunnerStopper(probe)
    result = _handler(overlay, probe, stopper, store).handle(
        ModelIdleRunnerStopRequest(runner_name=RUNNER)
    )
    assert result.outcome is EnumRunnerStopOutcome.REFUSED_UNREADABLE
    assert result.failure_code == overlay.runner_stop.codes.admission_not_closed
    assert probe.draining_when_read == []
    assert stopper.stopped == []
