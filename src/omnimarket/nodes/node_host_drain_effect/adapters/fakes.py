# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory ports for tests and drills: the same protocols the real adapters satisfy, plus the named injected breaks."""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumHostReadKind,
    EnumRunnerActivity,
    ModelDrainMark,
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


class StuckWorkerStopper:
    """The injected break of the runner stop: exits 0 and leaves the runner as it was."""

    def __init__(self) -> None:
        self.stopped: list[str] = []

    def stop(self, runner_name: str) -> bool:
        self.stopped.append(runner_name)
        return True


class DroppingDrainStore:
    """The injected break of the drain: accepts the write and keeps nothing."""

    durable = False

    def set_draining(
        self,
        host_name: str,
        draining: bool,
        *,
        reason: str,
        at: datetime,
        until: datetime | None = None,
    ) -> None:
        return None

    def is_draining(self, host_name: str) -> bool:
        return False

    def draining_hosts(self) -> tuple[str, ...]:
        return ()

    def until_of(self, host_name: str) -> datetime | None:
        return None

    def mark_of(self, host_name: str) -> ModelDrainMark | None:
        return None


class FixedHostRead:
    """A host read that answers fixed counts per host, or raises when the host is named in ``unreadable``."""

    def __init__(
        self,
        name: str,
        kind: EnumHostReadKind,
        counts: dict[str, int],
        *,
        unreadable: frozenset[str] = frozenset(),
    ) -> None:
        self.name = name
        self.kind = kind
        self.counts = counts
        self.unreadable = unreadable

    def read(self, host_name: str) -> int:
        if host_name in self.unreadable:
            raise OSError(f"{self.name}: the host {host_name} cannot be read")
        return self.counts.get(host_name, 0)
