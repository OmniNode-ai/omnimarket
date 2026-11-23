# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory ports of the record publication: the keyed record store and the gateway's idempotent check-run write."""

from __future__ import annotations

import threading
from datetime import date
from typing import Literal

from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_attribution_record import (
    ModelPublishedAttributionRecord,
    ModelRecordCheckRun,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.record_ports import (
    GatewayWriteError,
)


class InMemoryRecordStore:
    """Atomic by key under a lock; ``rows`` is the record table, one row per (repository, slot date)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rows: dict[str, tuple[ModelPublishedAttributionRecord, str]] = {}

    def upsert_pending(self, record: ModelPublishedAttributionRecord) -> bool:
        with self._lock:
            if record.key in self.rows:
                return False
            self.rows[record.key] = (record, "pending")
            return True

    def unpublished(self) -> tuple[ModelPublishedAttributionRecord, ...]:
        with self._lock:
            return tuple(r for r, status in self.rows.values() if status == "pending")

    def mark_published(self, key: str) -> None:
        with self._lock:
            record, _ = self.rows[key]
            self.rows[key] = (record, "published")

    def is_published(self, repository: str, slot_date: date) -> bool:
        key = f"{repository}|{slot_date.isoformat()}"
        with self._lock:
            return key in self.rows and self.rows[key][1] == "published"

    def get(
        self, repository: str, slot_date: date
    ) -> ModelPublishedAttributionRecord | None:
        key = f"{repository}|{slot_date.isoformat()}"
        with self._lock:
            row = self.rows.get(key)
            return row[0] if row else None


class InMemoryRecordGateway:
    """Idempotent by ``(repo, head_sha, name)``: ``check_runs`` holds one entry per key, ``writes`` counts real writes."""

    def __init__(self, *, fail_writes: int = 0) -> None:
        self._lock = threading.Lock()
        self.check_runs: list[tuple[str, ModelRecordCheckRun]] = []
        self.writes = 0
        self._fail_writes = fail_writes

    def ensure_check_run(
        self, repo: str, check_run: ModelRecordCheckRun
    ) -> Literal["created", "exists"]:
        with self._lock:
            for existing_repo, existing in self.check_runs:
                if (existing_repo, existing.head_sha, existing.name) == (
                    repo,
                    check_run.head_sha,
                    check_run.name,
                ):
                    return "exists"
            if self._fail_writes > 0:
                self._fail_writes -= 1
                raise GatewayWriteError("the gateway write failed")
            self.check_runs.append((repo, check_run))
            self.writes += 1
            return "created"


__all__: list[str] = ["InMemoryRecordGateway", "InMemoryRecordStore"]
