# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A harness: one orchestrator on scripted seams and a real conditional store, with every double kept for counting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_lines_compute.handlers import (
    HandlerFloorFillLines,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    ModelFloorFillConfig,
    load_floor_fill_config,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.core import (
    FloorFillOrchestrator,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelStateRead,
    ProtocolFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedBriefs,
    ScriptedClock,
    ScriptedFacts,
    ScriptedLedger,
    ScriptedPool,
    ScriptedStarter,
    ScriptedTickets,
    floor_record,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_reducer.handlers import HandlerFloorFillReducer

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
LINES4 = {
    "alpha": ["a1", "a2"],
    "bravo": ["b1", "b2"],
    "charlie": ["c1"],
    "delta": ["d1", "d2"],
}

OVERLAY: dict[str, Any] = {
    "scope": "floor-fill-test",
    "host_id": "fill-host",
    "url": "scripted://floor",
    "plans_root": "scripted://plans",
    "runner_command": [
        "node-entry",
        "--lane",
        "{lane}",
        "--key",
        "{key}",
        "--ticket",
        "{ticket}",
        "--attempt",
        "{attempt}",
    ],
    "reserved_timeout_seconds": 600,
    "sent_window_minutes": 120,
    "command_timeout_seconds": 60,
    "tick_interval_seconds": 300,
    "remote_argv": ["remote-shell", "{argv}"],
    "process_environment": {"EXAMPLE": "1"},
    "process_working_directory": "/scripted/work",
    "max_age_minutes": 15,
    "max_fill_lanes_per_line": 2,
    "stopped_max_lanes": 2,
    "stopped_report_chars": 600,
    "stopped_continue_rule": "Continue from the predecessor's head; do not start over.",
    "stopped_runner_command": [
        "node-entry",
        "--lane",
        "{lane}",
        "--key",
        "{key}",
        "--ticket",
        "{ticket}",
        "--attempt",
        "{attempt}",
        "--brief",
        "{brief}",
    ],
    "stopped_reports_root": "/scripted/reports",
    "stopped_brief_directory": "/scripted/briefs",
}


def overlay(**changes: Any) -> dict[str, Any]:
    return {**OVERLAY, **changes}


@dataclass
class Harness:
    config: ModelFloorFillConfig
    store: ProtocolFloorStateStore
    facts: ScriptedFacts
    pool: ScriptedPool
    ledger: ScriptedLedger
    tickets: ScriptedTickets
    starter: ScriptedStarter
    clock: ScriptedClock
    briefs: ScriptedBriefs
    extra: dict[str, Any] = field(default_factory=dict)

    def orchestrator(self) -> FloorFillOrchestrator:
        return FloorFillOrchestrator(
            config=self.config,
            store=self.store,
            facts=self.facts,
            pool=self.pool,
            ledger=self.ledger,
            tickets=self.tickets,
            runner=self.starter,
            briefs=self.briefs,
            clock=self.clock,
            candidates=HandlerFloorFillCandidates(),
            reducer=HandlerFloorFillReducer(),
            lines=HandlerFloorFillLines(),
        )


def harness(
    *,
    lines: dict[str, list[str]] | None = None,
    free: int = 20,
    store: ProtocolFloorStateStore | None = None,
    config: dict[str, Any] | None = None,
    **scripted: Any,
) -> Harness:
    clock = ScriptedClock(T0)
    floor = floor_record(lines if lines is not None else LINES4, generated_at=T0)
    return Harness(
        config=load_floor_fill_config(config or OVERLAY),
        store=store or InMemoryFloorStateStore(),
        facts=scripted.pop("facts", None) or ScriptedFacts(floor),
        pool=scripted.pop("pool", None) or ScriptedPool(free),
        ledger=scripted.pop("ledger", None) or ScriptedLedger(),
        tickets=scripted.pop("tickets", None) or ScriptedTickets(),
        starter=scripted.pop("starter", None) or ScriptedStarter(),
        clock=clock,
        briefs=scripted.pop("briefs", None) or ScriptedBriefs(),
    )


class SimulatedCrash(BaseException):
    """The process is killed here: not an Exception, so nothing in the pass can catch it."""


class CrashAfterWrites:
    """A store that lets ``writes`` conditional writes land and then kills the process."""

    def __init__(self, inner: ProtocolFloorStateStore, writes: int) -> None:
        self.inner = inner
        self.remaining = writes
        self.landed = 0

    def load(self, key: str) -> ModelStateRead:
        return self.inner.load(key)

    def compare_and_set(
        self, key: str, expected_version: int, new_version: int, payload: str
    ) -> bool:
        landed = self.inner.compare_and_set(key, expected_version, new_version, payload)
        if landed and not key.endswith(":answer"):
            self.landed += 1
            self.remaining -= 1
            if self.remaining == 0:
                raise SimulatedCrash
        return landed


class CountingStore:
    """Counts the conditional writes that landed, per row."""

    def __init__(self, inner: ProtocolFloorStateStore) -> None:
        self.inner = inner
        self.writes: list[str] = []

    def load(self, key: str) -> ModelStateRead:
        return self.inner.load(key)

    def compare_and_set(
        self, key: str, expected_version: int, new_version: int, payload: str
    ) -> bool:
        landed = self.inner.compare_and_set(key, expected_version, new_version, payload)
        if landed:
            self.writes.append(key)
        return landed


class SynchronizedSnapshot:
    """Makes ``parties`` instances read the state row before any of them goes on: one snapshot for all."""

    def __init__(
        self, inner: ProtocolFloorStateStore, scope: str, parties: int
    ) -> None:
        import threading

        self.inner = inner
        self.scope = scope
        self.barrier = threading.Barrier(parties, timeout=10)

    def load(self, key: str) -> ModelStateRead:
        read = self.inner.load(key)
        if key == self.scope:
            self.barrier.wait()
        return read

    def compare_and_set(
        self, key: str, expected_version: int, new_version: int, payload: str
    ) -> bool:
        return self.inner.compare_and_set(key, expected_version, new_version, payload)
