# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Doubles shared by the host announcer tests (OMN-24367)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models import ModelEventHeaders, ModelPublishReceipt

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_host_introspection_announce_effect.protocols import (
    HostFactsReading,
    HostFactsUnreadableError,
)
from omnimarket.nodes.node_host_introspection_compute.models import (
    ModelHostRuntime,
    ModelHostSoftware,
)

AT = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)


class RecordingBus(EventBusInmemory):
    """An in-memory bus that keeps what was published and fails publishes to the topics in ``down``."""

    def __init__(self) -> None:
        super().__init__(environment="local", group="host-introspection-test")
        self.records: list[tuple[str, dict[str, object]]] = []
        self.down: set[str] = set()

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> ModelPublishReceipt:
        if topic in self.down:
            raise ConnectionError("broker not available")
        self.records.append((topic, json.loads(value)["payload"]))
        return await super().publish(topic, key, value, headers)

    def on(self, topic: str) -> list[dict[str, object]]:
        return [payload for seen, payload in self.records if seen == topic]


class FakeFactsReader:
    """A facts reader that returns a fixed reading, or raises while ``unreadable`` is set."""

    def __init__(self) -> None:
        self.unreadable = False
        self.docker_version = "27.3.1"

    def read(self, software: list[str]) -> HostFactsReading:
        if self.unreadable:
            raise HostFactsUnreadableError("nvidia-smi exited 9: driver not loaded")
        return HostFactsReading(
            cores=8,
            mem_total_bytes=16 * 1024**3,
            runtimes=(ModelHostRuntime(name="docker", version=self.docker_version),),
            software=(ModelHostSoftware(name="git", version="2.45.0"),),
        )


def capacity(host: str = "h201") -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=8,
        load1=1.0,
        mem_available_bytes=8 * 1024**3,
        tools=["git"],
        advertised_at=AT,
        cadence_seconds=10,
        mem_total_bytes=16 * 1024**3,
    )
