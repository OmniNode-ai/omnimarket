# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay-store projection publishes its snapshot keyed by overlay id, so topic compaction keeps every overlay."""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.nodes.node_projection_overlay_store.handlers import (
    handler_overlay_store_projection as handler_module,
)
from omnimarket.nodes.node_projection_overlay_store.handlers.handler_overlay_store_projection import (
    HandlerOverlayStoreProjectionRunner,
)
from tests.unit.nodes.node_overlay_store.support import LOADED_TOPIC, NOW, record

pytestmark = pytest.mark.unit


OVERLAY_IDS = (
    "lab_hosts",
    "node_pr_landing_snapshot_effect",
    "node_floor_delegation_effect",
    "delegation_routing",
)


class CompactingTopic:
    """A topic that keeps, per key, only the newest record; every None key is one key."""

    def __init__(self) -> None:
        self.records: list[tuple[bytes | None, bytes]] = []

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.records.append((key, value))

    def compact(self) -> list[tuple[bytes | None, bytes]]:
        kept: dict[object, tuple[bytes | None, bytes]] = {}
        for key, value in self.records:
            kept[key] = (key, value)
        return list(kept.values())


def _payload(overlay_id: str, when: datetime) -> dict[str, Any]:
    return (
        record(loaded_at=when)
        .model_copy(update={"overlay_id": overlay_id})
        .model_dump(mode="json")
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryOverlayStore:
    """The projection's database, replaced by the in-memory store the handler's store call is routed to."""
    held = InMemoryOverlayStore()
    monkeypatch.setattr(handler_module, "PostgresOverlayStore", lambda _pool: held)
    monkeypatch.setattr(
        HandlerOverlayStoreProjectionRunner,
        "db_for",
        lambda _self, _table, **_options: types.SimpleNamespace(pool=None),
    )
    return held


async def _drive(runner: HandlerOverlayStoreProjectionRunner) -> None:
    for i, overlay_id in enumerate(OVERLAY_IDS):
        await runner.project_event(
            LOADED_TOPIC, _payload(overlay_id, NOW + timedelta(minutes=i)), None
        )


@pytest.mark.asyncio
async def test_keyed_publish_survives_compaction(
    store: InMemoryOverlayStore,
) -> None:
    topic = CompactingTopic()
    runner = HandlerOverlayStoreProjectionRunner(publish_keyed_fn=topic.publish)
    await _drive(runner)
    compacted = topic.compact()
    keys = {key for key, _value in compacted}
    assert keys == {overlay_id.encode("utf-8") for overlay_id in OVERLAY_IDS}
    assert len(compacted) == len(OVERLAY_IDS)
    for key, value in compacted:
        assert key is not None
        assert json.loads(value.decode("utf-8"))["overlay_id"] == key.decode("utf-8")


@pytest.mark.asyncio
async def test_an_unkeyed_publish_loses_every_overlay_but_the_last() -> None:
    topic = CompactingTopic()
    for i, overlay_id in enumerate(OVERLAY_IDS):
        await topic.publish(
            "topic",
            None,
            json.dumps(_payload(overlay_id, NOW + timedelta(minutes=i))).encode(
                "utf-8"
            ),
        )
    compacted = topic.compact()
    assert len(compacted) == 1
    assert compacted[0][0] is None


@pytest.mark.asyncio
async def test_an_invalid_payload_raises_and_publishes_nothing(
    store: InMemoryOverlayStore,
) -> None:
    topic = CompactingTopic()
    runner = HandlerOverlayStoreProjectionRunner(publish_keyed_fn=topic.publish)
    with pytest.raises(ValidationError):
        await runner.project_event(
            LOADED_TOPIC, {"overlay_id": "x", "digest": "0" * 64}, None
        )
    assert topic.records == []


@pytest.mark.asyncio
async def test_no_publisher_still_stores_and_returns_true(
    store: InMemoryOverlayStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = HandlerOverlayStoreProjectionRunner()

    async def _no_producer() -> None:
        return None

    monkeypatch.setattr(runner, "_ensure_producer", _no_producer)
    assert await runner.get_keyed_publish_fn() is None
    assert (
        await runner.project_event(LOADED_TOPIC, _payload("lab_hosts", NOW), None)
        is True
    )
    assert await store.read_current("lab_hosts") is not None


@pytest.mark.asyncio
async def test_injected_keyed_fn_is_returned_and_unkeyed_fn_is_unchanged() -> None:
    async def keyed(topic: str, key: bytes | None, value: bytes) -> None:
        return None

    async def plain(topic: str, value: bytes) -> None:
        return None

    runner = HandlerOverlayStoreProjectionRunner(
        publish_fn=plain, publish_keyed_fn=keyed
    )
    assert await runner.get_keyed_publish_fn() is keyed
    assert await runner.get_publish_fn() is plain
