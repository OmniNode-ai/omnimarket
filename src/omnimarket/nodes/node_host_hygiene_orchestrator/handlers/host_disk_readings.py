# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host disk reading store for the host hygiene orchestrator."""

from __future__ import annotations

import threading
from collections import OrderedDict

from omnimarket.models.model_host_disk_reading import (
    ModelHostDiskRead,
    ModelHostDiskReadFailed,
)
from omnimarket.models.model_host_hygiene import ModelHostHygieneReading


class HostDiskReadingStore:
    """Thread-safe, bounded store of the newest disk reading per host."""

    def __init__(self, max_hosts: int) -> None:
        self._max_hosts = max_hosts
        self._lock = threading.Lock()
        # host -> (read, failed) where read is ModelHostDiskRead | None, failed is ModelHostDiskReadFailed | None
        self._entries: OrderedDict[
            str, tuple[ModelHostDiskRead | None, ModelHostDiskReadFailed | None]
        ] = OrderedDict()

    def record_read(self, read: ModelHostDiskRead) -> None:
        with self._lock:
            host = read.host_name
            existing_read, existing_failed = self._entries.get(host, (None, None))
            # Newest read_at wins; an older or equal read_at is ignored
            if existing_read is not None and read.read_at <= existing_read.read_at:
                return
            # A read newer than the failure supersedes it
            if existing_failed is not None and read.read_at > existing_failed.read_at:
                self._entries[host] = (read, None)
            else:
                self._entries[host] = (read, existing_failed)
            self._entries.move_to_end(host)
            self._evict_if_needed()

    def record_failed(self, failed: ModelHostDiskReadFailed) -> None:
        with self._lock:
            host = failed.host_name
            existing_read, existing_failed = self._entries.get(host, (None, None))
            # A failure newer than the newest read makes the reading unread
            if existing_read is not None and failed.read_at <= existing_read.read_at:
                return
            if (
                existing_failed is not None
                and failed.read_at <= existing_failed.read_at
            ):
                return
            self._entries[host] = (existing_read, failed)
            self._entries.move_to_end(host)
            self._evict_if_needed()

    def reading_for(
        self, host: str, pressure_volume: str
    ) -> ModelHostHygieneReading | None:
        with self._lock:
            entry = self._entries.get(host)
            if entry is None:
                return None
            read, failed = entry
            if failed is not None and (read is None or failed.read_at > read.read_at):
                return ModelHostHygieneReading(
                    read_at=failed.read_at,
                    used_fraction=None,
                    unread_reason=f"{failed.reason}: {failed.detail}",
                )
            if read is None:
                return None
            # Find the volume whose path equals pressure_volume
            volume = None
            for vol in read.volumes:
                if vol.path == pressure_volume:
                    volume = vol
                    break
            if volume is None or volume.total_bytes == 0:
                return ModelHostHygieneReading(
                    read_at=read.read_at,
                    used_fraction=None,
                    unread_reason="pressure volume not in the reading",
                )
            used_fraction = volume.used_bytes / volume.total_bytes
            if used_fraction > 1.0:
                return ModelHostHygieneReading(
                    read_at=read.read_at,
                    used_fraction=None,
                    unread_reason="used bytes exceed total bytes",
                )
            return ModelHostHygieneReading(
                read_at=read.read_at,
                used_fraction=used_fraction,
                unread_reason="",
            )

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self._max_hosts:
            self._entries.popitem(last=False)
