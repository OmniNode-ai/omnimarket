# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory default backends of the host bring-up run (OMN-21899).

A host whose targets are a set of satisfied names, a lease with a fence and an
owed set, a bus that keeps steps and terminals, and a clock that only moves when
slept. They are the default wiring and the test doubles; the real backends reach
the host over its transport, hold the fence file under ``flock`` and publish to
the broker.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from omnimarket.models.host_bringup import EnumBringupAction
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

# One sequence across the bus and the lease, so a test can order their journals.
_SEQ = itertools.count()


@dataclass
class InMemoryClock:
    now: int = 0

    def time(self) -> int:
        return self.now

    def sleep(self, seconds: int) -> None:
        self.now += seconds


@dataclass
class InMemoryBringupHost:
    """A host whose check reads ok exactly for the names in ``satisfied``.

    A person step lands by itself ``person_lands_after_s[name]`` seconds after its
    first read; every other action satisfies its target unless it is in
    ``fail_actions``. ``on_act`` runs a hook after a named action, inside the lock.
    """

    satisfied: set[str]
    clock: InMemoryClock
    fail_actions: set[str] = field(default_factory=set)
    person_lands_after_s: dict[str, int] = field(default_factory=dict)
    on_act: dict[str, Callable[[], object]] = field(default_factory=dict)
    acts: list[str] = field(default_factory=list)
    _first_read: dict[str, int] = field(default_factory=dict)

    def read(self, host: str, target: ModelBringupTarget) -> EnumObservedState:
        name = target.name
        first = self._first_read.setdefault(name, self.clock.now)
        delay = self.person_lands_after_s.get(name)
        if (
            target.action is EnumBringupAction.PERSON_STEP
            and delay is not None
            and self.clock.now - first >= delay
        ):
            self.satisfied.add(name)
        return (
            EnumObservedState.OK if name in self.satisfied else EnumObservedState.NOT_OK
        )

    def act(
        self, host: str, target: ModelBringupTarget, fence: int
    ) -> ModelActionResult:
        self.acts.append(target.name)
        if target.name in self.fail_actions:
            return ModelActionResult(ok=False, detail=f"{target.name} action failed")
        self.satisfied.add(target.name)
        hook = self.on_act.get(target.name)
        if hook is not None:
            hook()
        return ModelActionResult(ok=True)


@dataclass
class InMemoryBringupLease:
    """One host's lease. ``grant`` stands in for another launching machine."""

    clock: InMemoryClock
    holder: str | None = None
    fence: int = 0
    expires: int = 0
    owed: set[str] = field(default_factory=set)
    renewals: list[tuple[int, int]] = field(default_factory=list)
    journal: list[tuple[int, str]] = field(default_factory=list)

    def grant(self, host: str, run_id: str, until: int) -> ModelLeaseGrant:
        if self.holder is not None and self.holder != run_id:
            self.owed.add(self.holder)
        self.fence += 1
        self.holder = run_id
        self.expires = until
        return ModelLeaseGrant(fence=self.fence, owed=tuple(sorted(self.owed)))

    def acquire(self, host: str, run_id: str, until: int) -> ModelLeaseGrant | None:
        if self.holder is not None and self.expires > self.clock.now:
            return None
        return self.grant(host, run_id, until)

    def settle_owed(self, host: str, fence: int, run_id: str) -> None:
        if fence == self.fence:
            self.owed.discard(run_id)

    def fenced(
        self, host: str, fence: int, until: int, call: Callable[[], T]
    ) -> tuple[bool, T | None]:
        if self.holder is None or fence != self.fence:
            return False, None
        self.renewals.append((self.clock.now, until))
        self.expires = max(self.expires, until)
        return True, call()

    def release(self, host: str, fence: int) -> None:
        if fence == self.fence and self.holder is not None:
            self.holder = None
            self.expires = 0
            self.journal.append((next(_SEQ), "release"))


@dataclass
class InMemoryBringupBus:
    steps: list[ModelBringupStepEvent] = field(default_factory=list)
    terminals: list[ModelBringupTerminalEvent] = field(default_factory=list)
    journal: list[tuple[int, str]] = field(default_factory=list)

    def emit_step(self, event: ModelBringupStepEvent) -> None:
        self.steps.append(event)

    def emit_terminal_if_absent(self, event: ModelBringupTerminalEvent) -> bool:
        if any(t.run_id == event.run_id for t in self.terminals):
            return False
        self.terminals.append(event)
        self.journal.append((next(_SEQ), "terminal"))
        return True

    def terminal_for(self, run_id: str) -> ModelBringupTerminalEvent | None:
        return next((t for t in self.terminals if t.run_id == run_id), None)


__all__ = [
    "InMemoryBringupBus",
    "InMemoryBringupHost",
    "InMemoryBringupLease",
    "InMemoryClock",
]
