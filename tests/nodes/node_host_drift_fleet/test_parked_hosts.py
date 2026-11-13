# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: a host the lab host table parks is never required to heartbeat and never alerts.

Golden: of two overlay hosts the table parks one; the fold alerts nothing, counts the other
as the only required reporter and lists the parked host with the table's reason on the status
line. Control: the same fold without the parked set alerts the silent host as never reported.
Error chain: a table the handler cannot read leaves every host required, alerts the silent
host and says the table is unread on the view. A parked host that reports anyway and carries
an open drift item still adds no alert.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_host_drift_fleet import HandlerHostDriftFleet
from omnimarket.nodes.node_host_drift_fleet.adapters.parked_hosts import (
    InMemoryParkedSource,
)
from omnimarket.nodes.node_host_drift_fleet.handlers.fold_host_drift_view import (
    fold_view,
)
from omnimarket.nodes.node_host_drift_fleet.models import (
    EnumHostDriftVerdict,
    ModelHostDriftFleetRequest,
    ModelHostDriftOpenItem,
    ModelHostDriftParkedHost,
)
from omnimarket.nodes.node_host_drift_fleet.node import load_topics

from .helpers import NOW, MemoryStore, RecordingBus, heartbeat, overlay

pytestmark = pytest.mark.unit

REASON = "parked, operator 2026-11-13"


def test_golden_a_parked_host_is_listed_and_never_required() -> None:
    view = fold_view(
        previous=None,
        events=(heartbeat("host-a"),),
        overlay=overlay(),
        now=NOW,
        consumer_group="test",
        parked={"host-b": REASON},
    )
    assert view.alerts == ()
    assert view.parked == (ModelHostDriftParkedHost(host="host-b", reason=REASON),)
    assert view.parked_cause == ""
    assert (
        "parked, not required: host-b (parked, operator 2026-11-13)" in view.status_line
    )
    assert view.status_line.startswith("host-drift: OK 1/1 hosts reporting")


def test_control_without_the_parked_set_the_silent_host_alerts() -> None:
    view = fold_view(
        previous=None,
        events=(heartbeat("host-a"),),
        overlay=overlay(),
        now=NOW,
        consumer_group="test",
    )
    assert view.alerts == ("host-b heartbeat: never reported (age unknown)",)
    assert view.parked == ()
    assert "parked, not required" not in view.status_line


def test_a_parked_host_that_reports_still_adds_no_alert() -> None:
    open_item = ModelHostDriftOpenItem(
        key="unit:service",
        kind="unit",
        declared="running",
        observed="stopped",
        first_seen_at=NOW - timedelta(seconds=60),
        bound_seconds=60,
        verdict=EnumHostDriftVerdict.CHECK_FAILED,
    )
    view = fold_view(
        previous=None,
        events=(
            heartbeat("host-a"),
            heartbeat("host-b", open=(open_item,)),
        ),
        overlay=overlay(),
        now=NOW,
        consumer_group="test",
        parked={"host-b": REASON},
    )
    assert view.alerts == ()
    assert "OK 1/1" in view.status_line


async def test_golden_the_handler_parks_the_tables_host() -> None:
    bus = EventBusInmemory(environment="test", group="fleet-parked-golden")
    await bus.start()
    try:
        beat = heartbeat("host-a")
        await bus.publish(
            load_topics()[0],
            None,
            json.dumps({"payload": beat.model_dump(mode="json")}).encode(),
        )
        handler = HandlerHostDriftFleet(
            bus=RecordingBus(bus),
            clock=lambda: NOW,
            state_store=MemoryStore(),
            parked_source=InMemoryParkedSource({"host-b": REASON}),
        )
        result = await handler.handle(
            ModelHostDriftFleetRequest(
                host="host-a", previous=None, now=NOW, overlay=overlay()
            )
        )
    finally:
        await bus.close()
    assert result.read_ok, result.error
    assert result.view.alerts == ()
    assert result.view.parked == (
        ModelHostDriftParkedHost(host="host-b", reason=REASON),
    )
    assert "parked, not required: host-b" in result.view.status_line


async def test_error_an_unreadable_table_leaves_every_host_required() -> None:
    bus = EventBusInmemory(environment="test", group="fleet-parked-error")
    await bus.start()
    try:
        beat = heartbeat("host-a")
        await bus.publish(
            load_topics()[0],
            None,
            json.dumps({"payload": beat.model_dump(mode="json")}).encode(),
        )
        handler = HandlerHostDriftFleet(
            bus=RecordingBus(bus),
            clock=lambda: NOW,
            state_store=MemoryStore(),
            parked_source=InMemoryParkedSource({}, error="no host table"),
        )
        result = await handler.handle(
            ModelHostDriftFleetRequest(
                host="host-a", previous=None, now=NOW, overlay=overlay()
            )
        )
    finally:
        await bus.close()
    assert result.read_ok, result.error
    assert result.view.parked == ()
    assert result.view.parked_cause.startswith("ValueError")
    assert result.view.alerts == ("host-b heartbeat: never reported (age unknown)",)
    assert "host table unread" in result.view.status_line
    assert "every host is required" in result.view.status_line
