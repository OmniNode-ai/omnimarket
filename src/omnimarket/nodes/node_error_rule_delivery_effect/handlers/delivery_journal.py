# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Append and fsync before returning; acknowledgements are separate durable records."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from omnimarket.models.error_rule_delivery import ModelDeliveryJournalEntry


class DeliveryJournal:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "delivery-outbox.jsonl"

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "delivery.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self) -> list[ModelDeliveryJournalEntry]:
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        # A process killed DURING append may leave a torn tail. No complete,
        # previously flushed entry is discarded; malformed complete rows fail loud.
        if data and not data.endswith(b"\n"):
            end = data.rfind(b"\n") + 1
            with self.path.open("r+b") as stream:
                stream.truncate(end)
                stream.flush()
                os.fsync(stream.fileno())
            data = data[:end]
        return [
            ModelDeliveryJournalEntry.model_validate_json(row)
            for row in data.splitlines()
        ]

    def append(self, entry: ModelDeliveryJournalEntry) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(entry.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
