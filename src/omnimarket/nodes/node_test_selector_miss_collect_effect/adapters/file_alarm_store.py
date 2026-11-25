# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The collector's durable alarm store: one JSON document in the serve tick's state directory.

The serve tick is a new process each pass, so the in-memory store would forget every alarm key and collector result
between two passes. This store keeps them in ``alarms.json`` under the state directory. Every operation takes an
exclusive file lock, reads the document, changes it and replaces the file atomically, so two overlapping passes append
one row per key and a crash leaves either the old document or the new one.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelNightlyAlarm,
)

DOCUMENT = "alarms.json"
LOCK = "alarms.lock"


class FileAlarmStore:
    """``ProtocolAlarmStore`` over one locked JSON document."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        with (self._dir / LOCK).open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                path = self._dir / DOCUMENT
                document: dict[str, Any] = (
                    json.loads(path.read_text(encoding="utf-8"))
                    if path.exists()
                    else {}
                )
                document.setdefault("alarms", {})
                document.setdefault("results", [])
                before = json.dumps(document, sort_keys=True)
                yield document
                if json.dumps(document, sort_keys=True) != before:
                    held = self._dir / (DOCUMENT + ".tmp")
                    held.write_text(
                        json.dumps(document, sort_keys=True), encoding="utf-8"
                    )
                    os.replace(held, path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def upsert_pending(self, alarm: ModelNightlyAlarm) -> bool:
        with self._locked() as document:
            if alarm.key in document["alarms"]:
                return False
            document["alarms"][alarm.key] = {
                "alarm": alarm.model_dump(mode="json"),
                "status": "pending",
            }
            return True

    def undelivered(self) -> tuple[ModelNightlyAlarm, ...]:
        with self._locked() as document:
            return tuple(
                ModelNightlyAlarm.model_validate(row["alarm"])
                for row in document["alarms"].values()
                if row["status"] == "pending"
            )

    def mark_delivered(self, key: str) -> None:
        with self._locked() as document:
            document["alarms"][key]["status"] = "delivered"

    def record_collector_result(self, repo: str, slot_date: date) -> None:
        entry = f"{repo}|{slot_date.isoformat()}"
        with self._locked() as document:
            if entry not in document["results"]:
                document["results"].append(entry)

    def has_collector_result(self, repo: str, slot_date: date) -> bool:
        with self._locked() as document:
            return f"{repo}|{slot_date.isoformat()}" in document["results"]


__all__: list[str] = ["FileAlarmStore"]
