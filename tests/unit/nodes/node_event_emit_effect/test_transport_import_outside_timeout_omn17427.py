# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Kafka transport is imported before any publish timeout starts (OMN-17427).

Each relayed event is a fresh process, so the first publish of every process
used to pay ``import omnibase_infra.event_bus.event_bus_kafka`` (3.4 to 3.7 s
on a loaded dev host) inside the 5 s per-publish cap, and published=False
came back from import time, not broker time. ``KafkaEventPublisher`` now
resolves the transport classes at construction.
"""

from __future__ import annotations

import asyncio
import builtins
import sys
import time
from collections.abc import Coroutine, Mapping, Sequence
from typing import Any

import pytest

from omnimarket.nodes.node_event_emit_effect.handlers import (
    handler_event_emit_effect as handler_module,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    KafkaEventPublisher,
)

pytestmark = pytest.mark.unit

_KAFKA_MODULE = "omnibase_infra.event_bus.event_bus_kafka"
_MODELS_MODULE = "omnibase_infra.event_bus.models"


class _YieldingFakeBus:
    """Fake bus that yields to the loop, as a real broker client does."""

    def __init__(self) -> None:
        self.published: list[str] = []

    async def start(self) -> None:
        await asyncio.sleep(0)

    async def publish(
        self, *, topic: str, key: bytes | None, value: bytes, headers: Any
    ) -> None:
        await asyncio.sleep(0)
        self.published.append(topic)

    async def close(self) -> None:
        await asyncio.sleep(0)


def test_transport_module_imported_before_publish_timeout_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, _KAFKA_MODULE, raising=False)
    monkeypatch.delitem(sys.modules, _MODELS_MODULE, raising=False)

    publisher = KafkaEventPublisher("localhost:9092")

    seen: list[bool] = []

    def _record_run(
        self: object, coro: Coroutine[Any, Any, None], *, timeout: float
    ) -> None:
        seen.append(_KAFKA_MODULE in sys.modules)
        coro.close()

    monkeypatch.setattr(handler_module._KafkaPublishLoop, "run", _record_run)

    publisher.publish("onex.evt.test.v1", {"a": 1}, key=None, correlation_id=None)

    assert seen == [True]


def test_slow_transport_import_cannot_time_out_a_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def _slow_import(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> Any:
        if name == _MODELS_MODULE and "ModelEventHeaders" in (fromlist or ()):
            time.sleep(0.3)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _slow_import)

    bus = _YieldingFakeBus()
    publisher = KafkaEventPublisher("localhost:9092", bus_factory=lambda: bus)

    publisher.publish(
        "onex.evt.test.v1",
        {"a": 1},
        key=None,
        correlation_id=None,
        timeout_seconds=0.2,
    )

    assert bus.published == ["onex.evt.test.v1"]
