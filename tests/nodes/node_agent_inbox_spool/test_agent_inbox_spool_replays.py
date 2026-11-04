# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Spool behaviour on the in-memory bus, with a falsifier for minted groups."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_agent_inbox_spool import HandlerAgentInboxSpool
from omnimarket.nodes.node_agent_inbox_spool.models import (
    ModelAgentInboxSpoolOverlay,
    ModelAgentInboxSpoolRequest,
)
from omnimarket.nodes.node_agent_inbox_spool.node import load_topics

from .helpers import RecordingBus, msg_row, overlay, record

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 1, 22, tzinfo=UTC)


class MintingHandler(HandlerAgentInboxSpool):
    def _group_id(
        self, request: ModelAgentInboxSpoolRequest, overlay: ModelAgentInboxSpoolOverlay
    ) -> str:
        return f"{super()._group_id(request, overlay)}-{uuid4().hex[:12]}"


def request(
    config: ModelAgentInboxSpoolOverlay, *lanes: str
) -> ModelAgentInboxSpoolRequest:
    return ModelAgentInboxSpoolRequest(
        host="host-a", lanes=lanes or ("lane-a",), overlay=config
    )


async def seeded_bus() -> tuple[EventBusInmemory, RecordingBus]:
    bus = EventBusInmemory(environment="test", group="spool-test")
    await bus.start()
    topic = load_topics()[0]
    rows = (
        msg_row("lane-a", "nonce-direct"),
        msg_row("all", "nonce-all"),
        msg_row("lane-z", "nonce-other"),
        msg_row("lane-a,lane-b", "nonce-list"),
    )
    for row in rows:
        await bus.publish(topic, None, record(row))
    await bus.publish(
        topic, None, record(msg_row("lane-a", "nonce-flat"), wrapped=False)
    )
    await bus.publish(
        topic, None, record(msg_row("lane-a", "nonce-bad"), row_id="0" * 64)
    )
    await bus.publish(topic, None, b"not json")
    await bus.publish(
        topic, None, record("2026-11-01T22:00:00Z | STATUS | lane=x | to=lane-a | hi")
    )
    return bus, RecordingBus(bus)


def entries(spool: Path, lane: str) -> list[dict[str, object]]:
    found = [
        json.loads(p.read_text())
        for p in sorted((spool / lane / "msgs").glob("*.json"))
    ]
    return sorted(found, key=lambda e: str(e["raw_row"]))


async def test_agent_inbox_spool_writes_entries_and_unread_marker(
    tmp_path: Path,
) -> None:
    bus, spy = await seeded_bus()
    try:
        config = overlay(tmp_path)
        handler = HandlerAgentInboxSpool(bus=spy, clock=lambda: NOW)
        result = await handler.handle(request(config, "lane-a", "lane-b"))
    finally:
        await bus.close()
    assert result.read_ok, result.error
    assert result.events_read == 8
    assert result.malformed == 3
    assert result.unaddressed == 1
    assert result.consumer_group == "test-agent-inbox-host-a"
    texts = {str(e["text"]) for e in entries(tmp_path, "lane-a")}
    assert texts == {
        f"text with a | pipe and nonce {n}"
        for n in ("nonce-direct", "nonce-all", "nonce-list", "nonce-flat")
    }
    assert len(entries(tmp_path, "lane-b")) == 2  # the list row and the broadcast
    assert not (tmp_path / "lane-z").exists()
    assert result.unread_lanes == ("lane-a", "lane-b")
    marker = (tmp_path / "lane-a" / "unread").read_text().strip()
    assert (tmp_path / "lane-a" / "msgs" / f"{marker}.json").exists()
    first = entries(tmp_path, "lane-a")[0]
    assert first["sender"] == "orch"
    assert first["ticket"] == "OMN-1"
    assert first["recipients"]


async def test_agent_inbox_spool_empty_topic_writes_nothing(tmp_path: Path) -> None:
    bus = EventBusInmemory(environment="test", group="spool-test")
    await bus.start()
    try:
        handler = HandlerAgentInboxSpool(bus=RecordingBus(bus), clock=lambda: NOW)
        result = await handler.handle(request(overlay(tmp_path)))
    finally:
        await bus.close()
    assert result.read_ok, result.error
    assert result.events_read == 0
    assert result.spooled == ()
    assert result.unread_lanes == ()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("handler_type", [HandlerAgentInboxSpool, MintingHandler])
async def test_agent_inbox_spool_replays_groups_per_call(
    tmp_path: Path, handler_type: type[HandlerAgentInboxSpool]
) -> None:
    bus, spy = await seeded_bus()
    try:
        handler = handler_type(bus=spy, clock=lambda: NOW)
        config = overlay(tmp_path)
        results = [await handler.handle(request(config)) for _ in range(20)]
    finally:
        await bus.close()
    groups = {group for _, group, _ in spy.subscriptions}
    assert spy.ungrouped == []
    assert all(r.read_ok for r in results)
    if handler_type is MintingHandler:
        assert len(groups) == 20  # the falsifier: a minted group per call is visible
    else:
        assert groups == {"test-agent-inbox-host-a"}
        assert len(entries(tmp_path, "lane-a")) == 4
        assert results[0].duplicates == 0
        assert all(r.spooled == () and r.duplicates == 4 for r in results[1:])


async def test_agent_inbox_spool_delivered_entry_does_not_raise_marker(
    tmp_path: Path,
) -> None:
    bus, spy = await seeded_bus()
    try:
        handler = HandlerAgentInboxSpool(bus=spy, clock=lambda: NOW)
        config = overlay(tmp_path)
        await handler.handle(request(config))
        # The hook shim moves what it injected to delivered/ and clears the marker.
        lane = tmp_path / "lane-a"
        (lane / "delivered").mkdir()
        for path in (lane / "msgs").glob("*.json"):
            path.rename(lane / "delivered" / path.name)
        (lane / "unread").unlink()
        again = await handler.handle(request(config))
    finally:
        await bus.close()
    assert again.spooled == ()
    assert again.duplicates == 4
    assert again.unread_lanes == ()
    assert not (lane / "unread").exists()


async def test_agent_inbox_spool_refuses_without_bus(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no bus injected"):
        await HandlerAgentInboxSpool().handle(request(overlay(tmp_path)))
