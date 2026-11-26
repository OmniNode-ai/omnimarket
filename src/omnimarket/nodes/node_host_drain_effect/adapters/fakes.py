# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory ports for tests and drills: the same protocols the real adapters satisfy, plus the named injected breaks."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_host_drain_command import ModelHostDrainCommand
from omnimarket.nodes.node_host_drain_effect.adapters.stores import BusDrainStore
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumHostReadKind,
    EnumRunnerActivity,
    ModelDrainMark,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    tools_for_drain_and_lanes,
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


class LoopbackBus:
    """A drain command publisher and an advertisement source over one dict: what a pool host's serve does on the bus, in memory.

    ``publish`` keeps the newest command per host (the serve's fold); ``advertisements`` answers a window of two advertisements
    per host that is not ``silent`` (so a reading of two exists), each carrying the host's mark while it reads draining at the
    clock, its ``lanes`` (``None``: the host did not read them, so no lane tool), its units and its capacity refusal. ``broken``
    makes the bus unreachable: every read raises OSError.
    """

    def __init__(
        self,
        hosts: Iterable[str],
        *,
        clock: Callable[[], datetime],
        lanes: dict[str, list[str] | int | None] | None = None,
        units: dict[str, int] | None = None,
        held: dict[str, str] | None = None,
        silent: frozenset[str] = frozenset(),
        advertisements_per_host: int = 2,
        broken: bool = False,
    ) -> None:
        self.hosts = tuple(hosts)
        self.clock = clock
        self.lanes = lanes if lanes is not None else {}
        self.units = units if units is not None else {}
        self.held = held if held is not None else {}
        self.beats = 0
        self.silent = silent
        self.per_host = advertisements_per_host
        self.broken = broken
        self.commands: dict[str, ModelHostDrainCommand] = {}
        self.published: list[ModelHostDrainCommand] = []

    def publish(self, command: ModelHostDrainCommand) -> None:
        if self.broken:
            raise OSError("the bus is unreachable")
        self.published.append(command)
        held = self.commands.get(command.host_name)
        if held is None or command.issued_at > held.issued_at:
            self.commands[command.host_name] = command

    def invalidate(self) -> None:
        return None

    def advertise(self) -> None:
        """One more beat: a no-op, the window always holds ``advertisements_per_host`` advertisements of the state now."""
        self.beats += 1

    def advertisements(self) -> tuple[ModelHostCapacityAdvertisement, ...]:
        if self.broken:
            raise OSError("the bus is unreachable")
        now = self.clock().astimezone(UTC)
        ads: list[ModelHostCapacityAdvertisement] = []
        for host in self.hosts:
            if host in self.silent:
                continue
            command = self.commands.get(host)
            until = (
                command.until
                if command is not None and command.reads_draining(now)
                else None
            )
            named = self.lanes.get(host, [])
            if isinstance(named, int):
                # A count stands for that many lanes, named lane-1, lane-2, ...
                named = [f"lane-{n}" for n in range(1, named + 1)]
            tools = tools_for_drain_and_lanes(until, named)
            for beat in range(self.per_host):
                ads.append(
                    ModelHostCapacityAdvertisement(
                        host_name=host,
                        cores=8,
                        load1=0.5,
                        mem_available_bytes=1 << 30,
                        tools=tools,
                        running_units=self.units.get(host, 0),
                        advertised_at=now
                        - timedelta(seconds=10 * (self.per_host - 1 - beat)),
                        cadence_seconds=10,
                        capacity_refusal=self.held.get(host, ""),
                    )
                )
        return tuple(ads)


def make_bus_store(
    bus: LoopbackBus, *, clock: Callable[[], datetime] | None = None
) -> BusDrainStore:
    """The durable store of the bus profile over a loopback bus."""
    return BusDrainStore(bus, bus, clock=clock or bus.clock)
