# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected dependencies of the host bring-up run (OMN-21899).

The in-memory defaults live in ``handlers/inmemory_bringup_backends.py``; a real
backend supplies a probe and an actuator that reach the host, a lease held in a
root-owned fence file on the host under ``flock``, and a bus sink.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from omnimarket.nodes.node_host_bringup_orchestrator.models.model_host_bringup_run import (
    ModelActionResult,
    ModelBringupStepEvent,
    ModelBringupTerminalEvent,
    ModelLeaseGrant,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan import (
    EnumObservedState,
    ModelBringupTarget,
)

T = TypeVar("T")


class ProtocolClock(Protocol):
    def time(self) -> int: ...

    def sleep(self, seconds: int) -> None: ...


class ProtocolTargetProbe(Protocol):
    """Read one target's read-only check on one host."""

    def read(self, host: str, target: ModelBringupTarget) -> EnumObservedState: ...


class ProtocolTargetActuator(Protocol):
    """Run one target's action on one host. Runs inside the fenced lock."""

    def act(
        self, host: str, target: ModelBringupTarget, fence: int
    ) -> ModelActionResult: ...


class ProtocolBringupLease(Protocol):
    """The per-host lease with a fence and the set of runs owed a terminal."""

    def acquire(self, host: str, run_id: str, until: int) -> ModelLeaseGrant | None:
        """Take the lease when free or expired; None when another holder is live.

        On takeover of an expired holder, that holder joins the owed set, and the
        grant carries the whole owed set.
        """
        ...

    def settle_owed(self, host: str, fence: int, run_id: str) -> None:
        """Remove one run id from the owed set once its terminal is on the bus."""
        ...

    def fenced(
        self, host: str, fence: int, until: int, call: Callable[[], T]
    ) -> tuple[bool, T | None]:
        """Under the host lock: compare the fence, renew to ``until``, run ``call``.

        Returns (False, None) with nothing run when the fence is stale.
        """
        ...

    def release(self, host: str, fence: int) -> None: ...


class ProtocolBringupBus(Protocol):
    def emit_step(self, event: ModelBringupStepEvent) -> None: ...

    def emit_terminal_if_absent(self, event: ModelBringupTerminalEvent) -> bool:
        """Append the terminal unless one exists for its run id; True when appended."""
        ...


__all__ = [
    "ProtocolBringupBus",
    "ProtocolBringupLease",
    "ProtocolClock",
    "ProtocolTargetActuator",
    "ProtocolTargetProbe",
]
