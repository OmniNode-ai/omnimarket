# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: the broker load handler on in-memory metrics and consumer, one break by name."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_broker_load_reading_effect.adapters.consumer_start import (
    InMemoryConsumerStart,
)
from omnimarket.nodes.node_broker_load_reading_effect.adapters.metrics_source import (
    InMemoryBrokerMetrics,
)
from omnimarket.nodes.node_broker_load_reading_effect.handlers.handler_broker_load import (
    HandlerBrokerLoad,
)
from omnimarket.nodes.node_broker_load_reading_effect.models.model_broker_load import (
    EnumConsumerStart,
    ModelBrokerMetrics,
    ModelConsumerStart,
)
from omnimarket.nodes.node_broker_load_reading_effect.overlay import (
    ModelBrokerLoadOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_broker_load_reading_effect"
)
EXAMPLE = NODE / "overlay.example.yaml"
FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures/broker_load/public_metrics_dev_2026-11-06.prom"
)
RUN_AT = datetime(2026, 11, 6, 22, 40, tzinfo=UTC)
HOST = "example-host"


def overlay() -> ModelBrokerLoadOverlay:
    return ModelBrokerLoadOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


#: A broker inside both bars of the example overlay (1000 partitions a shard, 5000 ms).
HEALTHY = ModelBrokerMetrics(topics=300, partitions=320, shards=1, empty_partitions=40)
STARTED_FAST = ModelConsumerStart(outcome=EnumConsumerStart.STARTED, elapsed_ms=800)


class World:
    """A broker and a probe consumer, both healthy until a test breaks one by name."""

    def __init__(self) -> None:
        self.metrics: ModelBrokerMetrics | None = HEALTHY
        self.consumer = STARTED_FAST

    def break_partitions_over_the_bar(self) -> None:
        """The dev broker's case: one shard holding far more partitions than a shard serves."""
        self.metrics = ModelBrokerMetrics(
            topics=2858, partitions=2894, shards=1, empty_partitions=2400
        )

    def break_consumer_start_slow(self) -> None:
        self.consumer = ModelConsumerStart(
            outcome=EnumConsumerStart.STARTED, elapsed_ms=21000
        )

    def break_consumer_never_starts(self) -> None:
        self.consumer = ModelConsumerStart(
            outcome=EnumConsumerStart.TIMEOUT, elapsed_ms=30000
        )

    def break_metrics_unreadable(self) -> None:
        self.metrics = None

    def break_consumer_transport_unreadable(self) -> None:
        self.consumer = ModelConsumerStart(
            outcome=EnumConsumerStart.UNREADABLE, detail="oserror"
        )

    def handler(self, ledger: InMemoryLedger | None) -> HandlerBrokerLoad:
        return HandlerBrokerLoad(
            overlay=overlay(),
            host=HOST,
            metrics=InMemoryBrokerMetrics(self.metrics),
            consumer=InMemoryConsumerStart(self.consumer),
            appender=ledger,
            clock=lambda: RUN_AT,
        )


__all__ = ["FIXTURE", "HOST", "RUN_AT", "World", "overlay"]
