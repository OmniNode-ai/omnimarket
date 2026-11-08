# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_host_drain_effect: the drain state, the running lanes, a runner's activity and its stop."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumRunnerActivity,
)


class ProtocolDrainStore(Protocol):
    """Which hosts are draining. The in-memory store is the default; the real stores are durable."""

    def set_draining(
        self, host_name: str, draining: bool, *, reason: str, at: datetime
    ) -> None: ...

    def is_draining(self, host_name: str) -> bool: ...

    def draining_hosts(self) -> tuple[str, ...]: ...


class ProtocolLaneSource(Protocol):
    """How many lanes run on a host now."""

    def running(self, host_name: str) -> int: ...


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
    "ProtocolDrainStore",
    "ProtocolLaneSource",
    "ProtocolRunnerProbe",
    "ProtocolRunnerStopper",
]
