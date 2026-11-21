# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_host_drain_effect: the drain state, the running lanes, a runner's activity and its stop."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_host_drain_command import ModelHostDrainCommand
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumHostReadKind,
    EnumRunnerActivity,
    ModelDrainMark,
)


class ProtocolDrainStore(Protocol):
    """Which hosts are draining, and until when.

    A drain mark is time-bounded: a draining record whose ``until`` is at or behind the store's clock reads as not
    draining. The in-memory store is the default; the real stores are durable.

    ``durable`` says whether a mark outlives the process that wrote it: a store whose marks die with the process can
    never back a SAFE reading. ``mark_of`` is the raw record the clock has not judged, so one status reads one mark
    and one clock and the line it prints cannot disagree with itself.
    """

    durable: bool

    def set_draining(
        self,
        host_name: str,
        draining: bool,
        *,
        reason: str,
        at: datetime,
        until: datetime | None = None,
    ) -> None: ...

    def is_draining(self, host_name: str) -> bool: ...

    def draining_hosts(self) -> tuple[str, ...]: ...

    def until_of(self, host_name: str) -> datetime | None:
        """The mark's until while the host reads draining, else None."""
        ...

    def mark_of(self, host_name: str) -> ModelDrainMark | None:
        """The host's record as held, whether or not it is past its until; None when the host has none."""
        ...


class ProtocolLaneSource(Protocol):
    """How many lanes and units run on a host now. ``running`` raises OSError or ValueError when the host cannot be read."""

    def running(self, host_name: str) -> int: ...


@runtime_checkable
class ProtocolLaneNames(Protocol):
    """The names of the lanes a host runs, so a status line names what keeps the host busy."""

    def names(self, host_name: str) -> tuple[str, ...]: ...


@runtime_checkable
class ProtocolHostHold(Protocol):
    """Why a host's own guard holds it (heat, memory, swap, slots): empty when it is not held."""

    def held_reason(self, host_name: str) -> str: ...


class ProtocolAdvertisementSource(Protocol):
    """The capacity advertisements every pool host publishes on the bus: the drain mark and the host's running units
    and lanes are read from them and from nowhere else. ``advertisements`` answers one listening window, oldest first,
    and raises OSError when the bus cannot be read."""

    def advertisements(self) -> tuple[ModelHostCapacityAdvertisement, ...]: ...

    def invalidate(self) -> None:
        """Forget what was read: the next call listens again (a write just changed what the hosts advertise)."""
        ...


class ProtocolDrainCommandPublisher(Protocol):
    """Sends a drain command to the bus, where the addressed host takes it."""

    def publish(self, command: ModelHostDrainCommand) -> None: ...


class ProtocolHostRead(Protocol):
    """One reading of the host itself: how many lanes, units or model servers are busy there now.

    ``read`` raises OSError, ValueError or subprocess.SubprocessError when the host cannot be read: the caller reads a
    raise as unreadable, which is never SAFE.
    """

    name: str
    kind: EnumHostReadKind

    def read(self, host_name: str) -> int: ...


class ProtocolRunnerProbe(Protocol):
    """Whether a runner holds a job. A runner that cannot be read is UNKNOWN, never IDLE."""

    def activity(self, runner_name: str) -> EnumRunnerActivity: ...

    def is_running(self, runner_name: str) -> bool | None:
        """Whether the runner's listener process is up; None when the process table cannot be read."""
        ...


class ProtocolRunnerStopper(Protocol):
    """Stops a runner's service. Returns True when the stop command exited 0."""

    def stop(self, runner_name: str) -> bool: ...


__all__ = [
    "ProtocolAdvertisementSource",
    "ProtocolDrainCommandPublisher",
    "ProtocolDrainStore",
    "ProtocolHostHold",
    "ProtocolHostRead",
    "ProtocolLaneNames",
    "ProtocolLaneSource",
    "ProtocolRunnerProbe",
    "ProtocolRunnerStopper",
]
