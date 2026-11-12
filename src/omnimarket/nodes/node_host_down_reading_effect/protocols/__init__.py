# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports of node_host_down_reading_effect: the down store, the host prober, the advertisement source and the reading publisher."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_host_down_reading_effect.models.model_host_down_reading import (
    ModelHostDownRecord,
)


class ProtocolDownStore(Protocol):
    """The store of down readings: one record per host, newest wins."""

    def write(self, record: ModelHostDownRecord) -> None: ...

    def latest(self, host_name: str) -> ModelHostDownRecord | None: ...

    def down_hosts(self, as_of: datetime, max_age_s: int) -> tuple[str, ...]: ...


class ProtocolHostProber(Protocol):
    """Probes one host: True answered, False did not, None could not run."""

    def probe(
        self, host_name: str, address: str, port: int, timeout_s: float
    ) -> bool | None: ...


class ProtocolAdvertisementSource(Protocol):
    """The newest capacity advertisement of a host, or None when there is none."""

    def newest(self, host_name: str) -> datetime | None: ...


class ProtocolReadingPublisher(Protocol):
    """Publishes one reading on its topic, keyed by the host."""

    def publish(self, topic: str, key: str, payload: bytes) -> None: ...


__all__ = [
    "ProtocolAdvertisementSource",
    "ProtocolDownStore",
    "ProtocolHostProber",
    "ProtocolReadingPublisher",
]
