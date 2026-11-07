# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory ports for tests and drills: the same protocols the real adapters satisfy, plus the named injected breaks."""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumRunnerActivity,
)


class FixedLaneSource:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        #: A drain never stops a lane; a test reads this to prove it.
        self.stopped: list[str] = []

    def running(self, host_name: str) -> int:
        return self.counts.get(host_name, 0)


class FakeRunnerProbe:
    def __init__(self, states: dict[str, EnumRunnerActivity]) -> None:
        self.states = states
        self.down: set[str] = set()

    def activity(self, runner_name: str) -> EnumRunnerActivity:
        return self.states.get(runner_name, EnumRunnerActivity.UNKNOWN)

    def is_running(self, runner_name: str) -> bool | None:
        return runner_name not in self.down


class FakeRunnerStopper:
    """A stop that works: the probe reads the runner down afterwards."""

    def __init__(self, probe: FakeRunnerProbe) -> None:
        self._probe = probe
        self.stopped: list[str] = []

    def stop(self, runner_name: str) -> bool:
        self.stopped.append(runner_name)
        self._probe.down.add(runner_name)
        return True


class StuckRunnerStopper:
    """The injected break of the runner stop: exits 0 and leaves the runner as it was."""

    def __init__(self) -> None:
        self.stopped: list[str] = []

    def stop(self, runner_name: str) -> bool:
        self.stopped.append(runner_name)
        return True


class DroppingDrainStore:
    """The injected break of the drain: accepts the write and keeps nothing."""

    def set_draining(
        self, host_name: str, draining: bool, *, reason: str, at: datetime
    ) -> None:
        return None

    def is_draining(self, host_name: str) -> bool:
        return False

    def draining_hosts(self) -> tuple[str, ...]:
        return ()
