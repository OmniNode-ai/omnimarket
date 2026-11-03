# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A converged host stops reading as drifted; a failed read is not served as current."""

from datetime import timedelta
from typing import cast

import pytest

from omnimarket.nodes.node_host_drift_fleet import HandlerHostDriftFleet
from omnimarket.nodes.node_host_drift_fleet.handlers.fold_host_drift_view import (
    fold_view,
)
from omnimarket.nodes.node_host_drift_fleet.models import (
    EnumHostDriftVerdict,
    ModelHostDriftFleetRequest,
    ModelHostDriftOpenItem,
)
from omnimarket.nodes.node_host_drift_fleet.protocols import ProtocolHostDriftFleetBus

from .helpers import NOW, MemoryStore, heartbeat, overlay, resolved

pytestmark = pytest.mark.unit


def open_item(key: str, first_seen_offset: int) -> ModelHostDriftOpenItem:
    return ModelHostDriftOpenItem(
        key=key,
        kind="plugin",
        declared="merged",
        observed="behind",
        first_seen_at=NOW + timedelta(seconds=first_seen_offset),
        bound_seconds=60,
        verdict=EnumHostDriftVerdict.DRIFT,
    )


def test_resolution_closes_the_open_item_until_the_next_heartbeat() -> None:
    first = fold_view(
        previous=None,
        events=[heartbeat("host-a", open=(open_item("plugin:omni", -600),))],
        overlay=overlay(),
        now=NOW,
        consumer_group="g",
    )
    later = NOW + timedelta(seconds=30)
    second = fold_view(
        previous=first,
        events=[
            resolved(
                "host-a",
                key="plugin:omni",
                first_seen_at=NOW - timedelta(seconds=600),
                resolved_at=later,
            )
        ],
        overlay=overlay(),
        now=later,
        consumer_group="g",
    )
    host = next(h for h in second.hosts if h.host == "host-a")
    assert host.open == ()
    assert not any("plugin:omni" in alert for alert in second.alerts)


def test_reopened_item_survives_an_older_resolution() -> None:
    reopened = open_item("plugin:omni", 10)
    view = fold_view(
        previous=None,
        events=[
            heartbeat("host-a", open=(reopened,)),
            resolved("host-a", key="plugin:omni", resolved_at=NOW),
        ],
        overlay=overlay(),
        now=NOW,
        consumer_group="g",
    )
    host = next(h for h in view.hosts if h.host == "host-a")
    assert host.open == (reopened,)


class FailingBus:
    async def subscribe(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("broker unreachable")

    async def subscribe_ungrouped(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("broker unreachable")


async def test_failed_read_keeps_the_previous_view_and_says_so() -> None:
    earlier = fold_view(
        previous=None,
        events=[heartbeat("host-a", open=(open_item("plugin:omni", -600),))],
        overlay=overlay(),
        now=NOW,
        consumer_group="g",
    )
    store = MemoryStore(earlier)
    handler = HandlerHostDriftFleet(
        bus=cast(ProtocolHostDriftFleetBus, FailingBus()), state_store=store
    )
    result = await handler.handle(
        ModelHostDriftFleetRequest(
            host="host-a",
            previous=None,
            now=NOW + timedelta(hours=1),
            overlay=overlay(),
        )
    )
    assert not result.read_ok
    assert result.view.folded_at == earlier.folded_at
    assert result.view.status_line.startswith("host-drift: STALE")
    assert "broker unreachable" in result.view.status_line
    assert store.view == earlier
    assert store.saves == 0
