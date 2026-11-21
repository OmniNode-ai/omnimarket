# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fakes of the host-down reading ports: a dict prober, a dict advertisement source and an in-memory publisher."""

from __future__ import annotations

from datetime import datetime


class FakeHostProber:
    """A host prober that answers from a dict: host -> True, False or None."""

    def __init__(self, results: dict[str, bool | None]) -> None:
        self._results = dict(results)

    def probe(
        self, host_name: str, address: str, port: int, timeout_s: float
    ) -> bool | None:
        return self._results.get(host_name)


class FakeAdvertisementSource:
    """An advertisement source that answers from a dict: host -> datetime."""

    def __init__(self, newest: dict[str, datetime]) -> None:
        self._newest = dict(newest)

    def newest(self, host_name: str) -> datetime | None:
        return self._newest.get(host_name)


class InMemoryReadingPublisher:
    """Keeps every published reading in memory: (topic, key, payload)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bytes]] = []

    def publish(self, topic: str, key: str, payload: bytes) -> None:
        self.published.append((topic, key, payload))
