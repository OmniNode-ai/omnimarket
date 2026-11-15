# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host capacity advertisement store for the host hygiene orchestrator (OMN-24557).

Thread-safe and bounded: the newest ``advertised_at`` wins per host and the oldest
host is evicted when the bound is exceeded. ``held_reason_for`` reports, in order,
no advertisement, a stale advertisement, or a capacity refusal (the host's own guard
refuses new work: heat, memory, swap, slots). A drain mark does not hold a host here:
a hygiene cleanup frees the host's disk and adds no lane.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections import OrderedDict

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)


class HostCapacityStore:
    """Thread-safe, bounded store of the newest capacity advertisement per host."""

    def __init__(self, max_hosts: int) -> None:
        self._max_hosts = max_hosts
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, ModelHostCapacityAdvertisement] = OrderedDict()

    def record(self, ad: ModelHostCapacityAdvertisement) -> None:
        with self._lock:
            host = ad.host_name
            existing = self._entries.get(host)
            if existing is not None and ad.advertised_at <= existing.advertised_at:
                return
            self._entries[host] = ad
            self._entries.move_to_end(host)
            self._evict_if_needed()

    def held_reason_for(self, host: str, now: dt.datetime, max_age_seconds: int) -> str:
        with self._lock:
            ad = self._entries.get(host)
            if ad is None:
                return f"no capacity advertisement seen for {host}"
            age = int((now - ad.advertised_at).total_seconds())
            if age > max_age_seconds:
                return (
                    f"capacity advertisement is {age}s old (bound {max_age_seconds}s)"
                )
            if ad.capacity_refusal:
                return f"capacity refusal: {ad.capacity_refusal}"
            return ""

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self._max_hosts:
            self._entries.popitem(last=False)


__all__: list[str] = ["HostCapacityStore"]
