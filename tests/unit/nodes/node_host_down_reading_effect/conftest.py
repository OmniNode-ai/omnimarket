# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of node_host_down_reading_effect: synthetic hosts, a fixed clock, no deployment fact."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.nodes.node_host_down_reading_effect.adapters.fakes import (
    FakeAdvertisementSource,
    FakeHostProber,
    InMemoryReadingPublisher,
)
from omnimarket.nodes.node_host_down_reading_effect.adapters.stores import (
    InMemoryDownStore,
)
from omnimarket.nodes.node_host_down_reading_effect.handlers.handler_host_down_reading import (
    HandlerHostDownReading,
)
from omnimarket.nodes.node_host_down_reading_effect.overlay import (
    ModelHostDownOverlay,
    load_example_overlay,
)
from omnimarket.nodes.node_host_down_reading_effect.runtime_host_down import (
    reading_topic,
)

NOW = datetime(2026, 11, 12, 16, 0, tzinfo=UTC)
OBSERVER = "lab-host-a"
HOST_A = "example-host-a"
HOST_B = "example-host-b"


def example_table() -> ModelLabHostsTable:
    """Two available rows and one row the table marks unavailable (never probed)."""
    return ModelLabHostsTable.model_validate(
        {
            "hosts": [
                {"name": HOST_A, "target": "ops@a.example", "available": True},
                {"name": HOST_B, "target": "b.example", "available": True},
                {
                    "name": "example-host-c",
                    "target": "c.example",
                    "available": False,
                    "unavailable_reason": "out for repair",
                },
            ]
        }
    )


class Rig:
    """One handler with every port exposed, and a clock a test moves."""

    def __init__(
        self,
        *,
        results: dict[str, bool | None],
        overlay: ModelHostDownOverlay | None = None,
        store: InMemoryDownStore | None = None,
        publisher: InMemoryReadingPublisher | None = None,
        table_source=example_table,
        observer: str = OBSERVER,
        adverts: dict[str, datetime] | None = None,
    ) -> None:
        self.overlay = overlay or load_example_overlay()
        self.now = NOW
        self.sink = InMemoryEmissionSink()
        self.store = store or InMemoryDownStore()
        self.publisher = publisher or InMemoryReadingPublisher()
        self.prober = FakeHostProber(results)
        self.probed: list[str] = []
        real_probe = self.prober.probe

        def spy(host_name: str, address: str, port: int, timeout_s: float):
            self.probed.append(f"{host_name}@{address}:{port}")
            return real_probe(host_name, address, port, timeout_s)

        self.prober.probe = spy  # type: ignore[method-assign]
        self.handler = HandlerHostDownReading(
            self.overlay,
            table_source,
            self.prober,
            FakeAdvertisementSource(adverts or {}),
            self.store,
            self.publisher,
            reading_topic(),
            self.sink,
            lambda: self.now,
            "in_memory",
            observer,
        )

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def overlay() -> ModelHostDownOverlay:
    return load_example_overlay()
