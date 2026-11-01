# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stable group regression proof and a falsifier for replay group minting."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_host_drift_fleet import HandlerHostDriftFleet
from omnimarket.nodes.node_host_drift_fleet.models import ModelHostDriftFleetRequest
from omnimarket.nodes.node_host_drift_fleet.node import load_topics

from .helpers import NOW, MemoryStore, RecordingBus, heartbeat, overlay, resolved

pytestmark = pytest.mark.unit


class MintingHandler(HandlerHostDriftFleet):
    def _group_id(self, request: ModelHostDriftFleetRequest) -> str:
        stable = super()._group_id(request)
        return f"{stable}.{uuid4().hex[:12]}" if request.replay else stable


@pytest.mark.parametrize("handler_type", [HandlerHostDriftFleet, MintingHandler])
async def test_host_drift_fleet_replays_groups_per_call(
    handler_type: type[HandlerHostDriftFleet],
) -> None:
    bus = EventBusInmemory(environment="test", group="fleet-test")
    await bus.start()
    spy = RecordingBus(bus)
    topics = load_topics()
    try:
        # Everything is published before the first read, including duplicates and an older heartbeat.
        for host in overlay().hosts:
            for beat in (
                heartbeat(host),
                heartbeat(
                    host, checked_at=NOW - timedelta(seconds=30), check_id="older"
                ),
            ):
                await bus.publish(
                    topics[0],
                    None,
                    json.dumps({"payload": beat.model_dump(mode="json")}).encode(),
                )
            event = resolved(host)
            for _ in range(2):
                await bus.publish(
                    topics[1],
                    None,
                    json.dumps({"payload": event.model_dump(mode="json")}).encode(),
                )
        for host in overlay().hosts:
            store = MemoryStore()
            handler = handler_type(bus=spy, clock=lambda: NOW, state_store=store)
            for replay in [True] * 20 + [False] * 3:
                result = await handler.handle(
                    ModelHostDriftFleetRequest(
                        host=host,
                        previous=store.view,
                        now=NOW,
                        overlay=overlay(),
                        replay=replay,
                    )
                )
                assert result.read_ok, result.error
                assert result.events_read == 8
                assert result.groups_used == (result.consumer_group,)
                assert result.view.consumer_group == result.consumer_group
                assert not result.view.alerts
                assert "OK 2/2 hosts reporting" in result.view.status_line
                assert [
                    (h.host, h.last_check_id, h.resolved_count)
                    for h in result.view.hosts
                ] == [("host-a", "check-new", 1), ("host-b", "check-new", 1)]
            groups = {
                group
                for _, group, _ in spy.subscriptions
                if group.startswith(f"{overlay().consumer_group_prefix}.{host}")
            }
            if handler_type is MintingHandler:
                assert (
                    len(groups) == 21
                )  # twenty replay identities plus the stable ordinary read
                assert any(re.search(r"\.[0-9a-f]{12}$", group) for group in groups)
            else:
                assert groups == {f"{overlay().consumer_group_prefix}.{host}"}
                assert all(
                    re.search(r"\.[0-9a-f]{12}$", group) is None for group in groups
                )
            assert store.saves == 23
        assert len(spy.subscriptions) == 2 * 23 * 2
        assert len(spy.stopped) == len(spy.subscriptions)
        assert all(reset == "earliest" for _, _, reset in spy.subscriptions)
        assert spy.ungrouped == []
    finally:
        await bus.close()


async def test_host_drift_fleet_replays_groups_per_call_overlay_and_saved_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "host_drift_fleet"
    root.mkdir()
    (root / "overlay.yaml").write_text(
        yaml.safe_dump(overlay().model_dump(mode="json"))
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    bus = EventBusInmemory(environment="test", group="fleet-test")
    await bus.start()
    spy = RecordingBus(bus)
    store = MemoryStore()
    handler = HandlerHostDriftFleet(bus=spy, clock=lambda: NOW, state_store=store)
    request = ModelHostDriftFleetRequest(host="host-a", previous=None, now=NOW)
    try:
        await bus.publish(
            load_topics()[0],
            None,
            json.dumps({"payload": heartbeat().model_dump(mode="json")}).encode(),
        )
        first = await handler.handle(request)
        await bus.clear_event_history()
        incremental = await handler.handle(request)
        assert incremental.view.hosts == first.view.hosts
        assert store.loads == 2
        replayed = await handler.handle(request.model_copy(update={"replay": True}))
        assert replayed.view.hosts[0].last_heartbeat_at is None
        assert store.loads == 2
        assert (
            first.consumer_group
            == incremental.consumer_group
            == replayed.consumer_group
        )
    finally:
        await bus.close()


@pytest.mark.parametrize(
    "failure", ["subscribe", "unsubscribe", "cancel", "timeout", "none"]
)
async def test_host_drift_fleet_replays_groups_per_call_cleanup_and_malformed(
    failure: str,
) -> None:
    stopped: list[int] = []
    entered = asyncio.Event()
    message = ModelEventMessage

    class FaultBus:
        async def subscribe(
            self,
            topic: str,
            on_message: Callable[[ModelEventMessage], Awaitable[None]],
            *,
            group_id: str,
            auto_offset_reset: str,
        ) -> Callable[[], Awaitable[None]]:
            index = load_topics().index(topic)
            if index == 1:
                entered.set()
                if failure == "subscribe":
                    raise ConnectionError("synthetic failure")
                if failure in ("cancel", "timeout"):
                    await asyncio.Event().wait()
            # Includes broken JSON, non-object payloads, and valid producer extensions.
            from omnibase_infra.event_bus.models.model_event_headers import (
                ModelEventHeaders,
            )

            headers = ModelEventHeaders(source="test", event_type="test", timestamp=NOW)
            if index == 0:
                for raw in (
                    b"{",
                    b'{"payload": []}',
                    b"{}",
                    json.dumps(
                        {
                            "payload": {
                                **heartbeat().model_dump(mode="json"),
                                "new_extension": True,
                            }
                        }
                    ).encode(),
                ):
                    await on_message(message(topic=topic, value=raw, headers=headers))

            async def stop() -> None:
                stopped.append(index)
                if failure == "unsubscribe" and index == 1:
                    raise ConnectionError("teardown failure")
                # A valid final callback must be drained after teardown.
                if index == 0:
                    await on_message(
                        message(
                            topic=topic,
                            value=json.dumps(
                                {"payload": heartbeat("host-b").model_dump(mode="json")}
                            ).encode(),
                            headers=headers,
                        )
                    )

            return stop

    store = MemoryStore()
    handler = HandlerHostDriftFleet(
        bus=FaultBus(), clock=lambda: NOW, state_store=store
    )
    task = asyncio.create_task(
        handler.handle(
            ModelHostDriftFleetRequest(
                host="host-a",
                previous=None,
                now=NOW,
                overlay=overlay(read_window_seconds=0.08),
            )
        )
    )
    if failure == "cancel":
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == [0]
        assert store.saves == 0
        return
    result = await task
    assert stopped == ([0] if failure in ("subscribe", "timeout") else [1, 0])
    assert result.read_ok is (failure == "none")
    assert result.view.malformed == 3
    assert result.events_read == 5
    assert not result.view.alerts
    assert store.view == result.view
