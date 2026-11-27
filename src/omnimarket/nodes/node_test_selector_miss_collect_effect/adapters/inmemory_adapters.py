# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory ports: the fixture gateway, the keyed alarm store and the idempotent receiver."""

from __future__ import annotations

import threading
from datetime import date
from typing import Literal

from omnimarket.models.test_selector_reading import ModelAttributionCandidate
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelNightlyAlarm,
    ModelNightlyRun,
    ModelNightReading,
    ModelPreviousNight,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.ports import (
    GatewayReadError,
)


class InMemoryNightlyGateway:
    def __init__(
        self,
        *,
        run: ModelNightlyRun | None,
        reading: ModelNightReading | None,
        previous: ModelPreviousNight | None,
        candidates: tuple[ModelAttributionCandidate, ...] = (),
        fail_reads: bool = False,
    ) -> None:
        self._run, self._reading = run, reading
        self._previous, self._candidates = previous, candidates
        self._fail = fail_reads

    def nightly_run(self, repo: str, slot_date: date) -> ModelNightlyRun | None:
        return self._run

    def read_night(self, repo: str, run: ModelNightlyRun) -> ModelNightReading:
        if self._fail or self._reading is None:
            raise GatewayReadError("the night's artifacts are unreadable")
        return self._reading

    def previous_night(self, repo: str, slot_date: date) -> ModelPreviousNight | None:
        return self._previous

    def candidates(
        self, repo: str, previous_head: str, head: str, retention_days: int
    ) -> tuple[ModelAttributionCandidate, ...]:
        return self._candidates


class InMemoryAlarmStore:
    """Atomic by key under a lock; ``rows`` is the alarm table, one row per key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rows: dict[str, tuple[ModelNightlyAlarm, str]] = {}
        self.results: set[tuple[str, date]] = set()

    def upsert_pending(self, alarm: ModelNightlyAlarm) -> bool:
        with self._lock:
            if alarm.key in self.rows:
                return False
            self.rows[alarm.key] = (alarm, "pending")
            return True

    def undelivered(self) -> tuple[ModelNightlyAlarm, ...]:
        with self._lock:
            return tuple(a for a, status in self.rows.values() if status == "pending")

    def mark_delivered(self, key: str) -> None:
        with self._lock:
            alarm, _ = self.rows[key]
            self.rows[key] = (alarm, "delivered")

    def record_collector_result(self, repo: str, slot_date: date) -> None:
        with self._lock:
            self.results.add((repo, slot_date))

    def has_collector_result(self, repo: str, slot_date: date) -> bool:
        with self._lock:
            return (repo, slot_date) in self.results


class InMemoryAlarmReceiver:
    """Idempotent by the alarm key: ``received`` holds one entry per key, a repeat answers ``duplicate``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.received: dict[str, ModelNightlyAlarm] = {}
        self.calls = 0

    def accept(self, alarm: ModelNightlyAlarm) -> Literal["accepted", "duplicate"]:
        with self._lock:
            self.calls += 1
            if alarm.key in self.received:
                return "duplicate"
            self.received[alarm.key] = alarm
            return "accepted"


__all__: list[str] = [
    "InMemoryAlarmReceiver",
    "InMemoryAlarmStore",
    "InMemoryNightlyGateway",
]
