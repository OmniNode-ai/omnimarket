# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader's snapshot topic is read from the lane the projection runs in, its readings stay on the broker."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.overlay_store import (
    EnumOverlayReadState,
    ModelOverlayStoreReadOverlay,
)
from omnimarket.nodes.node_overlay_store_read_effect import (
    runtime_overlay_store_read as read,
)
from tests.unit.nodes.node_overlay_store.support import (
    LOADED_TOPIC,
    SNAPSHOT_TOPIC,
    FakeBus,
    contract_topics,
)

pytestmark = pytest.mark.unit


def _overlay(**over: Any) -> ModelOverlayStoreReadOverlay:
    base: dict[str, Any] = {
        "backend": "bus_snapshot",
        "bus_lane": "store-lane",
        "snapshot_root": "/tmp/unused",
        "read_window_s": 0.1,
        "host_name_env": "HOST",
        "overlays": {"lab_hosts": {"stale_after_s": 3600, "on_stale": "read"}},
    }
    return ModelOverlayStoreReadOverlay.model_validate({**base, **over})


def _use(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def opened() -> AsyncIterator[FakeBus]:
        yield bus

    def opener(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return opened()

    monkeypatch.setattr(read, "open_lane_bus", opener)
    return calls


def _open_both(store: Any, reporter: Any) -> None:
    """Open the snapshot store's bus, then the reporter's, so each opener records its call."""

    async def run() -> None:
        async with store._open_bus():
            pass
        async with reporter._open_bus():
            pass

    asyncio.run(run())


def test_golden_chain_the_snapshot_is_listened_for_on_the_lane_named_and_the_reading_is_reported_on_the_store_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    calls = _use(monkeypatch, bus)
    overlay = _overlay(
        kafka_bootstrap="store.example.invalid:1", snapshot_bus_lane="snapshot-lane"
    )
    store, reporter = read._bus_snapshot_store(overlay, "h1", None, {})
    _open_both(store, reporter)
    assert calls[0]["lane"] == "snapshot-lane"
    assert calls[0]["kafka_bootstrap"] is None
    assert calls[1]["lane"] is None
    assert calls[1]["kafka_bootstrap"] == "store.example.invalid:1"


def test_absent_key_reads_exactly_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus()
    calls = _use(monkeypatch, bus)
    overlay = _overlay(kafka_bootstrap="store.example.invalid:1")
    store, reporter = read._bus_snapshot_store(overlay, "h1", None, {})
    _open_both(store, reporter)
    assert calls[0]["lane"] is None
    assert calls[0]["kafka_bootstrap"] == "store.example.invalid:1"
    assert calls[1]["lane"] is None
    assert calls[1]["kafka_bootstrap"] == "store.example.invalid:1"


def test_the_command_line_bootstrap_still_wins_for_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    calls = _use(monkeypatch, bus)
    overlay = _overlay(snapshot_bus_lane="snapshot-lane")
    store, reporter = read._bus_snapshot_store(
        overlay, "h1", "cli.example.invalid:2", {}
    )
    _open_both(store, reporter)
    assert calls[0]["lane"] is None
    assert calls[0]["kafka_bootstrap"] == "cli.example.invalid:2"


def test_refused_a_lane_with_no_broker_backend() -> None:
    with pytest.raises(ValidationError, match="no bus to read a snapshot from"):
        _overlay(backend="in_memory", snapshot_bus_lane="x")
    with pytest.raises(
        ValidationError, match="String should have at least 1 character"
    ):
        _overlay(snapshot_bus_lane="")


def test_named_break_the_snapshot_opener_is_the_store_broker_when_the_lane_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    calls = _use(monkeypatch, bus)
    overlay = _overlay(snapshot_bus_lane="snapshot-lane")
    store, reporter = read._bus_snapshot_store(overlay, "h1", None, {})
    _open_both(store, reporter)
    assert calls[0]["lane"] == "snapshot-lane"


def test_seam_the_topics_of_loader_projection_and_reader_are_the_same_names() -> None:
    """Loader publishes what the projection subscribes to; the reader listens where the projection publishes."""
    reader_contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_overlay_store_read_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    assert reader_contract["snapshot_read"]["topic"] == SNAPSHOT_TOPIC
    assert contract_topics("node_projection_overlay_store", "subscribe_topics") == [
        LOADED_TOPIC
    ]
    assert contract_topics("node_overlay_store_load_effect", "publish_topics") == [
        LOADED_TOPIC
    ]


def test_error_chain_a_snapshot_lane_that_refuses_ends_in_a_reading_reported_on_the_store_broker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The lane the projection publishes on is down: the read says so, and the reading still reaches the store broker."""
    store_bus = FakeBus()

    @contextlib.asynccontextmanager
    async def store_broker() -> AsyncIterator[FakeBus]:
        yield store_bus

    class Refused:
        async def __aenter__(self) -> FakeBus:
            raise ConnectionError("snapshot lane refused")

        async def __aexit__(self, *_: Any) -> None:
            return None

    def opener(**kwargs: Any) -> Any:
        return Refused() if kwargs["lane"] == "snapshot-lane" else store_broker()

    monkeypatch.setattr(read, "open_lane_bus", opener)
    overlay = _overlay(
        kafka_bootstrap="store.example.invalid:1",
        snapshot_bus_lane="snapshot-lane",
        snapshot_root=str(tmp_path / "snapshot"),
    )
    handler = read.build_handler(overlay, {"HOST": "h1"})
    (reading,) = asyncio.run(handler.refresh_all())
    assert reading.state is EnumOverlayReadState.UNREADABLE
    assert reading.store_error is not None
    assert "snapshot lane refused" in reading.store_error
    ((topic, key, _value),) = store_bus.published
    assert topic == "onex.evt.omnimarket.overlay-store-read.v1"
    assert key == b"h1:lab_hosts"
